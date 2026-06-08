# netbox/client.py

import pynetbox


class NetBoxClient:

    def __init__(
        self,
        config
    ):

        self.client = pynetbox.api(
            config["netbox"]["url"],
            token=config["env"]["NETBOX_TOKEN"]
        )

        self.client.http_session.verify = (
            config["netbox"]
            .get(
                "validate_certs",
                True
            )
        )

        self.client.http_session.timeout = 20