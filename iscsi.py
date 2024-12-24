import threading
import logging
import secrets
import base64

import kubernetes
from rtslib_fb.root import RTSRoot, RTSLibError
from rtslib_fb.target import Target, TPG
from rtslib_fb.tcm import BlockStorageObject, RTSLibNotInCFS
from rtslib_fb.fabric import ISCSIFabricModule

from util import run_process
from config import Constants, AuthConfig

logger = logging.getLogger(__name__)

root = None
tpg = None


iscsi_config_lock = threading.Lock()
def update_iscsi_config():
    iscsi_config_lock.acquire()
    root.save_to_file()
    iscsi_config_lock.release()

def create_lun_from_volume(pool_name, vol_name, lun_idx=None):
    device_path = f"/dev/{pool_name}/{vol_name}"

    # get serial number of volume which is consistent
    vol_serial = run_process("blkid", device_path, "--output", "value")[0]

    # only add new SO if it doesn't exist
    # so.name concats pool & vol names separated by ':'
    so_name = f"{pool_name}:{vol_name}"
    try:
        # TODO: figure out why this sometimes fails even though the device exists. can we look it up by path instead?
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
        return tpg.lun(storage_object=so, lun=lun_idx)

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

def export_lun_for_initiator(initiator_wwn, lun, auth_config):
    node_acl = tpg.node_acl(initiator_wwn)

    if auth_config:
        _, _, node_acl.chap_userid, node_acl.chap_password = auth_config.get_credentials()

    # only create mappedlun if it doesn't already exist
    try:
        node_acl.mapped_lun(lun.lun)
    except RTSLibNotInCFS:
        node_acl.mapped_lun(lun.lun, tpg_lun=lun)
        update_iscsi_config()

# export the disk for all nodes
def export_disk(lvm_pool, disk_name, auth_config, desired_lun_idx=None):
    core_api = kubernetes.client.CoreV1Api()
    nodes = core_api.list_node()
    lun = create_lun_from_volume(lvm_pool, disk_name, lun_idx=desired_lun_idx)

    for node in nodes.items:
        node_name = node.metadata.name
        initiator_name = f"iqn.2003-01.org.linux-iscsi.ragdollphysics:{node_name}"
        export_lun_for_initiator(initiator_name, lun, auth_config)

    return lun.lun

def un_export_lun_for_initiator(initiator_wwn, lun):
    node_acl = tpg.node_acl(initiator_wwn)
    
    # delete the lun and mapped lun
    try:
        mapped_lun = node_acl.mapped_lun(lun.lun)
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

def create_persistent_volume(pv_name, node_name, access_modes, desired_capacity, iscsi_portal, iscsi_target, iscsi_lun, fs_type, sc_name, volume_mode, auth_config):
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

    if auth_config:
        pv["iscsi"] = {
            **pv["iscsi"],
            "chapAuthDiscovery": True,
            "chapAuthSession": True,
            "secretRef": {
                "name": auth_config.chap_credentials_secret,
            }
        }

    body = kubernetes.client.V1PersistentVolume(api_version='v1', spec=pv,
        metadata=kubernetes.client.V1ObjectMeta(
            name=pv_name, 
            labels={"app": "storage", "component": "lab-disk"},
            annotations={Constants.PV_ASSIGNED_NODE_ANNOTATION_KEY: node_name}
        ), 
        kind="PersistentVolume"
    )

    core_api = kubernetes.client.CoreV1Api()
    core_api.create_persistent_volume(body)

def enable_chap_auth(tpg: TPG, auth_config: AuthConfig):
    """Enable CHAP authentication for the target."""
    if not tpg:
        raise RuntimeError("TPG is not initialized. Create a target first.")

    core_api = kubernetes.client.CoreV1Api()
    discovery_username, discovery_password, session_username, session_password = None, None, None, None
    try:
        discovery_username, discovery_password, session_username, session_password = auth_config.get_credentials()
    except:
        if auth_config.generate_if_not_exists:
            # use the same username + password combo for discovery and session for auto-generated credentials
            username = secrets.token_hex(8)  # 16-character hex string
            password = secrets.token_urlsafe(16)  # URL-safe 22-character string
            
            new_secret = kubernetes.client.V1Secret(
                type="kubernetes.io/iscsi-chap",
                metadata=kubernetes.client.V1ObjectMeta(
                    name=auth_config.chap_credentials_secret,
                ),
                string_data={
                    "discovery.sendtargets.auth.username": username,
                    "discovery.sendtargets.auth.password": password,
                    "node.session.auth.username": username,
                    "node.session.auth.password": password,
                 }
            )
            core_api.create_namespaced_secret(auth_config.secret_root_namespace, new_secret)

            discovery_username, discovery_password, session_username, session_password = username, password, username, password
        else:
            raise Exception(f"CHAP Auth is enabled but could not find secret {auth_config.chap_credentials_secret}")

    new_secret = kubernetes.client.V1Secret(
        type="kubernetes.io/iscsi-chap",
        metadata=kubernetes.client.V1ObjectMeta(
            name=auth_config.chap_credentials_secret,
        ),
        string_data={
            "discovery.sendtargets.auth.username": discovery_username,
            "discovery.sendtargets.auth.password": discovery_password,
            "node.session.auth.username": session_username,
            "node.session.auth.password": session_password,
        }
    )
    for namespace in auth_config.secret_replica_namespaces:
        # create or replace CHAP secret in all supported namespaces
        try:
            core_api.read_namespaced_secret(namespace=namespace, name=auth_config.chap_credentials_secret)
            core_api.replace_namespaced_secret(namespace=namespace, name=auth_config.chap_credentials_secret, body=new_secret)
        except:
            core_api.create_namespaced_secret(namespace=namespace, body=new_secret)
    
    tpg.set_attribute("authentication", "1")
    tpg.chap_userid = session_username
    tpg.chap_password = session_password

    fabric_module = ISCSIFabricModule()
    fabric_module.discovery_userid = discovery_username
    fabric_module.discovery_password = discovery_password
    fabric_module.discovery_enable_auth = True

# ensure the TPG and network portal are properly configured
def init_iscsi(node_name, portal_address, auth_config=None):
    global root, tpg

    root = RTSRoot()
    tpg = TPG(Target(ISCSIFabricModule(), f"iqn.2003-01.org.linux-iscsi.ragdollphysics:{node_name}"), 1)
    tpg.enable = True

    if auth_config:
        enable_chap_auth(tpg, auth_config)
    else:
        tpg.set_attribute("authentication", "0")

    portal_address = portal_address.split(":")
    tpg.network_portal(portal_address[0], int(portal_address[1]))

    update_iscsi_config()