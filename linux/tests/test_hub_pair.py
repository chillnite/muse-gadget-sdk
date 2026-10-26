# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json

import pytest

from musegadget import config, hub_pair


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path))
    return tmp_path


def answer(monkeypatch, body=None, status=200):
    sent = []

    def urlopen(req, timeout):
        sent.append((req.full_url, json.loads(req.data)))
        if status != 200:
            raise hub_pair.urllib.error.HTTPError(req.full_url, status, "", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(hub_pair.urllib.request, "urlopen", urlopen)
    return sent


def test_pairing_saves_what_the_service_reads(monkeypatch):
    sent = answer(monkeypatch, {"access_token": "a", "refresh_token": "r",
                                "api_url": "https://hub.example:8443", "noise_host": "hub.example:8443"})
    hub_pair.pair_with_hub("https://hub.example:8443/", "ABCD-EF01-2345", "homelink-abcdef", "pi")
    assert sent == [("https://hub.example:8443/hub/pair",
                     {"code": "ABCD-EF01-2345", "node_id": "homelink-abcdef", "display_name": "pi"})]
    saved = config.load_json(config.PAIRING_FILE)
    assert (saved["access_token"], saved["refresh_token"]) == ("a", "r")
    assert (saved["api_url_v2"], saved["noise_host"]) == ("https://hub.example:8443", "hub.example:8443")
    assert saved["access_token_saved_at"] > 0


def test_a_missing_noise_host_falls_back_to_the_hub_address(monkeypatch):
    answer(monkeypatch, {"access_token": "a", "refresh_token": "r"})
    hub_pair.pair_with_hub("https://hub.example", "c", "homelink-abcdef", "pi")
    saved = config.load_json(config.PAIRING_FILE)
    assert (saved["api_url_v2"], saved["noise_host"]) == ("https://hub.example", "hub.example")


@pytest.mark.parametrize("url, body, status, message", [
    ("http://hub.example", None, 200, "https://"),
    ("https://hub.example", None, 403, "doesn't know that code"),
    ("https://hub.example", None, 500, "HTTP 500"),
    ("https://hub.example", {"access_token": "a"}, 200, "no tokens"),
])
def test_failures_save_nothing(monkeypatch, url, body, status, message):
    answer(monkeypatch, body, status)
    with pytest.raises(hub_pair.HubPairError, match=message):
        hub_pair.pair_with_hub(url, "c", "homelink-abcdef", "pi")
    assert config.load_json(config.PAIRING_FILE) is None
