import yaml
import pynetbox
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv # Adicione esta linha
import urllib3
import requests
import re
import os
import time
import socket
import csv
import math
from zoneinfo import ZoneInfo
from datetime import datetime
from statistics import mean
from concurrent.futures import ThreadPoolExecutor, as_completed

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


urllib3.disable_warnings()

proxmox_vm_keys = set()
pbs_backup_map = {}  

# =========================================================
# LOAD CONFIG
# =========================================================
try:
    with open("/opt/netbox-sync/config.yaml") as f:
        config = yaml.safe_load(f)
except Exception as e:
    print(f"Erro fatal ao carregar config.yaml: {e}")
    exit(1)

load_dotenv("/opt/netbox-sync/netbox-sync.env")
NB_URL = config["netbox"]["url"]
NB_TOKEN = os.getenv("NETBOX_TOKEN")
ZABBIX_TOKEN = os.getenv("ZABBIX_TOKEN")
PROXMOX_TOKEN = os.getenv("PROXMOX_TOKEN")
PROXMOX_USER = os.getenv("PROXMOX_USER")
PBS_USER = os.getenv("PBS_USER")
PBS_TOKEN = os.getenv("PBS_TOKEN")
NB_VERIFY = config["netbox"].get("validate_certs", True)
SYNC_DELETE = config["netbox"].get("sync_delete", False)

# Adicione isso logo após os os.getenv
if not NB_TOKEN:
    print("ERRO CRÍTICO: NETBOX_TOKEN não encontrado")
    exit(1)

nb = pynetbox.api(NB_URL, token=NB_TOKEN)
nb.http_session.verify = NB_VERIFY
nb.http_session.timeout = 10

def log(msg):
    print(f"{datetime.now().isoformat()} - {msg}")

def format_size(size_bytes):
    if size_bytes is None or size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

# =========================================================
# RETRY ENGINE
# =========================================================
def retry(func, retries=3, delay=2):
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (2 ** attempt))
            else:
                log(f"[ERROR] {e}")
                return None

# =========================================================
# CONNECT PROXMOX
# =========================================================
def connect_proxmox(px_conf):
    # px_conf aqui é o dicionário do host vindo do config.yaml
    # Tenta pegar o token e user específicos do host, ou usa o global do .env
    token_v = px_conf.get("token_value") or os.getenv("PROXMOX_TOKEN")
    user_v = px_conf.get("token_user") or os.getenv("PROXMOX_USER")

    if not user_v or "!" not in user_v:
        log(f"[ERROR] Usuário do Proxmox inválido para o host {px_conf.get('host')}")
        return None

    # Garante que user_v seja tratado como string antes do split
    user_str = str(user_v)
    user_part, token_name = user_str.split("!")
    
    return ProxmoxAPI(
        px_conf["host"],
        user=user_part,
        token_name=token_name,
        token_value=token_v,
        verify_ssl=px_conf.get("validate_certs", False),
        timeout=60
    )


def is_reachable(ip, port=8006):
    try:
        socket.create_connection((ip, port), timeout=2)
        return True
    except:
        return False

# =========================================================
# ZABBIX CACHE POR HOSTNAME
# =========================================================
zabbix_cache = {}

def load_zabbix_cache():

    log(f"[ZABBIX CONFIG] {config.get('zabbix')}")

    if not ZABBIX_TOKEN:
        log("[ZABBIX] Token não definido")
        return

    for zbx in config.get("zabbix", []):

        zabbix_url = f"http://{zbx['host']}/zabbix/api_jsonrpc.php"

        # ================================
        # HOST.GET
        # ================================
        payload = {
            "jsonrpc": "2.0",
            "method": "host.get",
            "params": {
                "output": "extend", 
                "selectInterfaces": "extend"
            },
            "auth": ZABBIX_TOKEN,
            "id": 1
        }

        r = requests.post(zabbix_url, json=payload, timeout=40)

        log(f"[ZABBIX REQUEST] URL={zabbix_url} STATUS={r.status_code}")

        try:
            data = r.json()
        except Exception:
            log(f"[ZABBIX ERROR] resposta inválida: {r.text}")
            return

        if "error" in data:
            log(f"[ZABBIX ERROR] {data['error']}")
            return

        hosts = data.get("result", [])
        log(f"[ZABBIX] Hosts carregados: {len(hosts)}")

        for host in hosts:
            
            hostid = host["hostid"]
            hostname = (
                host.get("name")
                or host.get("host")
              or host.get("visible_name")
            )

            if not hostname:

                log(
                    f"[ZABBIX FULL HOST] "
                    f"{host}"
                )

                continue

            log(
                f"[ZABBIX HOST MAP] "
                f"host={host.get('host')} "
                f"name={host.get('name')} "
                f"final={hostname}"
            )
           

            # ==========================================================
            # ITEM.GET (OTIMIZADO - BUSCA APENAS O NECESSÁRIO)
            # ==========================================================
            payload_items = {
                "jsonrpc": "2.0",
                "method": "item.get",
                "params": {
                    "hostids": hostid,
                    "output": ["key_", "lastvalue"],
                    "search": {
                        "key_": "windows.*users.count" # Busca por local e logged
                    },
                    "searchWildcardsEnabled": True
                },
                "auth": ZABBIX_TOKEN,
                "id": 2
            }

            r_items = requests.post(zabbix_url, json=payload_items, timeout=40)

            log(f"[ZABBIX ITEM REQUEST] STATUS={r_items.status_code}")

            try:
                data_items = r_items.json()
            except Exception:
                log(f"[ZABBIX ITEM ERROR] resposta inválida: {r_items.text}")
                continue

            if "error" in data_items:
                log(f"[ZABBIX ITEM ERROR] {data_items['error']}")
                continue

            items = data_items.get("result", [])

            #log(f"[ZABBIX DEBUG] host={hostname} items={items}")

            if not items:
                log(f"[ZABBIX] Host {hostname} sem itens")
                items = []

            # =========================================================
            # CRIA CACHE BASE SEM DEPENDER DE ITENS WINDOWS
            # =========================================================
            host_key = (
                str(hostname)
                .lower()
                .replace("_", "-")
                .strip()
                .split(".")[0]
            )

            if host_key not in zabbix_cache:
                zabbix_cache[host_key] = {}

            statuses = []

            interfaces = host.get("interfaces", [])

            for iface in interfaces:

                iface_type = iface.get("type")
                available = str(
                    iface.get("available","0")
                )

                active_available = str(
                    iface.get("active_available", "0")
                )

                #log(
                #    f"[ZABBIX IFACE] "
                #    f"{hostname} "
                #    f"type={iface_type} "
                #    f"available={available}"
                #)

                if (
                    available == "1"
                    or active_available == "1"
                ):
                    statuses.append("Disponível")

                elif (
                    available == "2"
                    or active_available == "2"
                ):
                    statuses.append("Indisponível")

                elif (
                    available == "0"
                    or active_available == "0"
                ):
                    statuses.append("Desconhecido")
            
            
            if "Disponível" in statuses:
                final_status = "Disponível"

            elif "Indisponível" in statuses:
                final_status = "Indisponível"

            elif "Desconhecido" in statuses:
                final_status = "Desconhecido"

            else:
                final_status="Desconhecido"

            # =============================================lookup_name = name.lower().split(".")[0]========
            # GARANTE CACHE
            # =====================================================

            if host_key not in zabbix_cache:
                zabbix_cache[host_key] = {
                "agent_status": "Desconhecido", 
                "users_logged": 0,
                "users_local": 0
                }
           

            zabbix_cache[host_key]["agent_status"] = final_status            
            
            log(
                f"[ZABBIX CACHE SAVE] "
                f"{host_key} -> {final_status}"
            ) 
                     
            #log(f"[SYNC STEP] {nb_vm.name} BEFORE interface lookup")
            # salvar também por IP/interface principal
            interfaces_payload = {
                "jsonrpc": "2.0",
                "method": "hostinterface.get",
                "params": {
                    "hostids": hostid,
                    "output": ["ip"]
                },
                "auth": ZABBIX_TOKEN,
                "id": 3
            }
            
            try:

                r_if = requests.post(
                    zabbix_url,
                    json=interfaces_payload,
                    timeout=20
                )

                data_if = r_if.json()

                interfaces = data_if.get(
                    "result",
                    []
                )

                for iface in interfaces:

                    ip = iface.get("ip")

                    if ip:

                        zabbix_cache[ip] = zabbix_cache[host_key]

            except Exception as e:

                log(
                    f"[ZABBIX IP ERROR] "
                    f"{hostname}: {e}"
                )
                 
            for item in items:
                zabbix_cache[host_key][item["key_"]] = item["lastvalue"]

    
# =========================================================
# PBS COLLECTION (COM MÉDIA REAL)
# =========================================================
from urllib.parse import quote

def collect_pbs_backups():
    global pbs_backup_map

    local_map = {}

    if not config.get("pbs"):
        return local_map

    for pbs_conf in config["pbs"]:

        host = pbs_conf["host"]
        name = pbs_conf["name"]

        log(f"[PBS] Coletando servidor {name} ({host})")

        token_user = pbs_conf.get("token_user") or PBS_USER
        token_value = pbs_conf.get("token_value") or PBS_TOKEN

        headers = {
            "Authorization": f"PBSAPIToken {token_user}:{token_value}"
        }

        base_url = f"https://{host}:8007/api2/json/admin/datastore"

        try:
            # ===============================
            # LISTA DATASTORES
            # ===============================
            ds_res = requests.get(
                base_url,
                headers=headers,
                verify=False,
                timeout=15
            )

            if ds_res.status_code != 200:
                log(f"[PBS ERROR] {host} datastore HTTP {ds_res.status_code}")
                continue

            datastores = ds_res.json().get("data", [])

            # ===============================
            # LOOP DATASTORES
            # ===============================
            for ds in datastores:

                ds_name = ds["store"]

                # ===============================
                # Namespaces
                # ===============================
                namespaces = [""]

                try:
                    ns_url = f"{base_url}/{ds_name}/namespace"

                    ns_res = requests.get(
                        ns_url,
                        headers=headers,
                        verify=False,
                        timeout=10
                    )

                    if ns_res.status_code == 200:
                        for item in ns_res.json().get("data", []):
                            namespaces.append(item["ns"])

                except Exception as e:
                    log(f"[ERROR] {e}")

                # ===============================
                # LOOP Namespaces
                # ===============================
                for ns in namespaces:

                    try:
                        group_url = f"{base_url}/{ds_name}/groups"

                        params = {"ns": ns} if ns else {}

                        g_res = requests.get(
                            group_url,
                            headers=headers,
                            params=params,
                            verify=False,
                            timeout=15
                        )

                        if g_res.status_code != 200:
                            continue

                        groups = g_res.json().get("data", [])

                    except Exception:
                        continue

                    # ===============================
                    # LOOP GROUPS
                    # ===============================
                    for group in groups:

                        raw_id = str(
                            group.get("backup-id")
                            or group.get("group-id")
                            or ""
                        )

                        match = re.findall(r"\d+", raw_id)

                        if not match:
                            continue

                        vmid = match[0]

                        ns_name = ns if ns else "root"

                        raw_size = int(
                            group.get("filesize")
                            or group.get("size")
                            or 0
                        )

                        unique_key = f"{ns_name}:{vmid}"

                        local_map[unique_key] = {
                            "last_backup": group.get("last-backup"),
                            "backup_size": format_size(raw_size),
                            "datastore": ds_name,
                            "namespace": ns_name,
                            "backup_count": group.get(
                                "backup-count", 0
                            ),
                            "server_name": name,
                            "server_ip": host
                        }

        except Exception as e:
            log(f"[PBS ERROR] {host}: {e}")

    pbs_backup_map = local_map

    log(f"[PBS MAP FINAL] {len(local_map)} VMs encontradas")

    return pbs_backup_map    

# =========================================================
# SLA ENGINE (ORIGINAL + freq real)
# =========================================================

WEEKLY_PATTERNS = ["fw", "firewall", "borda"]

def is_weekly_vm(vm_name):
    name = vm_name.lower()
    return any(p in name for p in WEEKLY_PATTERNS)

def calculate_backup_status(nb_vm, vmid, vm_type, px_conf, proxmox_host, current_node):
    result = {
        "proxmox_access_link": f"https://{proxmox_host}:8006/#v1:0:=qemu/{vmid}",
        "server_host_ip": proxmox_host,
    }

    # LÓGICA HÍBRIDA:
    # 1. Se houver pbs_namespace fixo no YAML, usa ele.
    # 2. Senão, usa o nome do NÓ onde a VM está (current_node).
    # 3. Fallback para 'root'.
    target_namespace = px_conf.get("pbs_namespace") or current_node or "root"
    
    lookup_key = f"{target_namespace}:{vmid}"    
    log(f"[PBS DEBUG] VM {nb_vm.name} (ID {vmid}): Buscando no namespace '{target_namespace}'")

    backup_info = pbs_backup_map.get(lookup_key)

    if not backup_info:
        result.update({
            "pbs_backup_status": "NO BACKUP",
            "pbs_backup_age_hours": None,
            "pbs_backup_count": 0,
            "pbs_server_host": None,
            "pbs_server_name": None,
            "pbs_access_link": None
        })
        return result

    # --- COM BACKUP ---
    last_backup_ts = backup_info.get("last_backup")
    # Aqui pegamos as chaves novas que salvamos na coleta
    backup_count = backup_info.get("backup_count", 0)
    pbs_host = backup_info.get("server_ip")
    pbs_name = backup_info.get("server_name")
    datastore = backup_info.get("datastore")
    namespace = backup_info.get("namespace")

    last_backup_dt = datetime.fromtimestamp(last_backup_ts, tz=LOCAL_TZ)
    age_hours = (datetime.now(LOCAL_TZ) - last_backup_dt).total_seconds() / 3600

    # SLA
    threshold = 168 if is_weekly_vm(nb_vm.name) else 36
    status = "OK" if age_hours <= threshold else "CRITICAL"

    # Link de acesso corrigido
    pbs_link = f"https://{pbs_host}:8007/#DataStore-{datastore}:content"

    result.update({
        "pbs_backup_status": status,
        "pbs_last_backup": last_backup_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "pbs_backup_age_hours": round(age_hours, 2),
        "pbs_backup_count": backup_count,
        "backup_size": backup_info.get("backup_size"), 
        "pbs_datastore": datastore,
        "pbs_namespace": namespace,
        "pbs_server_host": pbs_host,
        "pbs_server_name": pbs_name,
        "pbs_access_link": pbs_link,
    })

    return result

# =========================================================
# SO DETECTION VIA AGENT
# =========================================================
def detect_os(proxmox, node, vmid):
    try:
        data = proxmox.nodes(node).qemu(vmid).agent.get("get-osinfo")
        result = data.get("result", {})

        name = result.get("pretty-name") \
            or result.get("name") \
            or result.get("kernel-release")

        version = result.get("version")
        kernel = result.get("kernel-release")

        if not name:
            return None, None

        # WINDOWS
        if "windows" in name.lower():
            build = result.get("version-id") or version
            if build:
                return name, f"Build {build}"
            return name, ""

        # LINUX
        if kernel:
            return name, f"Kernel {kernel}"

        return name, ""

    except Exception:
        return None, None


#============CREATE PLATAFORM =============================
def get_or_create_platform(nb, name):
    platform = nb.dcim.platforms.get(name=name)
    if platform:
        return platform

    # 🔥 sanitiza slug corretamente
    slug = name.lower()
    slug = re.sub(r'[^a-z0-9-_]+', '-', slug)   # remove tudo inválido
    slug = re.sub(r'-+', '-', slug)             # remove hífen duplicado
    slug = slug.strip('-')                      # remove hífen início/fim

    return nb.dcim.platforms.create({
        "name": name,
        "slug": slug
    })
# =========================================================
# TAG ENGINE
# =========================================================
def get_or_create_tag(name):
    tag = nb.extras.tags.get(name=name)

    if not tag:
        # 🔥 sanitiza slug corretamente
        slug = name.lower()
        slug = re.sub(r'[^a-z0-9-_]+', '-', slug)
        slug = re.sub(r'-+', '-', slug)
        slug = slug.strip('-')

        tag = nb.extras.tags.create({
            "name": name,
            "slug": slug
        })
        log(f"[TAG CREATED] {name}")

    return tag
# =========================================================
# VRF + MAC + IP ENGINE (INALTERADA DA 9.0)
# =========================================================
def extract_bridge(value):
    m = re.search(r'bridge=([^,]+)', value or "")
    return m.group(1) if m else None

def ensure_vrf(name):
    if not name:
        return None
    vrf = nb.ipam.vrfs.get(name=name)
    if not vrf:
        vrf = nb.ipam.vrfs.create({
            "name": name,
            "rd": f"65000:{abs(hash(name)) % 10000}"
        })
    return vrf

def get_mac_from_config(value):
    m = re.search(r'([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5})', value or "")
    return m.group(1).lower() if m else None

def get_lxc_ip(proxmox, node, vmid):
    try:
        data = proxmox.nodes(node).lxc(vmid).interfaces.get()

        if not data:
            return None

        for iface in data:
            if not isinstance(iface, dict):
                continue

            if "inet" in iface:
                ip = iface["inet"].split("/")[0]
                if ip and not ip.startswith("127."):
                    return ip

    except Exception as e:
        log(f"[LXC IP ERROR] {vmid} - {e}")

    return None

def get_qemu_ip_mac_map(proxmox, node, vmid):
    result = {}
    try:
        data = proxmox.nodes(node).qemu(vmid).agent(
            "network-get-interfaces"
        ).get()
        for iface in data.get("result", []):
            mac = iface.get("hardware-address")
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4":
                    ip = addr.get("ip-address")
                    if ip and not ip.startswith("127."):
                        result[mac.lower()] = f"{ip}/32"
    except Exception as e:
        log(f"[ERROR] {e}")

    return result

def get_proxmox_ip(proxmox, node):
    try:
        data = proxmox.nodes(node).network.get()

        for iface in data:
            if iface.get("iface") == "vmbr0":
                address = iface.get("address")
                if address:
                    return address

    except Exception as e:
        log(f"[PVE IP ERROR] {node} - {e}")

    return None

def sync_interfaces(nb_vm, proxmox, node, vmid, proxmox_config):
    ip_mac_map = get_qemu_ip_mac_map(proxmox, node, vmid)
    log(f"[SYNC STEP] {nb_vm.name} START")

    #FALLBACK PARA LXC
    if not ip_mac_map:
        try:
            lxc_ip = get_lxc_ip(proxmox, node, vmid)
            if lxc_ip:
                log(f"[LXC IP FOUND] VMID {vmid} -> {lxc_ip}")
                ip_mac_map = {"lxc": f"{lxc_ip}/32"}
        except Exception as e:
            log(f"[ERROR] {e}")
    # CASO LXC SEM MAC (DEFINE PRIMARY IP DIRETO)
    if "lxc" in ip_mac_map:
        ip = ip_mac_map["lxc"]
        
        try:

            ip_obj = nb.ipam.ip_addresses.get(
                address=ip,
                vrf_id=vrf.id if vrf else None
            )

            log(
                f"[IP LOOKUP OK] "
                f"{nb_vm.name} "
                f"ip={ip}"
            )

        except Exception as e:

            log(
                f"[IP LOOKUP ERROR] "
                f"{nb_vm.name} "
                f"ip={ip} "
                f"error={e}"
            )

            ip_obj = None
        
        log(
            f"[SYNC STEP] "
            f"{nb_vm.name} BEFORE IP create"
        ) 
        if not ip_obj:
            ip_obj = nb.ipam.ip_addresses.create({
                "address": ip,
                "status": "active"
            })
            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} AFTER IP create"
            )

        # ASSOCIA O IP À INTERFACE (OBRIGATÓRIO)
        iface = nb.virtualization.interfaces.filter(
            virtual_machine_id=nb_vm.id
        )

        iface = next(iter(iface), None)

        log(
            f"[SYNC STEP] "
            f"{nb_vm.name} BEFORE ip_obj.save"
        )

        if iface:
            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} BEFORE IP assignment"
            ) 
            ip_obj.assigned_object_type = "virtualization.vminterface"
            ip_obj.assigned_object_id = iface.id
            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} BEFORE ip_obj.save"
            )

            ip_obj.save()

            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} AFTER ip_obj.save"
            )

            if nb_vm.primary_ip4 != ip_obj.id:
                nb_vm.primary_ip4 = ip_obj.id

        else:
            log(f"[WARN] No interface found for VM {nb_vm.name}")
    
    for key, value in proxmox_config.items():
        if not key.startswith("net"):
            continue

        mac = get_mac_from_config(value)
        bridge = extract_bridge(value)
        vrf = ensure_vrf(bridge)

        # =========================================================
        # INTERFACE LOOKUP
        # =========================================================

        log(
            f"[SYNC STEP] "
            f"{nb_vm.name} BEFORE interface lookup"
        )

        log(
            f"[SYNC DEBUG] "
            f"vm={nb_vm.name} "
            f"interface={key}"
        )

        try:

            iface = nb.virtualization.interfaces.get(
                virtual_machine_id=nb_vm.id,
                name=key
            )

            log(
                f"[SYNC DEBUG] "
                f"{nb_vm.name} interface lookup OK"
            )

            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} AFTER interface lookup"
            )

            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} BEFORE IP processing"
            )

        except Exception as e:

            log(
                f"[SYNC IFACE ERROR] "
                f"{nb_vm.name}: {e}"
            )

            continue
                
        if not iface:
            iface = nb.virtualization.interfaces.create({
                "virtual_machine": nb_vm.id,
                "name": key,
                "type": "virtual"
            })

        if mac:
            existing_mac = nb.dcim.mac_addresses.get(mac_address=mac)

            if not existing_mac:
                existing_mac = nb.dcim.mac_addresses.create({
                    "mac_address": mac,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": iface.id
                })
            else:
                if existing_mac.assigned_object_id != iface.id:
                    existing_mac.update({
                        "assigned_object_type": "virtualization.vminterface",
                        "assigned_object_id": iface.id
                    })

        
        if mac and mac in ip_mac_map:
            
            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} BEFORE IP lookup"
            ) 
         
            ip = ip_mac_map[mac]

            current_cf = dict(nb_vm.custom_fields)

            current_cf["primary_mac"] = mac

            nb_vm.update({
                "custom_fields": current_cf
            })

            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} BEFORE IP section"
            )

            # =====================================================
            # DEBUG REAL DO IP LOOKUP
            # =====================================================

            try:

                if vrf:

                    results = list(
                        nb.ipam.ip_addresses.filter(
                            address=ip,
                            vrf_id=vrf.id
                        )
                    )

                else:

                    results = list(
                        nb.ipam.ip_addresses.filter(
                            address=ip
                        )
                    )

                ip_obj = results[0] if results else None

                log(
                    f"[IP LOOKUP OK] "
                    f"{nb_vm.name} "
                    f"ip={ip}"
                )

            except Exception as e:

                log(
                    f"[IP LOOKUP ERROR] "
                    f"{nb_vm.name} "
                    f"ip={ip} "
                    f"error={e}"
                )

                ip_obj = None

            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} AFTER IP lookup"
            )

            # 🔥 GARANTE QUE NÃO VAI DAR ERRO DE PRIMARY
            current_primary = nb_vm.primary_ip4

            if current_primary:
                if isinstance(current_primary, int):
                    current_id = current_primary
                else:
                    current_id = current_primary.id
            else:
                current_id = None

            # 🔥 SE ESTE IP JÁ É PRIMARY → REMOVE ANTES
            if current_id == (ip_obj.id if ip_obj else None):
                nb_vm.update({
                    "primary_ip4": None
                })
            # ================================
            # CREATE OR FIX IP
            # ================================
            log(
                f"[SYNC STEP] "
                f"{nb_vm.name} BEFORE IP lookup/create"
            ) 
            if not ip_obj:
                ip_obj = nb.ipam.ip_addresses.create({
                    "address": ip,
                    "status": "active",
                    "vrf": vrf.id if vrf else None,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": iface.id
                })
                log(
                    f"[SYNC STEP] "
                    f"{nb_vm.name} BEFORE IP create"
                )
            else:
                # 🔥 GARANTE REASSOCIAÇÃO LIMPA
                if ip_obj.assigned_object_id != iface.id:

                    # 🔥 REMOVE PRIMARY DE QUALQUER VM QUE ESTEJA USANDO ESSE IP
                    try:
                        vm_with_ip = nb.virtualization.virtual_machines.filter(primary_ip4_id=ip_obj.id)

                        for vm in vm_with_ip:
                            vm.update({
                                "primary_ip4": None
                            })
                            log(f"[FIX] Removed primary_ip4 from {vm.name}")

                    except Exception as e:
                        log(f"[WARN] Failed to clear primary_ip4: {e}")
                    log(
                        f"[SYNC STEP] "
                        f"{nb_vm.name} BEFORE ip_obj.save"
                    )                     

                    # 🔥 AGORA REASSOCIA
                    ip_obj.assigned_object_type = "virtualization.vminterface"
                    ip_obj.assigned_object_id = iface.id
                    ip_obj.save()

            # ================================
            # SET PRIMARY COM SEGURANÇA
            # ================================
            if not nb_vm.primary_ip4 or (hasattr(nb_vm.primary_ip4, 'id') and nb_vm.primary_ip4.id != ip_obj.id):
                log(
                    f"[SYNC STEP] "
                    f"{nb_vm.name} BEFORE primary_ip4 update"
                ) 
                # Usamos .update para enviar imediatamente ao Netbox
                nb_vm.update({"primary_ip4": ip_obj.id})
                log(f"[IP] {ip} definido como primário para {nb_vm.name}")
                log(
                    f"[SYNC STEP] "
                    f"{nb_vm.name} AFTER primary_ip4 update"
                ) 

            
            #if mac:

                #current_cf = dict(nb_vm.custom_fields)

                #current_cf["primary_mac"] = mac

                #nb_vm.update({
                    #"custom_fields": current_cf
                #})
    
# --- COLOQUE ESTA FUNÇÃO LOGO APÓS A FUNÇÃO log(msg) ---

def load_csv_inventory(file_path):
    csv_data = {}
    if not os.path.exists(file_path):
        log(f"[CSV ERROR] Arquivo não encontrado em: {file_path}")
        return csv_data
        
    try:
        with open(file_path, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                vmid = row.get('VMID')
                if vmid and vmid.strip():
                    # Usamos os nomes exatos das colunas do seu arquivo o3web.csv
                    csv_data[vmid.strip()] = {
                        "chave": row.get("chave_ativacao"),
                        "id": row.get("id_licenca"),
                        "tipo": row.get("tipo_licenca"),
                        "users": row.get("usuarios_licenca"),
                        "data": row.get("data_ativacao"),
                        "url": row.get("url_cliente")
                    }
        log(f"[CSV] Inventário carregado: {len(csv_data)} registros.")
    except Exception as e:
        log(f"[CSV ERROR] Falha ao processar CSV: {e}")
    return csv_data

# Carrega o cache do CSV uma única vez no início
csv_inventory = load_csv_inventory('/opt/netbox-sync/o3web.csv')

# =========================================================
# PROCESSING
# =========================================================
def process_vm(proxmox, px_conf, node, vm, vm_type, cluster_id, rack_name=None, site_name=None):

    name = vm["name"]
    vmid = vm["vmid"]
    log(f"[DEBUG] Processing {name} ({vm_type}) VMID={vmid}")
    log(f"[TRACE] {name} STEP 1")
    proxmox_vm_keys.add(f"{name}")

    try:
        if vm_type == "vm":
            config_vm = proxmox.nodes(node).qemu(vmid).config.get()
        else:
            config_vm = proxmox.nodes(node).lxc(vmid).config.get()
    except Exception as e:
        log(f"[CONFIG ERROR] {name} ({vm_type}) - {e}")
        return

    # --- BUSCA OU CRIA A VM NO NETBOX ---
    nb_vm = nb.virtualization.virtual_machines.get(name=name)
    
    if not nb_vm:
        log(f"[NETBOX] VM {name} não encontrada, tentando criar...")
        try:
            nb_vm = nb.virtualization.virtual_machines.create({
                "name": name,
                "cluster": cluster_id,
                "status": "active"
            })
        except Exception as e:
            log(f"[NETBOX ERROR] Falha crítica ao criar VM {name}: {e}")
            return 

    # -- VÍNCULO COM O HOST FÍSICO E SEGURANÇA DE CLUSTER ---
    nb_host_device = nb.dcim.devices.get(name=node)
    
    if nb_host_device:
        host_cluster_id = nb_host_device.cluster.id if nb_host_device.cluster else None
        
        # Se o host físico está em um cluster diferente da VM, corrigimos o cluster da VM primeiro
        if host_cluster_id and host_cluster_id != cluster_id:
            log(f"[FIX] Movendo VM {name} para o cluster correto ({host_cluster_id}) para bater com o host {node}")
            nb_vm.update({"cluster": host_cluster_id})
            cluster_id = host_cluster_id # Atualiza a variável local para os próximos passos

        # Agora vinculamos o device com segurança
        try:
            if nb_vm.device is None or nb_vm.device.id != nb_host_device.id:
                nb_vm.update({"device": nb_host_device.id})
                log(f"[NETBOX] VM {name} vinculada ao host físico {node}")
        except Exception as e:
            log(f"[NETBOX ERROR] Falha ao vincular device: {e}")
    else:
        log(f"[WARN] Host físico '{node}' não encontrado no Netbox.")

    # Verifica se a VM existe no Netbox antes de seguir para os custom_fields
    if nb_vm is None:
        log(f"[SKIP] Pulando {name} pois não foi possível carregar do Netbox.")
        return

    # Atualiza o VMID nos campos personalizados
    nb_vm.custom_fields["vmid"] = vmid

    # ================================
    # RECURSOS
    # ================================
    nb_vm.vcpus = config_vm.get("cores") or config_vm.get("cpus")
    nb_vm.memory = config_vm.get("memory")

    os_name = None
    platform_id = None 
    # --- DETECT OS ---
    if vm_type == "vm":
        os_name, os_version = detect_os(proxmox, node, vmid)
        if os_name:
            # Associa a plataforma (ex: Debian 12, Windows 2022)
            # NORMALIZAÇÃO DE PLATFORM
            normalized_platform = os_name

            if os_name:

                lower_os = os_name.lower()

            # pfSense
                if (
                    "pfsense" in lower_os
                    or "freebsd" in lower_os
                ):
                    normalized_platform = "pfSense"

            # Ubuntu
                elif "ubuntu" in lower_os:
                    normalized_platform = "Ubuntu"

            # Debian
                elif "debian" in lower_os:
                    normalized_platform = "Debian"

            # Rocky
                elif "rocky" in lower_os:
                    normalized_platform = "Rocky Linux"

            # CentOS
                elif "centos" in lower_os:
                    normalized_platform = "CentOS"

            # Windows
                elif "windows" in lower_os:
                    normalized_platform = os_name

            platform_obj = get_or_create_platform(
                nb,
                normalized_platform
            )

            platform_id = platform_obj.id
            
            # Salva a versão detalhada (ex: Kernel ou Build) no custom field
            # Certifique-se que o campo 'os_version' existe no seu NetBox
            nb_vm.custom_fields["os_version"] = os_version
    # --------------------------------

    disk = None
    for key, value in config_vm.items():
        if key.startswith(("scsi", "virtio", "sata", "rootfs")):
            size_match = re.search(r"size=(\d+)([G|M])", value)
            if size_match:
                size = int(size_match.group(1))
                unit = size_match.group(2)
                disk = size * 1024 if unit == "G" else size
                break

    if disk and nb_vm.disk != disk:

        old_disk = nb_vm.disk

        nb_vm.disk = disk

        log(
            f"[DISK UPDATE] "
            f"{name}: "
            f"{old_disk} -> {disk}"
        )

    # ================================
    # BOOT + PROXMOX INFO
    # ================================
    onboot = config_vm.get("onboot")

    #if onboot is None:
        #log(f"[INFO] {name} sem onboot explícito → assumindo False")
        #startonboot = False  # ou True se quiser assumir default
    #else:
        #startonboot = str(onboot) == "1"

    log(f"[DEBUG ONBOOT] {name} raw onboot={config_vm.get('onboot')}")
    nb_vm.custom_fields["proxmox_node"] = node
    nb_vm.custom_fields["proxmox_host_ip"] = px_conf["host"]

    real_ip = get_proxmox_ip(proxmox, node)

    if not real_ip or not is_reachable(real_ip):
        real_ip = px_conf["host"]

    # ================================
    # BACKUP (UMA VEZ SÓ)
    # ================================
    backup = calculate_backup_status(nb_vm, vmid, vm_type, px_conf, real_ip, node)
    #log(f"[MATCH DEBUG] VM={name} vmid={vmid}")
    #log(f"[MATCH DEBUG] PBS KEYS SAMPLE={list(pbs_backup_map.keys())[:20]}")

    proxmox_host = real_ip

    nb_vm.custom_fields["pbs_backup_status"] = backup.get("pbs_backup_status")
    nb_vm.custom_fields["pbs_backup_age_hours"] = backup.get("pbs_backup_age_hours")
    nb_vm.custom_fields["pbs_backup_count"] = backup.get("pbs_backup_count")
    nb_vm.custom_fields["pbs_last_backup"] = backup.get("pbs_last_backup")
    nb_vm.custom_fields["pbs_datastore"] = backup.get("pbs_datastore")
    nb_vm.custom_fields["pbs_namespace"] = backup.get("pbs_namespace")
    nb_vm.custom_fields["pbs_backup_size"] = backup.get("backup_size")
    nb_vm.custom_fields["pbs_server_ip"] = backup.get("pbs_server_host")
    nb_vm.custom_fields["pbs_server_name"] = backup.get("pbs_server_name")
    nb_vm.custom_fields["pbs_url"] = backup.get("pbs_access_link")

    nb_vm.custom_fields["pve_url"] = backup.get("proxmox_access_link")

    nb_vm.comments = f"Host: {node} ({px_conf['host']})"
          
    # ================================
    # INTERFACES
    # ================================
    log(f"[TRACE] {name} BEFORE sync_interfaces") 
    sync_interfaces(nb_vm, proxmox, node, vmid, config_vm)
    log(f"[TRACE] {name} AFTER sync_interfaces")

    # RELOAD DA VM APÓS ALTERAÇÃO DE INTERFACES/IP
    nb_vm = nb.virtualization.virtual_machines.get(id=nb_vm.id)
    log(f"[TRACE] {name} AFTER reload")
    
    # =========================================================
    # PREPARAÇÃO DE ATUALIZAÇÕES (CUSTOM FIELDS)
    # =========================================================
    cf_updates = {}

    
    # 1. Start on Boot (Extraído do Proxmox)
    onboot_raw = config_vm.get("onboot")
    cf_updates["startonboot"] = True if str(onboot_raw) == "1" else False

    # 2. Backup Ativado (Baseado no status do PBS)
    # Se o status não for "NO BACKUP", entendemos que existe rotina de backup
    if backup.get("pbs_backup_status") in ["OK", "CRITICAL"]:
        cf_updates["Backup-Ativado"] = True
    else:
        cf_updates["Backup-Ativado"] = False

    # 3. Dados do CSV (Enriquecimento o3web.csv)
    client_info = csv_inventory.get(str(vmid).strip())

    if client_info:
        log(f"[CSV MATCH] VMID {vmid} vinculado com sucesso.")
        cf_updates["cf_chave_ativacao"] = client_info.get("chave")
        cf_updates["customer_url"] = client_info.get("url")
        
        # --- ATUALIZADO PARA O NOME CORRETO DO NETBOX ---
        cf_updates["activation_data"] = client_info.get("data")

        # Mapeamento do Tipo de Licença
        raw_tipo = client_info.get("tipo", "").lower().strip()
        if "permanent" in raw_tipo:
            cf_updates["licence_type"] = "Permanente"
        elif "trial" in raw_tipo:
            cf_updates["licence_type"] = "trial"

        # Conversão de IDs e Números
        try:
            val_id = client_info.get("id")
            cf_updates["id_licenca"] = int(val_id) if val_id else None
            val_users = client_info.get("users")
            cf_updates["usuarios_licenca"] = int(val_users) if val_users else None
        except Exception as e:
            log(f"[SYNC ERROR] {nb_vm.name}: {e}")

    if not client_info:
        log(f"[CSV DEBUG] VMID {vmid} não encontrado no arquivo CSV. IDs disponíveis: {list(csv_inventory.keys())[:5]}")

    # 4. Dados do Zabbix (Usuários Logados)
    ip_debug = None
    if nb_vm.primary_ip4:
        ip_debug = str(nb_vm.primary_ip4.address).split("/")[0]
    else:
        ip_debug = None

    log(f"[TRACE] {name} BEFORE zabbix lookup")

    lookup_name = (
        str(name)
        .lower()
        .replace("_", "-")
        .strip()
        .split(".")[0]
    )

    ip_lookup = None

    if nb_vm.primary_ip4:

        try:

            ip_lookup = str(
                nb_vm.primary_ip4.address
            ).split("/")[0]

        except:

            ip_lookup = None


    z_data = (
        zabbix_cache.get(lookup_name)
        or zabbix_cache.get(ip_lookup)
    )

    log(
        f"[ZABBIX LOOKUP] "
        f"name={lookup_name} "
        f"ip={ip_lookup} "
        f"found={bool(z_data)} "
        f"data={z_data}"
    )

    if z_data:

        # usuários locais
        local_users = z_data.get(
            "windows.local.users.count"
        )

        try:
            cf_updates["windows_local_users"] = (
                int(local_users)
                if local_users not in [None, ""]
                else 0
            )
        except:
            cf_updates["windows_local_users"] = 0


        # usuários logados
        logged_users = z_data.get(
            "windows.logged.users.count"
        )

        try:
            cf_updates["windows_logged_users"] = (
                int(logged_users)
                if logged_users not in [None, ""]
                else 0
            )
        except:
            cf_updates["windows_logged_users"] = 0


        # status Zabbix
        cf_updates["zabbix_agent_status"] = (
            z_data.get(
                "agent_status",
                "Desconhecido"
            )
        )

        log(
            f"[ZABBIX STATUS] "
            f"{name} -> "
            f"{cf_updates['zabbix_agent_status']}"
        )
    log(f"[SYNC STEP] {nb_vm.name} END")  
    # =========================================================
    # APLICAÇÃO FINAL E SALVAMENTO
    # =========================================================
    
    # 1. Pegamos o que já existe na VM (para não perder outros campos manuais)
    final_custom_fields = dict(nb_vm.custom_fields)

    # 2. Injetamos os dados do PBS (Dados dinâmicos)
    final_custom_fields.update({
        "pbs_backup_status": backup.get("pbs_backup_status"),
        "pbs_backup_age_hours": backup.get("pbs_backup_age_hours"),
        "pbs_backup_count": backup.get("pbs_backup_count"),
        "pbs_last_backup": backup.get("pbs_last_backup"),
        "pbs_server_ip": backup.get("pbs_server_host"),
        "pbs_server_name": backup.get("pbs_server_name"),
        "pbs_url": backup.get("pbs_access_link"),
        "pve_url": backup.get("proxmox_access_link")
    })
    
    if "primary_mac" not in cf_updates:

        current_mac = nb_vm.custom_fields.get(
            "primary_mac"
        )

        if current_mac:
            cf_updates["primary_mac"] = current_mac

    # 3. Injetamos os dados do CSV e checagens (Dados de negócio)
    # Isso vai SOBRESCREVER os valores antigos pelos do cf_updates atualizados hoje
    final_custom_fields.update(cf_updates)

    try:
        # 4. Aplicamos a atualização completa
        update_payload = {
            "custom_fields": final_custom_fields,
            "vcpus": config_vm.get("cores") or config_vm.get("cpus"),
            "memory": config_vm.get("memory"),
            "comments": f"Host: {node} ({px_conf['host']})"
        }

        # =========================================================
        # PLATFORM UPDATE FIX
        # =========================================================
        if vm_type == "vm" and platform_id:

            current_platform = (
                nb_vm.platform.id
                if nb_vm.platform else None
            )

            if current_platform != platform_id:

                log(
                    f"[PLATFORM UPDATE] "
                    f"{name}: "
                    f"{current_platform} -> {platform_id}"
                )


            update_payload["platform"] = platform_id

        # =========================================================
        # UPDATE FINAL
        # =========================================================
        nb_vm.update(update_payload)

        
        log(f"[OK] Dados atualizados com sucesso para {name} (VMID: {vmid})")
        
    except Exception as e:
        log(f"[ERROR] Falha ao atualizar campos da VM {name}: {e}")

 
    # =========================================================
    # LOG DE VERIFICAÇÃO DE IP FINAL (COLOQUE AQUI)
    # =========================================================
    try:
        if nb_vm.primary_ip4:
            if hasattr(nb_vm.primary_ip4, 'address'):
                current_ip = nb_vm.primary_ip4.address
            elif isinstance(nb_vm.primary_ip4, dict):
                current_ip = nb_vm.primary_ip4.get('address')
            else:
                current_ip = str(nb_vm.primary_ip4)
            log(f"[DEBUG] {name} FINAL IP={current_ip}")
        else:
            log(f"[DEBUG] {name} FINAL IP=Não definido")
    except Exception as e:
        log(f"[DEBUG] {name} Erro ao ler IP final: {e}")

    # ================================
    # TAG SYNC (O bloco que vem depois)
    # ================================
    tag_ids = []
    # ... resto do código

    # ================================
    # TAG SYNC (USANDO IDs - CORRETO)
    # ================================
    tag_ids = []

    # 1. Recupera as tags que já vêm do Proxmox
    is_prod = False
    if "tags" in vm and vm["tags"]:
        for tag in vm["tags"].split(";"):
            tag_obj = get_or_create_tag(tag)
            tag_ids.append(tag_obj.id)
            if tag.lower() == "prod":
                is_prod = True

    # 2. TAG de backup (Status do PBS)
    if backup.get("pbs_backup_status") == "CRITICAL":
        tag_ids.append(get_or_create_tag("backup-missing").id)
    elif backup.get("pbs_backup_status") == "OK":
        tag_ids.append(get_or_create_tag("backup-ok").id)

    # 3. TAG condicional (Só coloca 'implan' se NÃO for 'prod')
    if not is_prod:
        implan_tag = get_or_create_tag("implan")
        if implan_tag.id not in tag_ids:
            tag_ids.append(implan_tag.id)
    else:
        # Garante que se a VM for PROD, a tag 'implan' seja removida da lista final
        implan_tag = get_or_create_tag("implan")
        if implan_tag.id in tag_ids:
            tag_ids.remove(implan_tag.id)

    # APPLY (Substitui as tags no Netbox pelas novas IDs filtradas)
    nb_vm.update({
        "tags": tag_ids
    })

    # ================================
    # SAVE FINAL (UMA VEZ SÓ)
    # ================================
    log(f"[OK] Sincronismo completo para {name}")
    vm_check = nb.virtualization.virtual_machines.get(id=nb_vm.id)
    #log(f"[NETBOX VERIFY] {name} -> {vm_check.custom_fields}")
    log(f"[OK] Sync finalizado para {name}")


def process_proxmox(px_conf, cluster_id, site_name, target_node=None):
    
    proxmox = connect_proxmox(px_conf)
    if not proxmox: return

    for node_info in proxmox.nodes.get():
        node_name = node_info["node"] # Aqui definimos node_name
        
        # Trava de Segurança: Processa apenas o nó alvo definido no YAML
        if target_node and node_name != target_node:
            continue

        log(f"[NODE] Processing {node_name} on Cluster ID {cluster_id}")
        
        # Lógica para identificar o Rack pelo nome do Node
        rack_match = re.search(r'-(R\d+)', node_name)
        rack_name = rack_match.group(1) if rack_match else None

        # Processamento de VMs (QEMU)
        for vm in proxmox.nodes(node_name).qemu.get():
            process_vm(proxmox, px_conf, node_name, vm, "vm", cluster_id, rack_name, site_name)

        # Processamento de Containers (LXC)
        for ct in proxmox.nodes(node_name).lxc.get():
            process_vm(proxmox, px_conf, node_name, ct, "ct", cluster_id, rack_name, site_name)

def run_cleanup(target_cluster_id):
    if not SYNC_DELETE:
        return
        
    log(f"[CLEANUP] Verificando cluster {target_cluster_id}. Proxmox reportou {len(proxmox_vm_keys)} VMs.")
    vms_no_netbox = nb.virtualization.virtual_machines.filter(cluster_id=target_cluster_id)
    
    for vm_obj in vms_no_netbox:

        if vm_obj.name not in proxmox_vm_keys:

            log(
                f"[DELETE] A VM '{vm_obj.name}' "
                f"não existe mais no Proxmox. Removendo..."
            )

            try:
                vm_obj.delete()

            except Exception as e:
                log(
                    f"[ERROR] Falha ao deletar "
                    f"{vm_obj.name}: {e}"
                )

# =========================================================
# EXECUTION (O Coração do Multi-Site)
# =========================================================
if __name__ == "__main__":

    log("Iniciando Sincronismo Multi-Site...")

    load_zabbix_cache()

    # PBS GLOBAL
    collect_pbs_backups()

    # IMPORTANTE:
    # NÃO limpar por host
    
    for env in config.get("environments", []):

        proxmox_vm_keys.clear()
 
        current_site = env['site_name']

        log(f"=== Processando Site: {current_site} ===")

        cluster_ids_processed = set()

        for host_data in env.get("proxmox_hosts", []):

            try:

                px_temp = connect_proxmox(host_data)

                if not px_temp:
                    continue

                nodes = px_temp.nodes.get()

                if not nodes:
                    continue

                yaml_cluster_name = host_data.get("cluster_name")

                current_node = None

                # PROCURA NODE EXATO
                for n in nodes:

                    if n["node"] == yaml_cluster_name:
                        current_node = n["node"]
                        break

                # FALLBACK
                if not current_node:
                    current_node = nodes[0]["node"]

                node = current_node

                target_cluster_name = (
                    yaml_cluster_name or node
                )

                nb_cluster = nb.virtualization.clusters.get(
                    name=target_cluster_name
                )

                if nb_cluster:

                    log(
                        f"Sincronizando Host "
                        f"{host_data['host']} "
                        f"no Cluster "
                        f"{target_cluster_name}"
                    )

                    process_proxmox(
                        host_data,
                        nb_cluster.id,
                        current_site,
                        target_node=node
                    )

                    cluster_ids_processed.add(
                        nb_cluster.id
                    )

                else:

                    log(
                        f"ERRO: Cluster "
                        f"'{target_cluster_name}' "
                        f"não encontrado no Netbox!"
                    )

            except Exception as e:

                log(
                    f"Falha ao processar "
                    f"host {host_data.get('host')}: {e}"
                )

        # CLEANUP APENAS UMA VEZ
        if config["netbox"].get("sync_delete", False):

            for cluster_id in cluster_ids_processed:

                run_cleanup(cluster_id)

    log("Sincronismo finalizado.")

print("=== Sync 10.1 Finalizado ===")