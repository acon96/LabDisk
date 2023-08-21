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

def create_volume(pool_name, volume_name, fs_type, mirror_disk, volume_size, mount_point=None):
    if volume_exists(pool_name, volume_name):
        return

    formatted_volume_size = volume_size.replace("Ki", "K").replace("Mi", "M").replace("Gi", "G").lower()
    block_device = f"/dev/{pool_name}/{volume_name}"

    unroll = []

    try:
        create_cmd = [ "lvcreate", "--zero", "n", "--size", formatted_volume_size, "--name", volume_name, pool_name ]
        if mirror_disk:
            create_cmd.extend(config.Constants.LVM_RAID1_FLAGS)

        util.run_process(*create_cmd)
        unroll.append("lvcreate")

        # wait for the device to be created
        time.sleep(1.0)

        util.run_process(f"mkfs.{fs_type}", "-f", block_device)
        unroll.append("mkfs")

        if mount_point:
            os.makedirs(mount_point, exist_ok=True)
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

def resize_volume(pool_name, volume_name, volume_size, new_volume_size):
    formatted_volume_size = volume_size.replace("Ki", "K").replace("Mi", "M").replace("Gi", "G").lower()
    new_formatted_volume_size = new_volume_size.replace("Ki", "K").replace("Mi", "M").replace("Gi", "G").lower()
    block_device = f"/dev/{pool_name}/{volume_name}"

    def name_to_bytes(name):
        extracted = int(name[:-1])
        if "k" in name:
            return 1024 * extracted
        if "m" in name:
            return 1024 * 1024 * extracted
        if "g" in name:
            return 1024 * 1024 * 1024 * extracted

    try:
        report = json.loads(" ".join(util.run_process("vgs", "vg-kube", "--units", "b", "--reportformat", "json")))
    except Exception as ex:
        logger.warn("Failed to get remaining space!", exc_info=ex)
        raise kopf.TemporaryError(f"Failed to retrieve remaining space in the volume group: {repr(ex)}")

    remaining_bytes = int(report["report"]["vg"][0]["vg_free"][:-1])
    increased_bytes = name_to_bytes(new_formatted_volume_size) - name_to_bytes(formatted_volume_size)

    if increased_bytes < 0:
        raise kopf.PermenentError("The new volume size must be larger than the current volume size.")

    if increased_bytes > remaining_bytes:
        raise kopf.PermenentError(f"Cannot increase size of volume from {volume_size} to {new_volume_size}. There is insufficent disk space!")

    try:
        util.run_process("lvextend", "--size", new_formatted_volume_size, "--resizefs", block_device)
    except Exception as ex:
        logger.warn("Failed to resize the volume!", exc_info=ex)
        raise kopf.TemporaryError(f"Error resizing volume: {repr(ex)}")

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

def import_volume(pool_name, volume_name, mount_point=None):
    if volume_name is None and config.get().import_mode:
        raise kopf.TemporaryError(f"Cannot create volume because import mode is enabled!")
    
    if not volume_exists(pool_name, volume_name):
        raise kopf.PermanentError(f"Cannot find lvm volume to import named '{volume_name}'")
    
    if mount_point:
        unroll = []
        block_device = f"/dev/{pool_name}/{volume_name}"
        fs_type = util.run_process("blkid", "-o", "value", "-s", "TYPE", block_device)
        try:
            os.makedirs(mount_point, exist_ok=True)
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
    
    return volume_name