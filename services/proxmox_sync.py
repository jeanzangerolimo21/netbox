import re

from utils.logger import log
from legacy.sync_original import process_vm


class ProxmoxSyncService:

    def __init__(
        self,
        px_conf,  
        proxmox,
        cluster_id,
        site_name,
        context,
        target_node=None
    ):

        self.px_conf = px_conf
        self.context = context
        self.proxmox = proxmox
        self.cluster_id = cluster_id
        self.site_name = site_name
        self.target_node = target_node

    def run(self):

        log(
            "[PROXMOX] "
            "Iniciando sincronismo"
        )

    
        for node_info in self.proxmox.nodes.get():

            try:

                node_name = node_info["node"]

                if (
                    self.target_node
                    and node_name != self.target_node
                ):
                    continue

                log(
                    f"[NODE] Processing "
                    f"{node_name}"
                )

                rack_match = re.search(
                    r'-(R\d+)',
                    node_name
                )

                rack_name = (
                    rack_match.group(1)
                    if rack_match
                    else None
                )

                for vm in self.proxmox.nodes(node_name).qemu.get():

                    process_vm(
                        self.proxmox,
                        self.px_conf,
                        self.context,
                        node_name,
                        vm,
                        "vm",
                        self.cluster_id,
                        rack_name,
                        self.site_name
                    )

                for ct in self.proxmox.nodes(node_name).lxc.get():
 
                    process_vm(
                        self.proxmox,
                        self.px_conf,
                        node_name,
                        ct,
                        "ct",
                        self.cluster_id,
                        rack_name,
                        self.site_name
                    )
            except Exception as e:

                log(
                    f"[PROXMOX ERROR] "
                    f"{str(e)}"
                )         
    
        log(
            "[PROXMOX] "
            "Finalizado"
        )