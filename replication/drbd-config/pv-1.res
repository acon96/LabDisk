resource pv-1 {
  net {
    protocol C;
    allow-two-primaries yes;
  }

  startup {
    become-primary-on both;
  }

  device    /dev/drbd1;
  disk      /dev/drbdpool/pv-1;
  meta-disk internal;

  on k8s-worker-intel-01 {
      address   192.168.0.252:7788;
  }

  on k8s-worker-intel-02 {
      address   192.168.0.251:7788;
  }
}