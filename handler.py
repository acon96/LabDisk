import sys
import logging

import kopf
import kubernetes

import config
import util
import nfs
import lvm

NODE_SELECTOR_ANNOTATION_KEY = "ragdollphysics.org/disk-node"
SHARED_STORAGE_PATH_ANNOTATION_KEY = "ragdollphysics.org/shared-storage-path"
FILESYSTEM_ANNOTATION_KEY = "ragdollphysics.org/filesystem"
VOLUME_TYPE_NFS = "nfs"
VOLUME_TYPE_ISCSI = "iscsi"
VOLUME_TYPE_SHARED = "shared-nfs"

util.setup_kube_client()
logger = logging.getLogger("handler")

config.setup()

registered_storage_classes = []

@kopf.on.login()
def api_login(**kwargs):
    return kopf.login_via_client(**kwargs)

def validate_and_register_storage_class(name, type):
    if type.lower() not in [VOLUME_TYPE_ISCSI, VOLUME_TYPE_NFS, VOLUME_TYPE_SHARED]:
        logger.error(f"Unrecognized LabDisk volume type: {type.lower()}")
        return

    enabled_volume_types = []
    if config.get().individual_volumes_enabled:
        enabled_volume_types.extend([VOLUME_TYPE_NFS, VOLUME_TYPE_ISCSI])
    
    if config.get().shared_volumes_enabled:
        enabled_volume_types.append(VOLUME_TYPE_SHARED)

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

    if config.get().shared_volumes_enabled:
        logger.info("Starting shared NFS export...")
        # make sure the main nfs share that backs shared volumes is exported
        nfs.export_share(config.get().shared_nfs_root, config.get().access_cidr)
    else:
        logger.info("Shared volume subsystem will be disabled.")
    
    if config.get().individual_volumes_enabled:
        logger.info("Starting individual volumes subsystem...")
    else:
        logger.info("Individual volume subsystem will be disabled.")
    
    storage_api = kubernetes.client.StorageV1Api()
    storage_classes = storage_api.list_storage_class()
    for sc in storage_classes.items:
        metadata = sc.metadata
        if sc.provisioner != config.get().provisioner_name:
            logger.debug(f"Ignoring storage class {metadata.name}...")
            continue

        validate_and_register_storage_class(metadata.name, sc.parameters["type"])


def get_storage_class_params(name):
    storage_api = kubernetes.client.StorageV1Api()
    storage_class = storage_api.read_storage_class(name)

    params = storage_class.parameters
    params["reclaim_policy"] = storage_class.reclaim_policy
    params["allow_volume_expansion"] = storage_class.allow_volume_expansion
    params["mount_options"] = storage_class.mount_options
    params["annotations"] = storage_class.metadata.annotations

    return params

def validate_pvc_spec(spec, meta, update=False):
    spec = dict(spec)
    storage_class_params = get_storage_class_params(spec["storageClassName"])
    if storage_class_params["type"] != VOLUME_TYPE_SHARED and ("ReadWriteMany" in spec["accessModes"] or "ReadOnlyMany" in spec["accessModes"]):
        raise kopf.PermanentError(f"LabDisk only supports ReadWriteMany/ReadOnlyMany volumes using the '{VOLUME_TYPE_SHARED}' disk type")
    
    if NODE_SELECTOR_ANNOTATION_KEY not in meta.annotations:
        raise kopf.PermanentError(f"No node was selected to store the volume. (PVC missing annotation '{NODE_SELECTOR_ANNOTATION_KEY}'")
    
    return storage_class_params

@kopf.on.create("persistentvolumeclaim")
def create_volume(meta, spec, **kwargs):
    storage_class_params = validate_pvc_spec(spec, meta)

    if spec["storageClassName"] not in registered_storage_classes:
        logger.info(f"Volume creation for storage class that we are not monitoring. ({spec['storageClassName']})")
        return

    volume_node = meta.annotations[NODE_SELECTOR_ANNOTATION_KEY]
    us = config.get().current_node_name
    if volume_node != us:
        logger.info(f"Volume creation not for this node. (Request: {volume_node}, Us: {us}")
        return

    desired_volume_size = spec["resources"].get("limits", {}).get("storage", spec["resources"].get("requests", {}).get("storage"))
    access_modes = spec["accessModes"]
    volume_type = storage_class_params["type"]
    pv_name = f"pvc-{meta.uid}"
    fs_type = meta.annotations.get(FILESYSTEM_ANNOTATION_KEY, "xfs")

    if not desired_volume_size:
        raise kopf.PermanentError("No volume size provided")

    if volume_type == VOLUME_TYPE_SHARED:
        if not config.get().shared_volumes_enabled:
            raise kopf.PermanentError("This instance of LabDisk does not have shared volumes configured")

        storage_path = meta.annotations.get(SHARED_STORAGE_PATH_ANNOTATION_KEY)
        if storage_path == None:
            raise kopf.PermanentError(f"No storage path provided for shared storage. (PVC missing annotation '{SHARED_STORAGE_PATH_ANNOTATION_KEY}'")

        # prevent misusing shared storage to gain access to other host paths
        if ".." in storage_path:
            raise kopf.PermanentError(f"Cannot use storage path outside of the shared NFS root! (storage path '{storage_path}')")

        volume_directory = f"{config.get().shared_nfs_root}/{storage_path}"
        util.run_process("mkdir", "-p", volume_directory)

        # create the pv object using the main nfs share and the subpath for this volume
        nfs.create_persistent_volume(pv_name, access_modes, desired_volume_size, config.get().current_node_ip, volume_directory, spec["storageClassName"], spec["volumeMode"])
    else:
        if not config.get().individual_volumes_enabled:
            raise kopf.PermanentError("This instance of LabDisk does not have individual volumes configured")      

        lvm_group = config.get().lvm_group

        if volume_type == VOLUME_TYPE_ISCSI:
            # todo: setup iscsi exports using rtstlib-fb
            # todo: create the pv object using the iscsi share info
            raise NotImplementedError()

        if volume_type == VOLUME_TYPE_NFS:
            mount_point = f"/srv/nfs/{pv_name}"

            # provision lvm volume then locally mount it where NFS can access it and the set up a NFS share
            lvm.create_volume(lvm_group, pv_name, fs_type, desired_volume_size, mount_point)

            # export the share
            nfs.export_share(mount_point, config.get().access_cidr)

            # create the pv object using the share we just exported
            nfs.create_persistent_volume(pv_name, access_modes, desired_volume_size, config.get().current_node_ip, mount_point, spec["storageClassName"], spec["volumeMode"])

    logger.info(f"Successfully provisioned volume for claim {meta.name}")

@kopf.on.update("persistentvolumeclaim")
def update_volume(spec, meta, old, new, diff, **kwargs):
    validate_pvc_spec(spec, meta, update=True)


@kopf.on.delete("persistentvolumeclaim")
def delete_volume(spec, **kwargs):
    pass
