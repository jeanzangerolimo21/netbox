from collectors.zabbix import ZabbixCollector
from collectors.pbs import PBSCollector
from collectors.o3web import O3WebCollector

from utils.logger import log
from utils.config import load_config

from services.proxmox_sync import (
    ProxmoxSyncService
)

from services.environment_sync import (
    EnvironmentSyncService
)


from models.sync_context import (
    SyncContext
)


def main():


    config = load_config()

    log(
        "Iniciando sincronismo"
    )

    zabbix_cache = (
        ZabbixCollector(config)
        .load_cache()
    )

    pbs_backup_map = (
        PBSCollector(config)
        .collect()
    )

    csv_inventory = (
        O3WebCollector(config)
        .load_inventory()
    )

    context = SyncContext(
        zabbix_cache=zabbix_cache,
        pbs_backup_map=pbs_backup_map,
        csv_inventory=csv_inventory
    )

    log(
        f"Zabbix: "
        f"{len(zabbix_cache)} hosts"
    )

    log(
        f"PBS: "
        f"{len(pbs_backup_map)} backups"
    )

    log(
        f"CSV: "
        f"{len(csv_inventory)} registros"
    )

    EnvironmentSyncService(
        config,
        context
    ).run()
     
    log(
        "Sincronismo finalizado"
    )


if __name__ == "__main__":
    main()