from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__


APP_DIR_NAME = "SAI"
CONFIG_FILE = "config.json"
KILL_SWITCH_FILE = "kill_switch.json"
DEFAULT_BACKEND_URL = "https://sponsoredai.dev"
DEFAULT_FREQUENCY = "normal"
logger = logging.getLogger(__name__)

# Identify the CLI on outbound backend requests. The default urllib User-Agent
# ("Python-urllib/x.y") is rejected by the edge's bot/DoS filter, which would
# otherwise 403 every placement and spend call and silently break ads.
USER_AGENT = f"sai-cli/{__version__}"


class ConfigError(RuntimeError):
    pass

# idle_seconds: how long the terminal must be idle before the first card shows
#   in a wait (we only advertise during a genuine pause, not mid-output).
# rotate_seconds: how long each card stays pinned before the carousel advances
#   to the next placement. Rotation only fires while the terminal stays idle;
#   the moment output resumes the billing window closes and the timer is moot.
#   Each rotated creative bills independently, and only if it accrues the
#   qualified-visible seconds, so the cadence never inflates a single impression.
FREQUENCY_PROFILES = {
    "off": {"rotate_seconds": 10**9, "idle_seconds": 10**9},
    "low": {"rotate_seconds": 90, "idle_seconds": 20},
    "normal": {"rotate_seconds": 45, "idle_seconds": 10},
    "high": {"rotate_seconds": 25, "idle_seconds": 6},
}


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    config_file: Path
    wallet_file: Path
    kill_switch_file: Path


def sai_home() -> Path:
    override = os.environ.get("SAI_HOME")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
        return Path.home() / ".sai"

    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "sai"
    return Path.home() / ".sai"


def runtime_paths() -> RuntimePaths:
    home = sai_home()
    return RuntimePaths(
        home=home,
        config_file=home / CONFIG_FILE,
        wallet_file=home / "wallet.json",
        kill_switch_file=home / KILL_SWITCH_FILE,
    )


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json_atomic(path: Path, payload: dict[str, Any], private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    if private and os.name == "posix":
        os.chmod(tmp, 0o600)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": utc_now_iso(),
        "user_id": None,
        "api_key": None,
        "install_id": f"ins_{secrets.token_urlsafe(18)}",
        "device_id": f"dev_{secrets.token_urlsafe(18)}",
        "frequency": os.environ.get("SAI_DEFAULT_FREQUENCY", DEFAULT_FREQUENCY),
        "ads_enabled": True,
        "cloud_sync_enabled": False,
        "backend_url": os.environ.get("SAI_DEFAULT_BACKEND_URL", DEFAULT_BACKEND_URL),
        "country": None,
    }


def load_config() -> dict[str, Any]:
    paths = runtime_paths()
    if not paths.config_file.exists():
        return _default_config()
    try:
        # utf-8-sig: tolerate a BOM left by Windows editors.
        with paths.config_file.open("r", encoding="utf-8-sig") as fh:
            loaded = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is corrupt: {paths.config_file} ({exc})") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config file is corrupt: {paths.config_file} (expected a JSON object)")
    config = _default_config()
    config.update(loaded)
    if config.get("frequency") not in FREQUENCY_PROFILES:
        logger.warning("config invalid frequency value=%s fallback=%s", config.get("frequency"), DEFAULT_FREQUENCY)
        config["frequency"] = DEFAULT_FREQUENCY
    return config


def save_config(config: dict[str, Any]) -> None:
    # The config file holds the API key, so keep it private on POSIX.
    write_json_atomic(runtime_paths().config_file, config, private=True)


def ensure_config_saved() -> dict[str, Any]:
    config = load_config()
    if not runtime_paths().config_file.exists():
        save_config(config)
    return config


def store_install_secret(secret: str) -> dict[str, Any]:
    """Persist the backend-issued per-install secret as a credential in config.

    The secret authenticates sensitive backend calls and is returned only once at
    registration, so it must survive across runs. ``install_secret`` ends in
    ``_secret`` so ``sai config show`` already redacts it. Re-reads from disk
    before writing so it merges with any concurrent change, and no-ops when the
    stored value already matches."""
    config = load_config()
    if config.get("install_secret") == secret:
        return config
    config["install_secret"] = secret
    save_config(config)
    logger.info("install secret stored")
    return config


def login(email: str | None = None, name: str | None = None) -> dict[str, Any]:
    config = load_config()
    if not config.get("user_id"):
        config["user_id"] = f"user_{secrets.token_urlsafe(16)}"
    if not config.get("api_key"):
        config["api_key"] = f"sai_{secrets.token_urlsafe(32)}"
    if email:
        config["email_hint"] = email
    if name:
        config["name"] = name
    config["logged_in_at"] = utc_now_iso()
    save_config(config)
    logger.info("login refreshed has_email_hint=%s has_name=%s", bool(email), bool(name))
    return config


def set_frequency(value: str) -> dict[str, Any]:
    if value not in FREQUENCY_PROFILES:
        logger.warning("config frequency rejected value=%s", value)
        valid = ", ".join(sorted(FREQUENCY_PROFILES))
        raise ValueError(f"Invalid frequency '{value}'. Valid values: {valid}")
    config = load_config()
    old = config.get("frequency")
    config["frequency"] = value
    config["ads_enabled"] = value != "off"
    save_config(config)
    logger.info("config frequency changed old=%s new=%s ads_enabled=%s", old, value, config["ads_enabled"])
    return config


def kill_switch_active() -> bool:
    if os.environ.get("SAI_KILL_SWITCH", "").lower() in {"1", "true", "yes", "on"}:
        return True
    paths = runtime_paths()
    if not paths.kill_switch_file.exists():
        return False
    try:
        with paths.kill_switch_file.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except OSError:
        logger.warning("kill switch fail closed source=file error=os_error")
        return True
    except json.JSONDecodeError:
        logger.warning("kill switch fail closed source=file error=json_decode")
        return True
    return bool(data.get("active", False))


def set_kill_switch(active: bool, reason: str | None = None) -> None:
    payload = {"active": active, "updated_at": utc_now_iso(), "reason": reason}
    write_json_atomic(runtime_paths().kill_switch_file, payload)
    logger.info("kill switch updated active=%s reason_present=%s", active, bool(reason))


def interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def ci_environment() -> bool:
    keys = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD")
    return any(os.environ.get(key) for key in keys)
