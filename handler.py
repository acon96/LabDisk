from contextlib import suppress
import logging
import os

import kopf
import kubernetes

import config
from config import Constants
import util
import nfs
import lvm
import iscsi

util.setup_kube_client()

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("kopf").setLevel(logging.INFO)
logging.getLogger("kubernetes").setLevel(logging.INFO)
logger = logging.getLogger("handler")

config.setup()

registered_storage_classes = []

@kopf.on.login()
def api_login(**kwargs):
    return kopf.login_via_client(**kwargs)

def validate_and_register_storage_class(name, type):
    if type.lower() not in [Constants.VOLUME_TYPE_ISCSI, Constants.VOLUME_TYPE_NFS, Constants.VOLUME_TYPE_SHARED]:
        logger.error(f"Unrecognized LabDisk volume type: {type.lower()}")
        return

    enabled_volume_types = []
    if config.get().individual_volumes_enabled:
        enabled_volume_types.extend([Constants.VOLUME_TYPE_NFS, Constants.VOLUME_TYPE_ISCSI])
    
    if config.get().shared_volumes_enabled:
        enabled_volume_types.append(Constants.VOLUME_TYPE_SHARED)

    if type.lower() not in enabled_volume_types:
        logger.error(f"Ignoring storage class for type '{type}' because the subsystem that handles it is not enabled.")
        return

    logger.info(f"Found valid LabDisk storage class {name}. Registering PVC Handlers.")
    registered_storage_classes.append(name)

@kopf.on.create("storageclass", field="provisioner", value=config.get().provisioner_name)
def new_storageclass(name, **kwargs):
    storage_api = kubernetes.client.StorageV1Api()
    sc = storage_api.read_storage_class(name)

    validate_and_register_storage_class(name, sc.parameters['type'])

@kopf.on.startup()
async def operator_startup(**kwargs):
    storage_api = kubernetes.client.StorageV1Api()
    storage_classes = storage_api.list_storage_class()
    for sc in storage_classes.items:
        metadata = sc.metadata
        if sc.provisioner != config.get().provisioner_name:
            logger.debug(f"Ignoring storage class {metadata.name}...")
            continue

        validate_and_register_storage_class(metadata.name, sc.parameters["type"])

    if config.get().shared_volumes_enabled:
        logger.info("Starting shared NFS export...")
        # make sure the main nfs share that backs shared volumes is exported
        nfs.export_share(config.get().shared_nfs_root, config.get().nfs_access_cidr)
    else:
        logger.info("Shared volume subsystem will be disabled.")
    
    if config.get().individual_volumes_enabled:
        logger.info("Starting individual volumes subsystem...")
        iscsi.init_iscsi(config.get().current_node_name, config.get().iscsi_portal_addr, None)
    else:
        logger.info("Individual volume subsystem will be disabled.")


@kopf.on.resume("persistentvolume")
def register_existing_volumes(spec, meta, **kwargs):
    storage_class = spec["storageClassName"]
    pv_name = meta["name"]

    # make sure it is a storage class that we manage
    if storage_class not in registered_storage_classes:
        logger.debug(f"Not registering volume {pv_name} because it is not a lab-disk volume")
        return

    # only process volumes on this node
    asigned_node = meta["annotations"].get(Constants.PV_NODE_ANNOTATION_KEY, "")
    if asigned_node != config.get().current_node_name:
        logger.debug(f"Not registering volume {pv_name} because it is served by a different node")
        return

    sc_params = get_storage_class_params(storage_class)
    volume_type = sc_params["type"]

    # re-mount individual NFS exports
    if volume_type == Constants.VOLUME_TYPE_NFS:
        mount_point = f"/srv/nfs/{pv_name}"
        logger.debug(f"Exporting NFS share for {mount_point}")
        nfs.export_share(mount_point, config.get().nfs_access_cidr)
    elif volume_type == Constants.VOLUME_TYPE_ISCSI:
        lvm_group = config.get().lvm_group
        logger.debug(f"Exporting iSCSI share for {lvm_group}:{pv_name}")
        iscsi.export_disk(lvm_group, pv_name)

    logger.info(f"Successfully registered existing pv '{pv_name}'")


storage_class_params_cache = {}
def get_storage_class_params(name):
    global storage_class_params_cache
    if name in storage_class_params_cache:
        return storage_class_params_cache[name]
    
    storage_api = kubernetes.client.StorageV1Api()
    storage_class = storage_api.read_storage_class(name)

    params = storage_class.parameters
    params["reclaim_policy"] = storage_class.reclaim_policy
    params["allow_volume_expansion"] = storage_class.allow_volume_expansion
    params["mount_options"] = storage_class.mount_options
    params["annotations"] = storage_class.metadata.annotations

    storage_class_params_cache[name] = params

    return params

def validate_pvc_spec(spec, meta, update=False):
    spec = dict(spec)
    storage_class_params = get_storage_class_params(spec["storageClassName"])
    if storage_class_params["type"] != Constants.VOLUME_TYPE_SHARED and ("ReadWriteMany" in spec["accessModes"] or "ReadOnlyMany" in spec["accessModes"]):
        raise kopf.PermanentError(f"LabDisk only supports ReadWriteMany/ReadOnlyMany volumes using the '{Constants.VOLUME_TYPE_SHARED}' disk type")
    
    if Constants.NODE_SELECTOR_ANNOTATION_KEY not in meta.annotations:
        raise kopf.PermanentError(f"No node was selected to store the volume. (PVC missing annotation '{Constants.NODE_SELECTOR_ANNOTATION_KEY}'")
    
    return storage_class_params

@kopf.on.create("persistentvolumeclaim")
def create_volume(meta, spec, **kwargs):
    storage_class_params = validate_pvc_spec(spec, meta)

    if spec["storageClassName"] not in registered_storage_classes:
        logger.info(f"Volume creation for storage class that we are not monitoring. ({spec['storageClassName']})")
        return

    volume_node = meta.annotations[Constants.NODE_SELECTOR_ANNOTATION_KEY]
    current_node_name = config.get().current_node_name
    if volume_node != current_node_name:
        logger.info(f"Volume creation not for this node. (Request: {volume_node}, Us: {current_node_name}")
        return

    desired_volume_size = spec["resources"].get("limits", {}).get("storage", spec["resources"].get("requests", {}).get("storage"))
    access_modes = spec["accessModes"]
    volume_type = storage_class_params["type"]
    pv_name = f"pvc-{meta.uid}"
    fs_type = meta.annotations.get(Constants.FILESYSTEM_ANNOTATION_KEY, "xfs")

    if not desired_volume_size:
        raise kopf.PermanentError("No volume size provided")

    if volume_type == Constants.VOLUME_TYPE_SHARED:
        if not config.get().shared_volumes_enabled:
            raise kopf.PermanentError("This instance of LabDisk does not have shared volumes configured")

        storage_path = meta.annotations.get(Constants.SHARED_STORAGE_PATH_ANNOTATION_KEY)
        if storage_path == None:
            raise kopf.PermanentError(f"No storage path provided for shared storage. (PVC missing annotation '{Constants.SHARED_STORAGE_PATH_ANNOTATION_KEY}'")

        # prevent misusing shared storage to gain access to other host paths
        if ".." in storage_path:
            raise kopf.PermanentError(f"Cannot use storage path outside of the shared NFS root! (storage path '{storage_path}')")

        volume_directory = f"{config.get().shared_nfs_root}/{storage_path}"
        with suppress(FileExistsError):
            os.mkdir(volume_directory)

        # create the pv object using the main nfs share and the subpath for this volume
        nfs.create_persistent_volume(pv_name, current_node_name, access_modes, desired_volume_size, config.get().current_node_ip, volume_directory, spec["storageClassName"], spec["volumeMode"])
    else:
        if not config.get().individual_volumes_enabled:
            raise kopf.PermanentError("This instance of LabDisk does not have individual volumes configured")      

        lvm_group = config.get().lvm_group

        if volume_type == Constants.VOLUME_TYPE_ISCSI:
            # provision lvm volume
            lvm.create_volume(lvm_group, pv_name, fs_type, desired_volume_size)

            # setup iscsi exports using rtstlib-fb
            iscsi_lun = iscsi.export_disk(lvm_group, pv_name)

            # create the pv object using the iscsi share info
            iscsi_portal = f"{config.get().current_node_ip}:{config.get().iscsi_portal_port}"
            iscsi_target = f"iqn.2003-01.org.linux-iscsi.ragdollphysics:{config.get().current_node_name}"
            iscsi.create_persistent_volume(pv_name, current_node_name, access_modes, desired_volume_size, iscsi_portal, iscsi_target, iscsi_lun, fs_type, spec["storageClassName"], spec["volumeMode"])

        if volume_type == Constants.VOLUME_TYPE_NFS:
            mount_point = f"/srv/nfs/{pv_name}"

            # provision lvm volume then locally mount it where NFS can access it and the set up a NFS share
            lvm.create_volume(lvm_group, pv_name, fs_type, desired_volume_size, mount_point)

            # export the share
            nfs.export_share(mount_point, config.get().nfs_access_cidr)

            # create the pv object using the share we just exported
            nfs.create_persistent_volume(pv_name, current_node_name, access_modes, desired_volume_size, config.get().current_node_ip, mount_point, spec["storageClassName"], spec["volumeMode"])

    logger.info(f"Successfully provisioned volume for claim {meta.name}")

@kopf.on.update("persistentvolumeclaim")
def update_volume_claim(spec, meta, old, new, diff, **kwargs):
    validate_pvc_spec(spec, meta, update=True)


@kopf.on.delete("persistentvolumeclaim")
def delete_volume_claim(spec, meta, **kwargs):
    storage_class = spec["storageClassName"]
    sc_params = get_storage_class_params(storage_class)
    volume_name = spec["volumeName"]
    retain_volume = sc_params["reclaim_policy"] == "Retain"

    if retain_volume:
        logger.info(f"Retaining PV after deleting PVC '{meta['name']}")
    else:
        logger.info(f"Deleting PV after deleting PVC '{meta['name']}")

        core_api = kubernetes.client.CoreV1Api()
        core_api.delete_persistent_volume(volume_name)


@kopf.on.delete("persistentvolume")
def delete_volume(spec, meta, **kwargs):
    storage_class = spec["storageClassName"]
    sc_params = get_storage_class_params(storage_class)
    volume_type = sc_params["type"]
    pv_name = meta["name"]
    lvm_group = config.get().lvm_group

    if volume_type == Constants.VOLUME_TYPE_SHARED:
        return # nothing to do for shared volumes

    elif volume_type == Constants.VOLUME_TYPE_NFS:
        mount_point = f"/srv/nfs/{pv_name}"
        nfs.un_export_share(mount_point, config.get().nfs_access_cidr)

        # unmount the share location
        lvm.unmount_volume(mount_point, lvm_group, pv_name)
        
    elif volume_type == Constants.VOLUME_TYPE_ISCSI:
        iscsi.un_export_disk(lvm_group, pv_name)

    # delete the volume (if destructive actions are on)
    lvm.delete_volume(lvm_group, pv_name)
