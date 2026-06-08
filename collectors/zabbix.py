import requests

from utils.logger import log

class ZabbixCollector:

    def __init__(self,config):

        self.config = config

        self.cache = {}

        self.token = (
            config["env"]
            ["ZABBIX_TOKEN"]
        )  

    def load_cache(self):

        if not self.token:

            log(
                "[ZABBIX] "
                "Token não definido"
            )

            return self.cache

        for zbx in self.config.get(
            "zabbix",
            []
        ):

            self._load_server(zbx)

        return self.cache

    def _load_server(
        self,
        zbx
    ):

        url = (
            f"http://{zbx['host']}"
            "/zabbix/api_jsonrpc.php"
        )

        payload = {
            "jsonrpc": "2.0",
            "method": "host.get",
            "params": {
                "output": "extend",
                "selectInterfaces": "extend"
            },
            "auth": self.token,
            "id": 1
        }

        try:

            r = requests.post(
                url,
                json=payload,
                timeout=30
            )

            data = r.json()

        except Exception as e:

            log(
                f"[ZABBIX ERROR] {e}"
            )

            return

        hosts = data.get(
            "result",
            []
        )

        for host in hosts:

            hostname = (
                host.get("name")
                or host.get("host")
            )

            if not hostname:
                continue

            key = (
                hostname
                .lower()
                .replace("_", "-")
                .split(".")[0]
            )

            self.cache[key] = {
                "agent_status":
                    "Disponível"
            }