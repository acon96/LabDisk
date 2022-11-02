import os
import json
import logging
import asyncio
import kopf
import kubernetes

PROVISIONER = os.environ.get("PROVISIONER", "ragdollphysics.org/lab-disk")
NODE_SELECTOR_ANNOTATION_KEY = "ragdollphysics.org/disk-node"
VOLUME_TYPE_NFS = "nfs"
VOLUME_TYPE_ISCSI = "iscsi"
VOLUME_TYPE_SHARED = "shared-nfs"

kubernetes.config.load_kube_config()

logger = logging.getLogger(__name__)

@kopf.on.login()
def api_login(**kwargs):
    return kopf.login_with_service_account(**kwargs) or kopf.login_with_kubeconfig(**kwargs)

def register_pvc_handlers(storage_class_name):
    kopf.on.create("persistentvolumeclaim", field="spec.storageClassName", value=storage_class_name)(create_volume)
    kopf.on.update("persistentvolumeclaim", field="spec.storageClassName", value=storage_class_name)(update_volume)
    kopf.on.delete("persistentvolumeclaim", field="spec.storageClassName", value=storage_class_name)(delete_volume)

def validate_and_register_storage_class(logger, storage_class):
    name = storage_class.metadata.name
    if storage_class.parameters["type"].lower() not in [VOLUME_TYPE_ISCSI, VOLUME_TYPE_NFS]:
        logger.error(f"Unrecorgnized LabDisk volume type: {storage_class.parameters['type']}")
        return

    logger.info(f"Found valid LabDisk storage class {name}. Registering PVC Handlers.")
    register_pvc_handlers(name)

@kopf.on.startup()
async def operator_startup(logger, **kwargs):
    # todo: setup config in a configmap
    # load the configmap via kubernetes api
    # config defines:
    # - provisioner name
    # - lvm volume group to use
    # - base path for shared nfs volumes

    storage_api = kubernetes.client.StorageV1Api()
    storage_classes = storage_api.list_storage_class()

    # todo: setup and export main nfs share

    for sc in storage_classes.items:
        metadata = sc.metadata
        if sc.provisioner != PROVISIONER:
            logger.debug(f"Ignoring storage class {metadata.name}...")
            continue

        validate_and_register_storage_class(logger, sc)

@kopf.on.create("storageclass", field="provisioner", value=PROVISIONER)
def new_storageclass(spec, logger, **kwargs):
    validate_and_register_storage_class(logger, spec)


def get_storage_class_params(name):
    storage_api = kubernetes.client.StorageV1Api()
    storage_class = storage_api.read_storage_class(name)

    params = storage_class.parameters
    params["reclaim_policy"] = storage_class.reclaim_policy
    params["allow_volume_expansion"] = storage_class.allow_volume_expansion
    params["mount_options"] = storage_class.mount_options
    params["annotations"] = storage_class.metadata.annotations

    return params
        

def validate_pvc_spec(spec, update=False):
    spec = dict(spec)
    storage_class_params = get_storage_class_params(spec["storageClassName"])
    if storage_class_params["type"] != VOLUME_TYPE_SHARED and ("ReadWriteMany" in spec["accessModes"] or "ReadOnlyMany" in spec["accessModes"]):
        kopf.PermanentError(f"LabDisk only supports ReadWriteMany/ReadOnlyMany volumes using the '{VOLUME_TYPE_SHARED}' disk type")
    
    if NODE_SELECTOR_ANNOTATION_KEY not in storage_class_params["annotations"]:
        kopf.PermanentError(f"No node was selected to store the volume. (PVC missing annotation '{NODE_SELECTOR_ANNOTATION_KEY}'")
    
    return storage_class_params

def create_volume(meta, spec, logger, **kwargs):
    storage_class_params = validate_pvc_spec(spec)
    desired_volume_size = spec["resources"].get("limits", {}).get("storage", spec["resources"].get("requests", {}).get("storage"))
    volume_type = storage_class_params["type"]

    if not desired_volume_size:
        kopf.PermanentError("No volume size provided")

    if volume_type != VOLUME_TYPE_SHARED:
        pass # todo: provision lvm volume using lvm2py for individual volumes

    if volume_type == VOLUME_TYPE_ISCSI:
        # todo: setup iscsi exports using rtstlib-fb
        # todo: create the pv object using the iscsi share info
        pass 

    if volume_type == VOLUME_TYPE_SHARED:
        # todo: make sure the folder exists under the main nfs share
        # todo: create the pv object using the main nfs share and the subpath from the pvc spec
        pass

    if volume_type == VOLUME_TYPE_NFS:
        # todo: locally mount lvm volume where NFS can access it
        # todo: setup nfs share exports by writing to /etc/exports directly and running exportfs
        # todo: create the pv object using the specific NFS share
        pass

    logger.info(f"Successfully provisioned volume for claim {meta.name}")
    
def update_volume(spec, old, new, diff, **kwargs):
    validate_pvc_spec(spec, update=True)

def delete_volume(spec, **kwargs):
    pass
