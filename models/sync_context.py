class SyncContext:

    def __init__(
        self,
        zabbix_cache=None,
        pbs_backup_map=None,
        csv_inventory=None
    ):

        self.zabbix_cache = (
            zabbix_cache or {}
        )

        self.pbs_backup_map = (
            pbs_backup_map or {}
        )

        self.csv_inventory = (
            csv_inventory or {}
        )

        self.proxmox_vm_keys = set()