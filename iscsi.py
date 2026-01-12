import threading
import logging
import secrets
import base64

import kubernetes
from rtslib.root import RTSRoot
from rtslib.target import Target, TPG
from rtslib.tcm import BlockStorageObject, RTSLibNotInCFSError
from rtslib.fabric import ISCSIFabricModule

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
    """Return a LUN for the given volume, enforcing a specific index if requested.

    Behavior:
    - If no LUN index is requested (lun_idx is None):
      * Reuse an existing LUN for the volume if one exists.
      * Otherwise create a new LUN letting rtslib choose the index.
    - If a LUN index is requested (lun_idx is not None):
      * If a LUN for the volume exists and already has that index, reuse it.
      * If a LUN for the volume exists but with a different index, recreate it
        at the requested index.
      * If no LUN exists, create a new one at the requested index.

    This ensures that when we resume PVs with a specified LUN index, the
    exported LUN index matches the value stored in the PV spec.
    """

    device_path = f"/dev/{pool_name}/{vol_name}"

    # get serial number of volume which is consistent
    vol_serial = run_process("blkid", device_path, "--output", "value")[0]

    # only add new SO if it doesn't exist
    # so.name concats pool & vol names separated by ':'
    so_name = f"{pool_name}:{vol_name}"
    try:
        # TODO: figure out why this sometimes fails even though the device exists. can we look it up by path instead?
        so = BlockStorageObject(so_name)
    except RTSLibNotInCFSError:
        so = BlockStorageObject(so_name, dev=device_path)
        so.wwn = vol_serial

    # export useful scsi model if kernel > 3.8
    # with ignored(RTSLibError):
    #     so.set_attribute("emulate_model_alias", "1")

    # First, try to find an existing LUN for this storage object.
    existing_lun = None
    for tpg_lun in tpg.luns:
        if tpg_lun.storage_object.name == so.name and tpg_lun.storage_object.plugin == "block":
            existing_lun = tpg_lun
            break

    # If no specific index is requested, just reuse or create.
    if lun_idx is None:
        if existing_lun is not None:
            return existing_lun
        # Create a new LUN and let rtslib choose the index.
        new_lun = tpg.lun(lun_idx, storage_object=so)
        update_iscsi_config()
        return new_lun

    # A specific LUN index was requested.
    if existing_lun is not None:
        if existing_lun.lun == lun_idx:
            # Already using the requested index.
            return existing_lun

        # The existing LUN has a different index. Recreate it at the
        # requested index to keep PV spec and exported LUN in sync.
        try:
            existing_lun.delete()
        except RTSLibNotInCFSError:
            # If it disappeared between lookup and delete, just ignore and
            # create a fresh one below.
            pass

    # At this point, no suitable LUN exists: create one at the desired index.
    new_lun = tpg.lun(lun_idx, storage_object=so)
    update_iscsi_config()
    return new_lun

def find_lun_for_volume(pool_name, vol_name):
    # so.name concats pool & vol names separated by ':'
    try:
        so = BlockStorageObject(f"{pool_name}:{vol_name}")
    except RTSLibNotInCFSError:
        return None

    # only add tpg lun if it doesn't exist
    for existing_lun in tpg.luns:
        if existing_lun.storage_object.name == so.name and existing_lun.storage_object.plugin == "block":
            return existing_lun
    
    return None

def export_lun_for_initiator(initiator_wwn, lun, auth_config):
    node_acl = tpg.node_acl(initiator_wwn)

    if auth_config:
        credentials = auth_config.get_credentials()
        node_acl.chap_userid = credentials["session_username"]
        node_acl.chap_password = credentials["session_password"]
        node_acl.chap_mutual_userid = credentials["session_username_in"]
        node_acl.chap_mutual_password = credentials["session_password_in"]

    # only create mappedlun if it doesn't already exist
    try:
        node_acl.mapped_lun(lun.lun)
    except RTSLibNotInCFSError:
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

    # ``create_lun_from_volume`` guarantees that when a desired index is
    # provided, the returned LUN object will have that index.  No additional
    # mismatch checks are necessary here.
    return lun.lun

def un_export_lun_for_initiator(initiator_wwn, lun):
    node_acl = tpg.node_acl(initiator_wwn)
    
    # delete the lun and mapped lun
    try:
        mapped_lun = node_acl.mapped_lun(lun.lun)
        mapped_lun.delete()
        
    except RTSLibNotInCFSError:
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
    except RTSLibNotInCFSError:
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
    discovery_username, discovery_password, discovery_username_in, discovery_password_in, \
        session_username, session_password, session_username_in, session_password_in = None, None, None, None, None, None, None, None
    try:
        credentials = auth_config.get_credentials()
        discovery_username = credentials["discovery_username"]
        discovery_password = credentials["discovery_password"]
        discovery_username_in = credentials["discovery_username_in"]
        discovery_password_in = credentials["discovery_password_in"]
        session_username = credentials["session_username"]
        session_password = credentials["session_password"]
        session_username_in = credentials["session_username_in"]
        session_password_in = credentials["session_password_in"]
    except:
        if auth_config.generate_if_not_exists:
            # use the same username + password combo for discovery and session for auto-generated credentials
            username = secrets.token_hex(8)  # 16-character hex string
            password = secrets.token_urlsafe(16)  # URL-safe 22-character string

            username_in = secrets.token_hex(8)  # 16-character hex string
            password_in = secrets.token_urlsafe(16)  # URL-safe 22-character string
            
            new_secret = kubernetes.client.V1Secret(
                type="kubernetes.io/iscsi-chap",
                metadata=kubernetes.client.V1ObjectMeta(
                    name=auth_config.chap_credentials_secret,
                ),
                string_data={
                    "discovery.sendtargets.auth.username": username,
                    "discovery.sendtargets.auth.password": password,
                    "discovery.sendtargets.auth.username_in": username_in,
                    "discovery.sendtargets.auth.password_in": password_in,
                    "node.session.auth.username": username,
                    "node.session.auth.password": password,
                    "node.session.auth.username_in": username_in,
                    "node.session.auth.password_in": password_in,
                 }
            )
            core_api.create_namespaced_secret(auth_config.secret_root_namespace, new_secret)

            discovery_username = username
            discovery_password = password
            discovery_username_in = username_in
            discovery_password_in = password_in
            session_username = username
            session_password = password
            session_username_in = username_in
            session_password_in = password_in
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
            "discovery.sendtargets.auth.username_in": discovery_username_in,
            "discovery.sendtargets.auth.password_in": discovery_password_in,
            "node.session.auth.username": session_username,
            "node.session.auth.password": session_password,
            "node.session.auth.username_in": session_username_in,
            "node.session.auth.password_in": session_password_in,
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
    tpg.chap_mutual_userid = session_username_in
    tpg.chap_mutual_password = session_password_in

    fabric_module = ISCSIFabricModule()
    fabric_module.discovery_userid = discovery_username
    fabric_module.discovery_password = discovery_password
    fabric_module.discovery_mutual_userid = discovery_username_in
    fabric_module.discovery_mutual_password = discovery_password_in
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