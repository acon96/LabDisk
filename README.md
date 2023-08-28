# LabDisk

LabDisk is a Kubernetes dynamic storage provisioner that provides automatic provisioning of NFS and iSCIS volumes backed by an LVM pool.

The goal of this project is to provide a persistent volume provisioner for Kubernetes that can be run in a "lab" or "homelab" environment. Persistent Storage for Kubernetes is difficult to set up if you are not using a cloud distribution or running a CSI package like Longhorn or OpenEBS.

## Installation
This install guide assumes Ubuntu 22.04.

1. Install the pre-requisites on every node in the Kubernetes cluster.
    - nfs-common
    - nfs-kernel-server
    - open-iscsi

Install the pre-requisites on every node that will store data.
    - lvm2

2. Set an initiator name for every node if using iSCSI in `/etc/iscsi/initiatorname.iscsi`
```
InitiatorName=iqn.2003-01.org.linux-iscsi.ragdollphysics:<kubernetes node name>
```

3. Create the LVM pool(s) to store the persistent volumes.  
This should be done on each node that will be used for storage on your cluster.  
a. Locate the drives you whish to use using `lsblk` and note their path (ex: /dev/sda); THESE WILL BE WIPED WHEN SETTING UP THE LVM POOL!  
a. For each drive run: `pvcreate <drive_path>` to set up the drive for LVM2  
c. Run `vgcreate <volume_group_name> <drive_path1> <drive_path2> ...` to initialize the pool.  

4. Modify the configmap to match your deployed hardware config
Options:
    - provisioner: the name of the provisioner to match; defined in the PVC
    - lvm_group: the name of the LVM Volume Group (VG) to provision kubernetes Volumes in
    - nfs_access_cidr: the IP range to allow NFS access from. Should match the CIDR of your nodes (default: 0.0.0.0/0)
    - iscsi_portal_addr: the interface and port to export the iSCSI volumes on. (default: 0.0.0.0:3260)
    - allow_destructive_actions: this software is still experimental. enabling this flag will allow it to perform destructive disk actions. USE AT YOUR OWN RISK

5. Install the app from the manifests. Currently installs into the kube-system namespace.

```
kubectl apply -f manifests/
```

5. Provision your first volume!
```
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: lab-disk-iscsi
provisioner: ragdollphysics.org/lab-disk
parameters:
  type: iscsi
reclaimPolicy: Retain
allowVolumeExpansion: true
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-iscsi
  namespace: default
  labels:
    app: test
  annotations:
    "ragdollphysics.org/disk-node": k8s-dev
spec:
  storageClassName: lab-disk-iscsi
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Mi
```

6. (Optional) Import an existing LVM Volume:  
a. Set the `LAB_DISK_IMPORT_MODE` environment variable to "true" and restart LabDisk  
b. Create the template below. The PV should be created that matches the name of the existing LVM volume  
c. Undo the environment variable change and restart LabDisk again  
```
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-iscsi
  namespace: default
  labels:
    app: test
  annotations:
    "ragdollphysics.org/lvm-disk-to-import": "some-existing-lv"
    "ragdollphysics.org/disk-node": k8s-dev
spec:
  storageClassName: lab-disk-iscsi
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Mi
```

## Todo
[ ] Implement CHAP authentication for iSCIS disks. Auto generate passwords if not provided  
[ ] Research and plan out data replication  


## Version History
| Version | Description                                                               |
| ------- | ------------------------------------------------------------------------- |
| v0.2    | Add support for resizing disks, importing disks, & multi-node deployments |
| v0.1    | Initial Release                                                           |