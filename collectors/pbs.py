import requests

from utils.logger import log


class PBSCollector:

    def __init__(self, config):

        self.config = config

        self.backups = {}

        self.pbs_user = (
            config["env"]
            ["PBS_USER"]
        )

        self.pbs_token = (
            config["env"]
            ["PBS_TOKEN"]
        )
        

    def collect(self):

        for pbs in self.config.get(
            "pbs",
            []
        ):

            self._collect_server(
                pbs
            )

        return self.backups

    def _collect_server(
        self,
        pbs
    ):

        host = pbs["host"]

        token_user = (
            pbs.get("token_user")
            or self.pbs_user
        )

        token_value = (
            pbs.get("token_value")
            or self.pbs_token
        )

        headers = {
            "Authorization":
            f"PBSAPIToken "
            f"{token_user}:{token_value}"
        }

        try:

            r = requests.get(
                f"https://{host}:8007/api2/json/admin/datastore",
                headers=headers,
                verify=False,
                timeout=15
            )

            if r.status_code != 200:
                return

            datastores = r.json().get(
                "data",
                []
            )

            log(
                f"[PBS] "
                f"{host} "
                f"{len(datastores)} datastores"
            )

        except Exception as e:

            log(
                f"[PBS ERROR] "
                f"{host}: {e}"
            )