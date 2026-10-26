# SPDX-License-Identifier: Apache-2.0

"""Pair with a self-hosted Claude hub (``hub/`` in this repository) instead of the Muse app.

The hub hands out the same device and refresh tokens the Muse app does, so
once paired the service runs unchanged: it fetches the hub's "VM", opens a
Noise session to it and serves its commands.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from musegadget import config, muse_api

log = logging.getLogger(__name__)

PAIR_PATH = "/hub/pair"


class HubPairError(Exception):
    pass


def pair_with_hub(hub_url: str, code: str, node_id: str, display_name: str) -> dict:
    """Redeem a pairing code from ``gadgethub pair-code`` and save the pairing."""
    hub_url = hub_url.rstrip("/")
    if not hub_url.startswith("https://"):
        raise HubPairError("the hub URL must start with https://")
    request = urllib.request.Request(
        hub_url + PAIR_PATH, method="POST",
        data=json.dumps({"code": code, "node_id": node_id, "display_name": display_name}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": muse_api.user_agent()},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise HubPairError("the hub doesn't know that code; make a new one with "
                               "`gadgethub pair-code`") from None
        raise HubPairError(f"the hub refused pairing: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HubPairError(f"could not reach the hub: {exc}") from None
    if not (isinstance(data, dict) and data.get("access_token") and data.get("refresh_token")):
        raise HubPairError("the hub's answer had no tokens")
    api_url = data.get("api_url") if str(data.get("api_url", "")).startswith("https://") else hub_url
    record = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "token_type": "device",
        "api_url_v2": api_url,
        "noise_host": data.get("noise_host") or api_url.split("://", 1)[1],
        "access_token_saved_at": int(time.time()),
        "hub": True,
    }
    config.save_json(config.PAIRING_FILE, record)
    log.info("paired with the hub at %s", api_url)
    return record
