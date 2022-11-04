import os
import logging
import kubernetes

logger = logging.getLogger(__name__)

class Config:
    def __init__(self):
        configmap_name = os.environ.get("LAB_DISK_CONFIGMAP")
        if not configmap_name:
            raise Exception("No config specified! Please set the 'LAB_DISK_CONFIGMAP' env variable.")

        self.namespace = os.environ.get("LAB_DISK_NAMESPACE", "kube-system")

        core_api = kubernetes.client.CoreV1Api()
        config = core_api.read_namespaced_config_map(name=configmap_name, namespace=self.namespace).data
    
        self.provisioner_name = config.get("provisioner")
        self.lvm_group = config.get("lvm_group")
        self.shared_nfs_root = config.get("shared_nfs_root")
        self.access_cidr = config.get("access_cidr", "0.0.0.0/0")

        self.current_node_ip = os.environ.get("LAB_DISK_NODE_IP")
        if not self.current_node_ip:
            raise Exception("No external node ip specified! Please set the 'LAB_DISK_NODE_IP' env variable.")

        self.current_node_name = os.environ.get("LAB_DISK_NODE_NAME", self.current_node_ip)

        self.shared_volumes_enabled = self.shared_nfs_root != None
        self.individual_volumes_enabled = self.lvm_group != None

        

# global config object
config = None

def setup(*args):
    global config
    config = Config(*args)

def get() -> Config:
    return config