import csv
import os

from utils.logger import log


class O3WebCollector:

    def __init__(
        self,
        config
    ):

        self.config = config        
 
        self.csv_file = (
            config.get(
                "csv_inventory",
                "/opt/netbox-sync/data/o3web.csv"
            )
        )  

    def load_inventory(self):

        inventory = {}

        if not os.path.exists(self.csv_file):

            log(
                f"[CSV ERROR] "
                f"{self.csv_file} não encontrado"
            )

            return inventory

        with open(
            self.csv_file,
            encoding="utf-8-sig"
        ) as f:

            reader = csv.DictReader(f)

            for row in reader:

                vmid = row.get("VMID")

                if not vmid:
                    continue

                inventory[str(vmid).strip()] = {

                    "chave":
                        row.get("chave_ativacao"),

                    "id":
                        row.get("id_licenca"),

                    "tipo":
                        row.get("tipo_licenca"),

                    "users":
                        row.get("usuarios_licenca"),

                    "data":
                        row.get("data_ativacao"),

                    "url":
                        row.get("url_cliente")
                }

        log(
            f"[CSV] "
            f"{len(inventory)} registros carregados"
        )

        return inventory