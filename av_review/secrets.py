from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from av_review.constants import (
    CONNECTION_ENV_REMOTE,
    EMBY_ENV_REMOTE,
    MAGPIE_KEYS_REMOTE,
    SECRETS,
)
from av_review.util import load_env, scp_from


def _mode600(p: Path):
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def secrets_dir() -> Path:
    SECRETS.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS, stat.S_IRWXU)
    d = SECRETS / "cache"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, stat.S_IRWXU)
    return d


def refresh_secrets(need_emby: bool = True, need_magpie: bool = True, need_db: bool = False) -> Path:
    """Copy key files from Unraid into a 700 dir. Never print values."""
    d = secrets_dir()
    if need_emby:
        dest = d / "emby.env"
        scp_from(EMBY_ENV_REMOTE, str(dest))
        _mode600(dest)
    if need_magpie:
        dest = d / "caller-keys.json"
        scp_from(MAGPIE_KEYS_REMOTE, str(dest))
        _mode600(dest)
    if need_db:
        dest = d / "connection.env"
        scp_from(CONNECTION_ENV_REMOTE, str(dest))
        _mode600(dest)
    return d


def load_emby_key(d: Path | None = None) -> str:
    d = d or secrets_dir()
    key = load_env(d / "emby.env").get("EMBY_API_KEY") or ""
    if not key:
        raise RuntimeError("EMBY_API_KEY missing (read from findav emby.env, not from argv)")
    return key


def load_magpie_key(d: Path | None = None) -> str:
    d = d or secrets_dir()
    raw = json.loads((d / "caller-keys.json").read_text())
    items = raw if isinstance(raw, list) else raw.get("keys") or raw.get("caller_keys") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        secret = (it.get("secret") or it.get("key") or "").strip()
        if secret and it.get("enabled", True) is not False:
            return secret
    raise RuntimeError("magpie gateway key missing")
