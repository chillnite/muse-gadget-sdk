# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from gadgethub.store import ACCESS_TOKEN_TTL_S, PAIR_CODE_TTL_S, VM_TOKEN_TTL_S, Store


def test_a_pairing_code_works_once_and_issues_tokens(tmp_path):
    store = Store.load(tmp_path)
    code = store.new_pair_code(now=0)
    tokens = store.pair(code.lower(), "homelink-abcdef", "pi", now=1)
    assert tokens is not None
    assert store.pair(code, "homelink-other", "", now=2) is None
    assert store.device_for_access_token(tokens.access_token, now=2).node_id == "homelink-abcdef"
    assert store.device_for_access_token("wrong", now=2) is None


def test_pairing_codes_expire(tmp_path):
    store = Store.load(tmp_path)
    code = store.new_pair_code(now=0)
    assert store.pair(code, "homelink-abcdef", "pi", now=PAIR_CODE_TTL_S + 1) is None


def test_only_hashes_reach_the_disk_and_devices_reload(tmp_path):
    store = Store.load(tmp_path)
    tokens = store.pair(store.new_pair_code(now=0), "homelink-abcdef", "pi", now=0)
    saved = (tmp_path / "devices.json").read_text()
    assert tokens.access_token not in saved and tokens.refresh_token not in saved
    assert (tmp_path / "devices.json").stat().st_mode & 0o777 == 0o600
    reloaded = Store.load(tmp_path)
    assert reloaded.device_for_access_token(tokens.access_token, now=1).name == "pi"
    assert json.loads(saved)["devices"]["homelink-abcdef"]["name"] == "pi"


def test_access_tokens_expire_and_refresh_rotates_them(tmp_path):
    store = Store.load(tmp_path)
    first = store.pair(store.new_pair_code(now=0), "homelink-abcdef", "pi", now=0)
    assert store.device_for_access_token(first.access_token, now=ACCESS_TOKEN_TTL_S + 1) is None
    # The gadgets send the refresh token with a prefix.
    second = store.refresh(f"hatch_refresh:{first.refresh_token}", "homelink-abcdef", now=10)
    assert second is not None and second.access_token != first.access_token
    assert store.device_for_access_token(first.access_token, now=11) is None
    assert store.device_for_access_token(second.access_token, now=11) is not None
    assert store.refresh(second.refresh_token, "homelink-other", now=12) is None
    assert store.refresh("not-a-token", "homelink-abcdef", now=12) is None


def test_the_previous_refresh_token_survives_a_lost_rotation(tmp_path):
    store = Store.load(tmp_path)
    first = store.pair(store.new_pair_code(now=0), "homelink-abcdef", "pi", now=0)
    store.refresh(first.refresh_token, "homelink-abcdef", now=1)  # the gadget never hears back
    assert store.refresh(first.refresh_token, "homelink-abcdef", now=2) is not None


def test_vm_tokens_expire_and_die_with_the_pairing(tmp_path):
    store = Store.load(tmp_path)
    store.pair(store.new_pair_code(now=0), "homelink-abcdef", "pi", now=0)
    token = store.new_vm_token("homelink-abcdef", now=0)
    assert store.device_for_vm_token(token, now=1).node_id == "homelink-abcdef"
    assert store.device_for_vm_token(token, now=VM_TOKEN_TTL_S + 1) is None
    token = store.new_vm_token("homelink-abcdef", now=0)
    assert store.unpair("homelink-abcdef")
    assert store.device_for_vm_token(token, now=1) is None


def test_the_admin_token_is_stable_and_private(tmp_path):
    store = Store.load(tmp_path)
    assert store.admin_token() == Store.load(tmp_path).admin_token()
    assert (tmp_path / "admin_token").stat().st_mode & 0o777 == 0o600
