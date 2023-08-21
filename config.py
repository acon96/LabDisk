import os
import sys
import logging
import kubernetes

logger = logging.getLogger(__name__)

class Constants:
    PERSISTENCE_ANNOTATION_KEY_PREFIX = "ragdollphysics.org"
    NODE_SELECTOR_ANNOTATION_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/disk-node"
    SHARED_STORAGE_PATH_ANNOTATION_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/shared-storage-path"
    FILESYSTEM_ANNOTATION_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/filesystem"
    MIRROR_ANNOTATION_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/mirror"
    PVC_FINALIZER_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/disk-finalizer"
    PV_ASSIGNED_NODE_ANNOTATION_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/lab-disk-node"
    IMPORTED_LVM_NAME_ANNOTATION_KEY = f"{PERSISTENCE_ANNOTATION_KEY_PREFIX}/lvm-disk-to-import"

    VOLUME_TYPE_NFS = "nfs"
    VOLUME_TYPE_ISCSI = "iscsi"
    VOLUME_TYPE_SHARED = "shared-nfs"

    NFS_MOUNT_FLAGS = "rw,sync,no_subtree_check,insecure,no_root_squash"

    # support more raid modes?
    LVM_RAID1_FLAGS = [ "--type", "raid1", "--mirrors", "1", "--nosync" ]

class Config:
    def __init__(self):
        configmap_name = os.environ.get("LAB_DISK_CONFIGMAP")
        if not configmap_name:
            raise Exception("No config specified! Please set the 'LAB_DISK_CONFIGMAP' env variable.")

        self.namespace = os.environ.get("LAB_DISK_NAMESPACE", "kube-system")

        core_api = kubernetes.client.CoreV1Api()
        config = core_api.read_namespaced_config_map(name=configmap_name, namespace=self.namespace).data
    
        self.provisioner_name = config.get("provisioner")

        if not self.provisioner_name:
            raise Exception("No proviioner name provided for this instance!")

        self.lvm_group = config.get("lvm_group")
        self.shared_nfs_root = config.get("shared_nfs_root")
        self.shared_nfs_nodes = config.get("shared_nfs_nodes")
        self.nfs_access_cidr = config.get("nfs_access_cidr", "0.0.0.0/0")
        self.iscsi_portal_addr = config.get("iscsi_portal_addr", "0.0.0.0:3260")
        self.iscsi_portal_port = self.iscsi_portal_addr.split(":")[1]

        self.current_node_ip = os.environ.get("LAB_DISK_NODE_IP")
        if not self.current_node_ip:
            raise Exception("No external node ip specified! Please set the 'LAB_DISK_NODE_IP' env variable.")

        self.current_node_name = os.environ.get("LAB_DISK_NODE_NAME", self.current_node_ip)

        self.shared_volumes_enabled = (self.shared_nfs_root != None and self.shared_nfs_nodes != None and self.current_node_name in self.shared_nfs_nodes.split(","))
        self.individual_volumes_enabled = self.lvm_group != None

        logger.info(f"Shared Volumes Enabled: {self.shared_volumes_enabled}")
        logger.info(f"Individual Volumes Enabled: {self.individual_volumes_enabled}")

        self.allow_destructive_actions = config.get("allow_destructive_actions", "false").lower() == "true"

        if self.allow_destructive_actions:
            logger.info("-------- WARNING --------")
            logger.info("Destructive actions are turned ON. This means that the application can potentially delete/destroy data on your disks.")
            logger.info("USE AT YOUR OWN RISK")

        self.import_mode = False
        if os.environ.get("LAB_DISK_IMPORT_MODE", "false").lower() == "true":
            logger.info("LabDisk is in 'Import' mode!")
            logger.info("It will not create any new LVM disks and will only match up new PVCs with existing disks")
            self.import_mode = True


# global config object
config = None

def setup(*args):
    global config
    config = Config(*args)

def get() -> Config:
    return config
