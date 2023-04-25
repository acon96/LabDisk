# LabDisk

LabDisk is a Kubernetes dynamic storage provisioner that provides automatic provisioning of NFS and iSCIS volumes backed by an LVM pool.

## Installation

1. Install the pre-requisites on every node in the Kubernetes cluster.
    - nfs-common
    - nfs-kernel-server
    - open-iscsi

2. Set an initiator name for every node if using iSCSI in `/etc/iscsi/initiatorname.iscsi`
```
InitiatorName=iqn.2003-01.org.linux-iscsi.ragdollphysics:<kubernetes node name>
```

3. Modify the configmap to match your deployed hardware config
Options:
    - provisioner: the name of the provisioner to match; defined in the PVC
    - lvm_group: the name of the LVM Volume Group (VG) to provision kubernetes Volumes in
    - nfs_access_cidr: the IP range to allow NFS access from. Should match the CIDR of your nodes (default: 0.0.0.0/0)
    - iscsi_portal_addr: the interface and port to export the iSCSI volumes on. (default: 0.0.0.0:3260)
    - allow_destructive_actions: this software is still experimental. enabling this flag will allow it to perform destructive disk actions. USE AT YOUR OWN RISK

4. Install the app from the manifests. Currently installs into the kube-system namespace.

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
    ragdollphysics.org/disk-node: k8s-dev
spec:
  storageClassName: lab-disk-iscsi
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Mi
```

# Todo
[ ] Implement CHAP authentication for iSCIS disks. Auto generate passwords if not provided