resource pv-2 {
  net {
    protocol C;
    allow-two-primaries yes;
  }

  startup {
    become-primary-on both;
  }

  device    /dev/drbd2;
  disk      /dev/drbdpool/pv-2;
  meta-disk internal;

  on k8s-worker-intel-01 {
      address   192.168.0.252:7799;
  }

  on k8s-worker-intel-02 {
      address   192.168.0.251:7799;
  }
}