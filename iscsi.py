import os
import threading
import logging
from contextlib import suppress

import kubernetes
from rtslib_fb import FabricModule, Target, TPG, RTSRoot, BlockStorageObject, LUN, MappedLUN, RTSLibError, RTSLibNotInCFS

from util import run_process
from config import Constants

logger = logging.getLogger(__name__)

root = None
tpg = None

iscsi_config_lock = threading.Lock()
def update_iscsi_config():
    iscsi_config_lock.acquire()
    root.save_to_file()
    iscsi_config_lock.release()

def create_lun_from_volume(pool_name, vol_name):
    device_path = f"/dev/{pool_name}/{vol_name}"

    # get serial number of volume which is consistent
    vol_serial = run_process("blkid", device_path, "--output", "value")[0]

    # only add new SO if it doesn't exist
    # so.name concats pool & vol names separated by ':'
    so_name = f"{pool_name}:{vol_name}"
    try:
        so = BlockStorageObject(so_name)
    except RTSLibNotInCFS:
        so = BlockStorageObject(so_name, dev=device_path)
        so.wwn = vol_serial

    # export useful scsi model if kernel > 3.8
    # with ignored(RTSLibError):
    #     so.set_attribute("emulate_model_alias", "1")

    # only add tpg lun if it doesn't exist
    for existing_lun in tpg.luns:
        if existing_lun.storage_object.name == so.name and existing_lun.storage_object.plugin == "block":
            return existing_lun
    else:
        return LUN(tpg, storage_object=so)

def find_lun_for_volume(pool_name, vol_name):
    # so.name concats pool & vol names separated by ':'
    try:
        so = BlockStorageObject(f"{pool_name}:{vol_name}")
    except RTSLibNotInCFS:
        return None

    # only add tpg lun if it doesn't exist
    for existing_lun in tpg.luns:
        if existing_lun.storage_object.name == so.name and existing_lun.storage_object.plugin == "block":
            return existing_lun
    
    return None

def export_lun_for_initiator(initiator_wwn, lun):
    node_acl = tpg.node_acl(initiator_wwn)

    # only create mappedlun if it doesn't already exist
    try:
        MappedLUN(node_acl, lun.lun)
    except RTSLibNotInCFS:
        MappedLUN(node_acl, lun.lun, lun)
        update_iscsi_config()

# export the disk for all nodes
def export_disk(lvm_pool, disk_name):
    core_api = kubernetes.client.CoreV1Api()
    nodes = core_api.list_node()
    lun = create_lun_from_volume(lvm_pool, disk_name)

    for node in nodes.items:
        node_name = node.metadata.name
        initiator_name = f"iqn.2003-01.org.linux-iscsi.ragdollphysics:{node_name}"
        export_lun_for_initiator(initiator_name, lun)

    return lun.lun

def un_export_lun_for_initiator(initiator_wwn, lun):
    node_acl = tpg.node_acl(initiator_wwn)
    
    # delete the lun and mapped lun
    try:
        mapped_lun = MappedLUN(node_acl, lun.lun)
        mapped_lun.delete()
        
    except RTSLibNotInCFS:
        pass
        
# unexport the disk for all nodes
def un_export_disk(lvm_pool, disk_name):
    core_api = kubernetes.client.CoreV1Api()
    nodes = core_api.list_node()
    lun = find_lun_for_volume(lvm_pool, disk_name)

    if not lun:
        return

    for node in nodes.items:
        node_name = node.metadata.name
        initiator_name = f"iqn.2003-01.org.linux-iscsi.ragdollphysics:{node_name}"
        un_export_lun_for_initiator(initiator_name, lun)

    if lun.storage_object:
        lun.storage_object.delete()

    try:
        lun.delete()
    except RTSLibNotInCFS:
        pass

    update_iscsi_config()

def create_persistent_volume(pv_name, node_name, access_modes, desired_capacity, iscsi_portal, iscsi_target, iscsi_lun, fs_type, sc_name, volume_mode):
    pv = {
        "accessModes": access_modes,
        "capacity": {"storage": desired_capacity},
        # "mount_options": None,
        "iscsi": {
            "readOnly": False,
            "targetPortal": iscsi_portal,
            "iqn": iscsi_target,
            "lun": iscsi_lun,
            "fsType": fs_type
        },
        "storageClassName": sc_name,
        "volumeMode": volume_mode,
    }

    body = kubernetes.client.V1PersistentVolume(api_version='v1', spec=pv,
        metadata=kubernetes.client.V1ObjectMeta(
            name=pv_name, 
            labels={"app": "storage", "component": "lab-disk"},
            annotations={Constants.PV_NODE_ANNOTATION_KEY: node_name}
        ), 
        kind="PersistentVolume"
    )

    core_api = kubernetes.client.CoreV1Api()
    core_api.create_persistent_volume(body)


# ensure the TPG and network portal are properly configured
def init_iscsi(node_name, portal_address, auth_config=None):
    global root, tpg

    # make sure dbroot exists for RTSLib
    with suppress(FileExistsError):
        os.mkdir("/etc/target")

    root = RTSRoot()
    tpg = TPG(Target(FabricModule("iscsi"), f"iqn.2003-01.org.linux-iscsi.ragdollphysics:{node_name}"), 1)
    tpg.enable = True

    if auth_config:
        raise NotImplementedError()
    else:
        tpg.set_attribute("authentication", "0")

    portal_address = portal_address.split(":")
    tpg.network_portal(portal_address[0], int(portal_address[1]))

    update_iscsi_config()