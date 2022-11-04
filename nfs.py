import logging

import config
import util

NFS_MOUNT_FLAGS = "rw,sync,no_subtree_check,insecure,no_root_squash"

logger = logging.getLogger(__name__)

def get_exported_filesystems():
    cur_node = config.get().current_node_ip
    logger.debug(f"Listing mounts on {cur_node}")
    mounts = util.run_process(["showmount", "--no-headers", "-e", cur_node])

    logger.debug(f"Got mounts: {mounts}")

    result = []
    for line in mounts:
        split = line.split(" ")
        if len(split) > 1:
            result.append((split[0], split[1]))
    
    return result

def export_share(mount, client):
    if (mount, client) in get_exported_filesystems():
        return # share already mounted

    logger.info(f"Exporting fs '{mount}'")
    util.run_process(["exportfs", "-o", NFS_MOUNT_FLAGS, f"{client}:{mount}"])

# TODO: add an api so that we can call this code in another pod that is running with host networking