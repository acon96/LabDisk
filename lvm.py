import logging
import time
import json
import os
from contextlib import suppress

import util
import config
import kopf

logger = logging.getLogger(__name__)

def volume_exists(pool_name, volume_name):
    lines = util.run_process("lvs", "--reportformat", "json")
    report = json.loads("".join(lines))

    for entry in report["report"][0]["lv"]:
        if entry["vg_name"] == pool_name and entry["lv_name"] == volume_name:
            return True

    return False

def create_volume(pool_name, volume_name, fs_type, volume_size, mount_point=None):
    if volume_exists(pool_name, volume_name):
        return

    formatted_volume_size = volume_size.replace("Ki", "K").replace("Mi", "M").replace("Gi", "G").lower()
    block_device = f"/dev/{pool_name}/{volume_name}"

    unroll = []

    try:
        util.run_process("lvcreate", "-Z", "n", "-L", formatted_volume_size, "-n", volume_name, pool_name)
        unroll.append("lvcreate")

        # wait for the device to be created
        time.sleep(1.0)

        util.run_process(f"mkfs.{fs_type}", "-f", block_device)
        unroll.append("mkfs")

        if mount_point:
            with suppress(FileExistsError):
                os.mkdir(mount_point)
            unroll.append("mkdir")

            util.run_process("mount", "-t", fs_type, block_device, mount_point)
            unroll.append("mount")

            # save our mount
            options = f"defaults,noatime"
            with open("/app/hostetc/fstab", "a") as f:
                f.write(f"{block_device} {mount_point} {fs_type} {options} 0 0\n") # dump and fsck disabled

    except Exception as ex:
        try:
            if "mount" in unroll:
                util.run_process("umount", mount_point)
            
            if "mkdir" in unroll:
                os.rmdir(mount_point)

            if "lvcreate" in unroll and config.get().allow_destructive_actions:
                util.run_process("lvremove", f"{pool_name}/{volume_name}", "--yes")
        except Exception as ex2:
            msg = "Fatal Error encountered unrolling volume creation. Disk will be left in a intermediate state!"
            logger.error(msg, exc_info=ex2)
            raise kopf.PermanentError(msg)

        logger.warn("Failed to create volume!", exc_info=ex)
        raise kopf.TemporaryError(f"Error creating volume: {repr(ex)}")


def unmount_volume(mount_point, pool_name, volume_name):
    # unmount right now
    try:
        util.run_process("umount", mount_point)
    except:
        pass

    # clean up mount point
    try:
        os.rmdir(mount_point)
    except:
        pass
    
    # remove any entires in fstab
    block_device = f"/dev/{pool_name}/{volume_name}"

    with open("/app/hostetc/fstab", "r") as f:
        lines = f.readlines()

    with open("/app/hostetc/fstab", "w") as f:
        for line in lines:
            if not line.lstrip().startswith(block_device):
                f.write(line)

def delete_volume(pool_name, volume_name):
    if config.get().allow_destructive_actions:
        util.run_process("lvremove", f"{pool_name}/{volume_name}", "--yes")
