import logging, uuid
import kopf
import kubernetes

import config
import util
import nfs

NODE_SELECTOR_ANNOTATION_KEY = "ragdollphysics.org/disk-node"
SHARED_STORAGE_PATH_ANNOTATION_KEY = "ragdollphysics.org/shared-storage-path"
VOLUME_TYPE_NFS = "nfs"
VOLUME_TYPE_ISCSI = "iscsi"
VOLUME_TYPE_SHARED = "shared-nfs"

util.setup_kube_client()
logger = logging.getLogger("handler")

registered_storage_classes = []

@kopf.on.login()
def api_login(**kwargs):
    return kopf.login_via_client(**kwargs)

def validate_and_register_storage_class(name, type):
    if type.lower() not in [VOLUME_TYPE_ISCSI, VOLUME_TYPE_NFS, VOLUME_TYPE_SHARED]:
        logger.error(f"Unrecognized LabDisk volume type: {type.lower()}")
        return

    logger.info(f"Found valid LabDisk storage class {name}. Registering PVC Handlers.")
    registered_storage_classes.append(name)

def new_storageclass(name, **kwargs):
    storage_api = kubernetes.client.StorageV1Api()
    sc = storage_api.read_storage_class(name)

    validate_and_register_storage_class(name, sc.parameters['type'])

@kopf.on.startup()
async def operator_startup(**kwargs):
    config.setup()

    if config.get().shared_volumes_enabled:
        logger.info("Starting shared NFS export...")
        # make sure the main nfs share that backs shared volumes is exported
        nfs.export_share(config.get().shared_nfs_root, config.get().access_cidr)
    
    storage_api = kubernetes.client.StorageV1Api()
    storage_classes = storage_api.list_storage_class()
    for sc in storage_classes.items:
        metadata = sc.metadata
        if sc.provisioner != config.get().provisioner_name:
            logger.debug(f"Ignoring storage class {metadata.name}...")
            continue

        validate_and_register_storage_class(metadata.name, sc.parameters["type"])

    # register handler for new storageclasses
    kopf.on.create("storageclass", field="provisioner", value=config.get().provisioner_name)(new_storageclass)


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

def create_persistent_volume_nfs(pv_name, access_modes, desired_capacity, nfs_server, volume_path, sc_name, volume_mode):

    pv = {
        "accessModes": access_modes,
        "capacity": {"storage": desired_capacity},
        # "mount_options": None,
        "nfs": {
            "path": volume_path,
            "readOnly": False,
            "server": nfs_server
        },
        "storageClassName": sc_name,
        "volumeMode": volume_mode,
    }

    body = kubernetes.client.V1PersistentVolume(api_version='v1', spec=pv,
        metadata=kubernetes.client.V1ObjectMeta(name=pv_name, labels={"app": "storage", "component": "lab-disk"}), 
        kind="PersistentVolume"
    )

    core_api = kubernetes.client.CoreV1Api()
    core_api.create_persistent_volume(body)


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
        util.run_process(["mkdir", "-p", volume_directory])

        # todo: create the pv object using the main nfs share and the subpath for this volume
        pv_name = f"pvc-{uuid.uuid4()}"
        create_persistent_volume_nfs(pv_name, access_modes, desired_volume_size, config.get().current_node_ip, volume_directory, spec["storageClassName"], spec["volumeMode"])
    else:
        if not config.get().individual_volumes_enabled:
            raise kopf.PermanentError("This instance of LabDisk does not have individual volumes configured")

        # todo: provision lvm volume using lvm2py for individual volumes

        if volume_type == VOLUME_TYPE_ISCSI:
            # todo: setup iscsi exports using rtstlib-fb
            # todo: create the pv object using the iscsi share info
            raise NotImplementedError()

        if volume_type == VOLUME_TYPE_NFS:
            # todo: locally mount lvm volume where NFS can access it
            # todo: setup nfs share exports by writing to /etc/exports directly and running exportfs
            # todo: create the pv object using the specific NFS share
            raise NotImplementedError()

    logger.info(f"Successfully provisioned volume for claim {meta.name}")

@kopf.on.update("persistentvolumeclaim")
def update_volume(spec, meta, old, new, diff, **kwargs):
    validate_pvc_spec(spec, meta, update=True)


@kopf.on.delete("persistentvolumeclaim")
def delete_volume(spec, **kwargs):
    pass
