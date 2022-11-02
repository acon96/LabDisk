# LabDisk

None of the storage solutions are any good. They all require at least 3 replicas and are a total pain in the ass to configure. I just want a way to have n replicas (1 hot the rest cold/warm) that can be shared among pods, run on hard drives, is easy to configure, and has volume size limit enforcement.

## Notes
https://github.com/fghaas/drbd-documentation/blob/master/users-guide/configure.txt
https://mental.me.uk/viewtopic.php?t=463
http://rdstash.blogspot.com/2012/11/high-availability-storage-using-drbd.html
https://www.shapeblue.com/installing-and-configuring-an-ocfs2-clustered-file-system/
http://www.voleg.info/stretch-nfs-cluster-centos-drbd-gfs2.html


## Architecture
OCFS2 on top of DRDB being shared using NFS
One NFS server pod per node.
One DRDB resource per volume.


## Installation requirements
- `apt install drbd-utils ocfs2-tools`
- `modprobe drbd`
- `modprobe dm-mod`
- `sudo bash -c "echo drbd >> /etc/modules"`
- `sudo bash -c "echo dm-mod >> /etc/modules"`
- modify drbd global config
- enable loading ocfs2 on boot
- create ocfs2 cluster file
- `systemctl enable drbd ocfs2 o2cb`
- `systemctl restart drbd ocfs2 o2cb`

## Steps for setting up the volume group
1. Wipe any partitions and create a new one with the LVM type
    - `sudo fdisk /dev/<dev>`
        - delete all parititions using `d`
        - create a new parition with all the default values using `n`
        - change the type to LVM using `t` (it's usually 30)
2. `sudo pvcreate /dev/<dev>1`
3. `sudo vgcreate drbdpool /dev/<dev>1`

## Adding additional drives to a volume group
1. Wipe any partitions and create a new one with the LVM type
    - `sudo fdisk /dev/<dev>`
        - delete all parititions using `d`
        - create a new parition with all the default values using `n`
        - change the type to LVM using `t` (it's usually 30)
2. `sudo pvcreate /dev/<dev>1`
3. `sudo vgextend drbd /dev/<dev>1`

## Steps for creating a volume
1. create the logical volume to back the drbd device
    - `sudo lvcreate -L100M -npv-3 drbdpool`
2. create drbd resource file
3. create drbd metadata
    - `sudo drbdadm create-md pv-3`
4. bring up drbd resource
    - `sudo drbdadm up pv-3`
5. mark volume as synced
    - `sudo drbdadm -- --clear-bitmap new-current-uuid pv-3`
6. mark all replicas as primary
    - `sudo drbdadm primary pv-3`
6. create fs
    - `sudo mkfs.ocfs2 -L "pv-3" /dev/drbd3`
7. mount fs
    - `sudo mkdir /srv/nfs/pv-3`
    - `sudo mount -t ocfs2 /dev/drbd3 /srv/nfs/pv-3`

## Steps for growing a volume
1. Extend the logical volume
    - `sudo lvextend -L+500M /dev/drbdpool/pv-3`
2. Unmount and set one node to secondary
    - `sudo umount /srv/nfs/pv-3`
    - `sudo drbdadm secondary pv-3`

3. Resize the drbd resource
    - `sudo drbdadm resize pv-3`
4. Put 