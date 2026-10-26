# SPDX-License-Identifier: Apache-2.0

"""The hub's persistent state: paired devices, their tokens, and the admin token.

Only SHA-256 hashes of device tokens are stored. Pairing codes and VM bearers
are short-lived and stay in memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_DIR_ENV = "GADGETHUB_STATE_DIR"
DEFAULT_STATE_DIR = Path("/var/lib/gadgethub")
DEVICES_FILE = "devices.json"
ADMIN_TOKEN_FILE = "admin_token"

ACCESS_TOKEN_TTL_S = 12 * 3600
PAIR_CODE_TTL_S = 10 * 60
VM_TOKEN_TTL_S = 15 * 60
# The prefix the gadgets put in front of a refresh token. Kept for compatibility.
REFRESH_PREFIX = "hatch_refresh:"


def state_dir() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _matches(token: str, digest: str | None) -> bool:
    return bool(digest) and hmac.compare_digest(_hash(token), digest)


@dataclass
class Tokens:
    access_token: str
    refresh_token: str


@dataclass
class Device:
    node_id: str
    name: str
    paired_at: int
    access_hash: str = ""
    access_issued_at: int = 0
    refresh_hash: str = ""
    previous_refresh_hash: str = ""

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Store:
    directory: Path = field(default_factory=state_dir)
    devices: dict[str, Device] = field(default_factory=dict)
    _pair_codes: dict[str, float] = field(default_factory=dict)
    _vm_tokens: dict[str, tuple[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path | None = None) -> Store:
        store = cls(directory or state_dir())
        try:
            data = json.loads((store.directory / DEVICES_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        for node_id, record in (data.get("devices") or {}).items():
            store.devices[node_id] = Device(**record)
        return store

    def save(self) -> None:
        data = {"devices": {k: v.to_json() for k, v in sorted(self.devices.items())}}
        _write_private(self.directory / DEVICES_FILE, json.dumps(data, indent=2))

    # -- Admin ------------------------------------------------------------------

    def admin_token(self) -> str:
        """The token the CLI and the web chat present; created on first use."""
        path = self.directory / ADMIN_TOKEN_FILE
        try:
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except FileNotFoundError:
            pass
        token = secrets.token_urlsafe(32)
        _write_private(path, token + "\n")
        return token

    # -- Pairing ----------------------------------------------------------------

    def new_pair_code(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        self._expire(now)
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        self._pair_codes[_hash(code)] = now + PAIR_CODE_TTL_S
        return code

    def pair(self, code: str, node_id: str, name: str, now: float | None = None) -> Tokens | None:
        """Pair a device with a one-time code; None if the code is unknown or used."""
        now = time.time() if now is None else now
        self._expire(now)
        if self._pair_codes.pop(_hash(code.strip().upper()), None) is None:
            return None
        device = Device(node_id=node_id, name=name or node_id, paired_at=int(now))
        self.devices[node_id] = device
        tokens = self._issue(device, now)
        self.save()
        return tokens

    def unpair(self, node_id: str) -> bool:
        if self.devices.pop(node_id, None) is None:
            return False
        self._vm_tokens = {k: v for k, v in self._vm_tokens.items() if v[0] != node_id}
        self.save()
        return True

    # -- Tokens -----------------------------------------------------------------

    def device_for_access_token(self, token: str, now: float | None = None) -> Device | None:
        now = time.time() if now is None else now
        for device in self.devices.values():
            if _matches(token, device.access_hash):
                if now - device.access_issued_at > ACCESS_TOKEN_TTL_S:
                    return None
                return device
        return None

    def refresh(self, presented: str, node_id: str, now: float | None = None) -> Tokens | None:
        """Rotate a device's tokens. The refresh token before the current one
        still works, in case the device never received the last rotation."""
        now = time.time() if now is None else now
        token = presented.split(":", 1)[1] if presented.startswith(REFRESH_PREFIX) else presented
        device = self.devices.get(node_id)
        if device is None:
            return None
        if _matches(token, device.refresh_hash):
            device.previous_refresh_hash = device.refresh_hash
        elif not _matches(token, device.previous_refresh_hash):
            return None
        tokens = self._issue(device, now)
        self.save()
        return tokens

    def new_vm_token(self, node_id: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        self._expire(now)
        token = secrets.token_urlsafe(32)
        self._vm_tokens[_hash(token)] = (node_id, now + VM_TOKEN_TTL_S)
        return token

    def device_for_vm_token(self, token: str, now: float | None = None) -> Device | None:
        now = time.time() if now is None else now
        self._expire(now)
        entry = self._vm_tokens.get(_hash(token))
        return self.devices.get(entry[0]) if entry else None

    def _issue(self, device: Device, now: float) -> Tokens:
        tokens = Tokens(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        device.access_hash = _hash(tokens.access_token)
        device.access_issued_at = int(now)
        device.refresh_hash = _hash(tokens.refresh_token)
        return tokens

    def _expire(self, now: float) -> None:
        self._pair_codes = {k: v for k, v in self._pair_codes.items() if v > now}
        self._vm_tokens = {k: v for k, v in self._vm_tokens.items() if v[1] > now}


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text``, readable only by the owner."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
