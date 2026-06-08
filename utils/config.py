# utils/config.py

import yaml
import os
from dotenv import load_dotenv


def load_config():

    with open(
        "/opt/netbox-sync/config.yaml"
    ) as f:

        config = yaml.safe_load(f)

    load_dotenv(
        "/opt/netbox-sync/netbox-sync.env"
    )

    config["env"] = {

        "NETBOX_TOKEN":
            os.getenv("NETBOX_TOKEN"),

        "ZABBIX_TOKEN":
            os.getenv("ZABBIX_TOKEN"),

        "PROXMOX_TOKEN":
            os.getenv("PROXMOX_TOKEN"),

        "PROXMOX_USER":
            os.getenv("PROXMOX_USER"),

        "PBS_USER":
            os.getenv("PBS_USER"),

        "PBS_TOKEN":
            os.getenv("PBS_TOKEN")
    }

    return config