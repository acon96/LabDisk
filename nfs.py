import logging
import re
import subprocess

import kubernetes

import config
import util

NFS_MOUNT_FLAGS = "rw,sync,no_subtree_check,insecure,no_root_squash"

logger = logging.getLogger(__name__)

def get_exported_filesystems():
    cur_node = config.get().current_node_ip
    logger.debug(f"Listing mounts on {cur_node}")
    mounts = util.run_process("showmount", "--no-headers", "-e", cur_node)

    result = []
    for line in mounts:
        found = re.findall(r"(.*?)[^\S]+(\d.*)", line)
        if len(found) == 1:
            result.append(found[0])
        else:
            raise RuntimeError(f"failed to parse mount: {line}")

    logger.debug(f"Got mounts: {result}")
    
    return result

def export_share(mount, client):
    if (mount, client) in get_exported_filesystems():
        return # share already mounted

    logger.info(f"Exporting fs '{mount}'")
    try:
        util.run_process("exportfs", "-o", NFS_MOUNT_FLAGS, f"{client}:{mount}")
    except subprocess.CalledProcessError as ex:
        # exportfs doesn't like us running from inside a container
        # check after we exported it to see if to happened or not and then error out then
        if ex.returncode == 1 and (mount, client) not in get_exported_filesystems():
            raise ex


def create_persistent_volume(pv_name, access_modes, desired_capacity, nfs_server, volume_path, sc_name, volume_mode):

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