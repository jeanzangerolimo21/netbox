from utils.logger import log

from services.proxmox_sync import (
    ProxmoxSyncService
)

from legacy.sync_original import (
    connect_proxmox,
    nb
)


class EnvironmentSyncService:

    def __init__(self, config, context):

        self.config = config
        self.context = context

    def run(self):

        for env in self.config.get(
            "environments",
            []
        ):

            current_site = (
                env["site_name"]
            )

            log(
                f"=== Processando Site: "
                f"{current_site} ==="
            )

            for host_data in env.get(
                "proxmox_hosts",
                []
            ):

                try:

                    proxmox = (
                        connect_proxmox(
                            host_data
                        )
                    )

                    if not proxmox:
                        continue

                    nodes = (
                        proxmox.nodes.get()
                    )

                    if not nodes:
                        continue

                    yaml_cluster_name = (
                        host_data.get(
                            "cluster_name"
                        )
                    )

                    current_node = None

                    for n in nodes:

                        if (
                            n["node"]
                            ==
                            yaml_cluster_name
                        ):

                            current_node = (
                                n["node"]
                            )

                            break

                    if not current_node:

                        current_node = (
                            nodes[0]["node"]
                        )

                    node = current_node

                    target_cluster_name = (
                        yaml_cluster_name
                        or node
                    )

                    nb_cluster = (
                        nb.virtualization
                        .clusters
                        .get(
                            name=target_cluster_name
                        )
                    )

                    if not nb_cluster:

                        continue

                    ProxmoxSyncService(
                        host_data,
                        proxmox,
                        nb_cluster.id,
                        current_site,
                        self.context,
                        target_node=node
                    ).run()

                except Exception as e:

                    log(
                        f"[ENV ERROR] "
                        f"{host_data.get('host')}: "
                        f"{e}"
                    )