import os
import re
import math
import yaml
import requests
import urllib3
import pynetbox
from dotenv import load_dotenv
from datetime import datetime

urllib3.disable_warnings()

# =========================================================
# LOAD CONFIG
# =========================================================
try:
    with open("/opt/netbox-sync/config.yaml") as f:
        config = yaml.safe_load(f)
except Exception as e:
    print(f"Erro fatal ao carregar config.yaml: {e}")
    exit(1)

load_dotenv("/etc/netbox-sync.env")
NB_URL = config["netbox"]["url"]
NB_TOKEN = os.getenv("NETBOX_TOKEN","").strip()
ZABBIX_TOKEN = os.getenv("ZABBIX_TOKEN","").strip()
TRUENAS_TOKEN = os.getenv("TRUENAS_TOKEN","").strip()
PROXMOX_TOKEN = os.getenv("PROXMOX_TOKEN","").strip()
PROXMOX_USER = os.getenv("PROXMOX_USER","").strip()
PBS_USER = os.getenv("PBS_USER")
PBS_TOKEN = os.getenv("PBS_TOKEN","").strip()
NB_VERIFY = config["netbox"].get("validate_certs", True)
SYNC_DELETE = config["netbox"].get("sync_delete", False)

# =========================================================
# NETBOX
# =========================================================
nb = pynetbox.api(
    NB_URL,
    token=NB_TOKEN
)

nb.http_session.verify = False
nb.http_session.timeout = 20

# =========================================================
# CONFIG
# =========================================================
TRUENAS_HOSTS = [
    {
        "name": "TN-STORAGE-01",
        "host": "10.100.100.5",
        "site": "R0"
    }
]

# =========================================================
# LOG
# =========================================================
def log(msg):
    print(f"{datetime.now().isoformat()} - {msg}")

# =========================================================
# HELPERS
# =========================================================
def format_bytes(value):

    try:
        value = int(value)
    except:
        return "0 B"

    if value <= 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]

    size = float(value)

    for unit in units:

        if size < 1024:
            return f"{round(size, 2)} {unit}"

        size /= 1024

    return f"{round(size, 2)} PB"


def slugify(text):

    text = text.lower()
    text = re.sub(r'[^a-z0-9-_]+', '-', text)
    text = re.sub(r'-+', '-', text)

    return text.strip('-')


# =========================================================
# HTTP WRAPPER
# =========================================================
def truenas_get(host, endpoint):

    url = f"https://{host}/api/v2.0{endpoint}"

    headers = {
        "Authorization": f"Bearer {TRUENAS_TOKEN}"
    }

    r = requests.get(
        url,
        headers=headers,
        verify=False,
        timeout=30
    )

    r.raise_for_status()

    return r.json()


# =========================================================
# NETBOX HELPERS
# =========================================================
def get_or_create_manufacturer(name):

    obj = nb.dcim.manufacturers.get(name=name)

    if obj:
        return obj

    return nb.dcim.manufacturers.create({
        "name": name,
        "slug": slugify(name)
    })


def get_or_create_role(name):

    obj = nb.dcim.device_roles.get(name=name)

    if obj:
        return obj

    return nb.dcim.device_roles.create({
        "name": name,
        "slug": slugify(name),
        "color": "607d8b"
    })


def get_or_create_platform(name):

    obj = nb.dcim.platforms.get(name=name)

    if obj:
        return obj

    return nb.dcim.platforms.create({
        "name": name,
        "slug": slugify(name)
    })


def get_or_create_device_type(model, manufacturer):

    obj = nb.dcim.device_types.get(model=model)

    if obj:
        return obj

    return nb.dcim.device_types.create({
        "model": model,
        "slug": slugify(model),
        "manufacturer": manufacturer.id
    })


def get_or_create_tag(name, color="607d8b"):

    tag = nb.extras.tags.get(name=name)

    if tag:
        return tag

    return nb.extras.tags.create({
        "name": name,
        "slug": slugify(name),
        "color": color
    })


# =========================================================
# TRUE NAS COLLECTOR
# =========================================================
def collect_truenas(host):

    data = {}

    log(f"[TRUENAS] Coletando {host}")

    try:

        data["system"] = truenas_get(
            host,
            "/system/info"
        )

        data["pools"] = truenas_get(
            host,
            "/pool"
        )

        data["disks"] = truenas_get(
            host,
            "/disk"
        )

        data["alerts"] = truenas_get(
            host,
            "/alert/list"
        )

        data["interfaces"] = truenas_get(
            host,
            "/interface"
        )

        data["smb"] = truenas_get(
            host,
            "/sharing/smb"
        )

        data["nfs"] = truenas_get(
            host,
            "/sharing/nfs"
        )

        return data

    except Exception as e:

        log(f"[TRUENAS ERROR] {host} -> {e}")

        return None


# =========================================================
# DISK HEALTH
# =========================================================
def evaluate_disk_health(disks):

    failed = []
    healthy = []

    for disk in disks:

        name = disk.get("name")

        if not name:
            continue

        smart = disk.get("smartoptions")
        expired = disk.get("expiretime")

        if expired:
            failed.append(name)
        else:
            healthy.append(name)

    return healthy, failed


# =========================================================
# POOL HEALTH
# =========================================================
def evaluate_pool_health(pools):

    online = []
    degraded = []

    total_size = 0
    total_allocated = 0

    for pool in pools:

        name = pool.get("name")
        status = pool.get("status")

        size = pool.get("size", 0)
        allocated = pool.get("allocated", 0)

        if isinstance(size, dict):
            size = size.get("parsed", 0)

        if isinstance(allocated, dict):
            allocated = allocated.get("parsed", 0)

        try:
            size = int(size)
        except:
            size = 0

        try:
            allocated = int(allocated)
        except:
            allocated = 0

        total_size += size
        total_allocated += allocated

        if status == "ONLINE":
            online.append(name)
        else:
            degraded.append(name)

    return {
        "online": online,
        "degraded": degraded,
        "total_size": total_size,
        "total_allocated": total_allocated
    }


# =========================================================
# CREATE INTERFACES
# =========================================================
def sync_interfaces(device, interfaces):

    for iface in interfaces:

        name = iface.get("name")

        if not name:
            continue

        nb_iface = nb.dcim.interfaces.get(
            device_id=device.id,
            name=name
        )

        if not nb_iface:

            nb_iface = nb.dcim.interfaces.create({
                "device": device.id,
                "name": name,
                "type": "1000base-t"
            })

            log(f"[INTERFACE CREATED] {device.name} {name}")


# =========================================================
# SMB SERVICES
# =========================================================
def sync_smb_services(device, smb_shares):

    for share in smb_shares:

        name = share.get("name")

        if not name:
            continue

        existing = nb.ipam.services.get(
            parent_object_id=device.id,
            name=name
        )

        if existing:
            continue

        try:

            nb.ipam.services.create({
                "name": name,
                "protocol": "tcp",
                "ports": [445],
                "parent_object_type": "dcim.device",
                "parent_object_id": device.id
            })

            log(f"[SMB SERVICE] {name}")

        except Exception as e:

            log(f"[SMB ERROR] {name} -> {e}")


# =========================================================
# NFS SERVICES
# =========================================================
def sync_nfs_services(device, nfs_shares):

    for share in nfs_shares:

        paths = share.get("paths", [])

        for path in paths:

            name = f"NFS-{path}"

            existing = nb.ipam.services.get(
                device_id=device.id,
                name=name
            )

            if existing:
                continue

            try:

                nb.ipam.services.create({
                    "name": name,
                    "protocol": "tcp",
                    "ports": [2049],
                    "parent_object_type": "dcim.device",
                    "parent_object_id": device.id
                 })

                log(f"[NFS SERVICE] {name}")

            except Exception as e:

                log(f"[NFS ERROR] {name} -> {e}")


# =========================================================
# MAIN SYNC
# =========================================================
def sync_truenas(host_conf):

    host = host_conf["host"]
    name = host_conf["name"]
    site_name = host_conf["site"]

    data = collect_truenas(host)

    if not data:
        return

    site = nb.dcim.sites.get(name=site_name)

    if not site:
        raise Exception(f"Site '{site_name}' não encontrado")

    manufacturer = get_or_create_manufacturer(
        "iXsystems"
    )

    role = get_or_create_role(
        "Storage"
    )

    platform = get_or_create_platform(
        "TrueNAS CORE"
    )

    dtype = get_or_create_device_type(
        "TrueNAS CORE",
        manufacturer
    )

    device = nb.dcim.devices.get(name=name)

    if not device:

        device = nb.dcim.devices.create({
            "name": name,
            "site": site.id,
            "role": role.id,
            "device_type": dtype.id,
            "platform": platform.id,
            "status": "active"
        })
      

        log(f"[DEVICE CREATED] {name}")

    # =====================================================
    # POOL ANALYSIS
    # =====================================================
    pool_info = evaluate_pool_health(
        data.get("pools", [])
    )

    # =====================================================
    # DISK ANALYSIS
    # =====================================================
    healthy_disks, failed_disks = evaluate_disk_health(
        data.get("disks", [])
    )

    # =====================================================
    # ALERTS
    # =====================================================
    alerts = data.get("alerts") or []

    critical_alerts = []

    for alert in alerts:

        if alert.get("level") == "CRITICAL":
            critical_alerts.append(
                alert.get("formatted")
            )

    # =====================================================
    # TAGS
    # =====================================================
    tags = []

    if failed_disks:

        tag = get_or_create_tag(
            "storage-critical",
            "f44336"
        )

        tags.append(tag.id)

    elif pool_info["degraded"]:

        tag = get_or_create_tag(
            "storage-warning",
            "ff9800"
        )

        tags.append(tag.id)

    else:

        tag = get_or_create_tag(
            "storage-ok",
            "4caf50"
        )

        tags.append(tag.id)

    # =====================================================
    # USAGE
    # =====================================================
    usage_percent = 0

    if pool_info["total_size"] > 0:

        usage_percent = round(
            (
                pool_info["total_allocated"]
                / pool_info["total_size"]
            ) * 100,
            2
        )

    # =====================================================
    # CUSTOM FIELDS
    # =====================================================
    cf = dict(device.custom_fields)

    cf.update({
        "storage_total": format_bytes(
            pool_info["total_size"]
        ),
        "storage_used": format_bytes(
            pool_info["total_allocated"]
        ),
        "storage_usage_percent": usage_percent,
        "zfs_pools_ok": ",".join(
            pool_info["online"]
        ),
        "zfs_pools_problem": ",".join(
            pool_info["degraded"]
        ),
        "healthy_disks": len(healthy_disks),
        "failed_disks": len(failed_disks),
        "critical_alerts": len(critical_alerts)
    })

    # =====================================================
    # UPDATE DEVICE
    # =====================================================
    device.update({
        "custom_fields": cf,
        "tags": tags
    })

    # =====================================================
    # SYNC INTERFACES
    # =====================================================
    sync_interfaces(
        device,
        data.get("interfaces", [])
    )

    # =====================================================
    # SMB
    # =====================================================
    sync_smb_services(
        device,
        data.get("smb", [])
    )

    # =====================================================
    # NFS
    # =====================================================
    sync_nfs_services(
        device,
        data.get("nfs", [])
    )

    log(f"[SYNC OK] {name}")

# =========================================================
# CUSTOM FIELD ENGINE
# =========================================================
def ensure_custom_field(
    name,
    field_type="text",
    label=None,
    description=""
):

    cf = nb.extras.custom_fields.get(
        name=name
    )

    if cf:
        return cf

    payload = {
        "name": name,
        "type": field_type,
        "label": label or name,
        "description": description,
        "object_types": [
            "dcim.device"
        ]
    }

    cf = nb.extras.custom_fields.create(
        payload
    )

    log(
        f"[CUSTOM FIELD CREATED] "
        f"{name}"
    )

    return cf

# =========================================================
# TRUE NAS CUSTOM FIELDS
# =========================================================
def ensure_truenas_custom_fields():

    ensure_custom_field(
        "storage_total",
        "text",
        "Storage Total"
    )

    ensure_custom_field(
        "storage_used",
        "text",
        "Storage Used"
    )

    ensure_custom_field(
        "storage_usage_percent",
        "decimal",
        "Storage Usage Percent"
    )

    ensure_custom_field(
        "zfs_pools_ok",
        "text",
        "Healthy Pools"
    )

    ensure_custom_field(
        "zfs_pools_problem",
        "text",
        "Problem Pools"
    )

    ensure_custom_field(
        "healthy_disks",
        "integer",
        "Healthy Disks"
    )

    ensure_custom_field(
        "failed_disks",
        "integer",
        "Failed Disks"
    )

    ensure_custom_field(
        "critical_alerts",
        "integer",
        "Critical Alerts"
    )
# =========================================================
# EXECUTION
# =========================================================
if __name__ == "__main__":

    log("Iniciando sincronismo TrueNAS")
    
    ensure_truenas_custom_fields()    

    for host in TRUENAS_HOSTS:

        try:

            sync_truenas(host)

        except Exception as e:

            log(
                f"[FATAL ERROR] "
                f"{host['name']} -> {e}"
            )

    log("Sincronismo finalizado")
