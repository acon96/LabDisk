resource pv-3 {
  net {
    protocol C;
    allow-two-primaries yes;
  }

  startup {
    become-primary-on both;
  }

  device    /dev/drbd3;
  disk      /dev/drbdpool/pv-3;
  meta-disk internal;

  on k8s-worker-intel-01 {
      address   192.168.0.252:7800;
  }

  on k8s-master {
      address   192.168.0.254:7800;
  }
}