import logging

import config
import util

NFS_MOUNT_FLAGS = "rw,sync,no_subtree_check,insecure,no_root_squash"
EXPORTS_DIR = "/app/exports.d"

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

def export_share(name, mount, client):
    if (mount, client) in get_exported_filesystems():
        return # share already mounted

    logger.info(f"Exporting fs '{mount}'")

    with open(f"{EXPORTS_DIR}/{name}", "w+") as f:
        f.write(f"{mount} {client}({NFS_MOUNT_FLAGS})\n")

    mounts = util.run_process(["exportfs", "-a"])
