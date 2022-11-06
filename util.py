import subprocess
import logging
from kubernetes import client, config

logger = logging.getLogger(__name__)

def run_process(*args):
    process = subprocess.run(args, 
        stdout=subprocess.PIPE, 
        universal_newlines=True,
        check=True
    )

    return process.stdout.splitlines()

def setup_kube_client():
    try:
        config.load_kube_config()
        
    except:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token", "r") as f:
            bearer_token = f.read().strip()

        configuration = client.Configuration()
        configuration.api_key["authorization"] = bearer_token
        configuration.api_key_prefix['authorization'] = "Bearer"
        configuration.host = "https://kubernetes.default.svc.cluster.local"
        configuration.verify_ssl = False

        client.Configuration.set_default(configuration)