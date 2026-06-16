from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .config import USER_AGENT, load_config
from .sponsors import INSTALL_AUTH_SCHEME, hash_install_id, resolve_install_secret
from .wallet import Wallet


logger = logging.getLogger(__name__)

# 1 AI credit == 1 money unit == this many micros on the backend ledger. Kept
# local so the read path never imports the heavy backend module.
MICROS_PER_UNIT = 1_000_000

# Throttle the dashboard's per-poll reconciliation: the page polls /api/overview
# often, but the authoritative balance only moves when an earn/spend/clawback
# lands, so one backend round trip every interval is plenty.
DASHBOARD_SYNC_INTERVAL_SECONDS = 30.0

# The reconciliation entry's source tag. A stable, greppable label so the local
# ledger plainly shows which entries came from aligning to the backend.
RECONCILE_SOURCE = "backend:reconcile"

_BALANCE_FIELDS = ("pending_balance", "available_balance", "settled_balance", "revoked_balance")


def _is_credit_summary(payload: Any) -> bool:
    """True only for a genuine developer_credit_summary payload (not an error
    body the backend may return for an unregistered or unauthenticated install)."""
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("install_id_hash"), str)
        and "available_balance" in payload
    )


def _micros(summary: dict[str, Any], field: str) -> int:
    """Read a balance field in micros, preferring the exact ``*_micros`` integer
    and falling back to the rounded money figure."""
    raw = summary.get(f"{field}_micros")
    if isinstance(raw, bool):  # bool is an int subclass; never a balance
        raw = None
    if isinstance(raw, (int, float)):
        return int(raw)
    money = summary.get(field)
    if isinstance(money, (int, float)) and not isinstance(money, bool):
        return int(round(float(money) * MICROS_PER_UNIT))
    return 0


def authoritative_micros(summary: dict[str, Any]) -> int:
    """The live, spendable-or-maturing balance the backend recognises, in micros.

    pending + available + settled. Revoked credit is excluded because a clawback
    already removed it from those buckets, so it must not be re-added here."""
    return (
        _micros(summary, "pending_balance")
        + _micros(summary, "available_balance")
        + _micros(summary, "settled_balance")
    )


def authoritative_balance(summary: dict[str, Any]) -> float:
    return round(authoritative_micros(summary) / MICROS_PER_UNIT, 6)


def spendable_balance(summary: dict[str, Any]) -> float:
    """Credit the developer can spend right now: available + settled (pending is
    still maturing and therefore unspendable)."""
    micros = _micros(summary, "available_balance") + _micros(summary, "settled_balance")
    return round(micros / MICROS_PER_UNIT, 6)


def fetch_backend_credits(config: dict[str, Any] | None = None, timeout: float = 6.0) -> dict[str, Any] | None:
    """GET the authoritative developer credit summary for this install.

    Best-effort: returns None when the backend is unconfigured or unreachable,
    or when the response is not a real credit summary (e.g. the install has not
    registered yet). Authenticates with the per-install secret in the
    Authorization header, exactly like the spend and placement calls."""
    config = config if config is not None else load_config()
    backend_url = str(config.get("backend_url") or "").rstrip("/")
    install_id = config.get("install_id")
    if not backend_url or not install_id:
        return None
    install_id_hash = hash_install_id(str(install_id))
    secret = resolve_install_secret(config)
    url = f"{backend_url}/v1/developer/credits?install_id_hash={install_id_hash}"
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Authorization", f"{INSTALL_AUTH_SCHEME} {secret}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("Backend credit fetch failed route=%s error=%s", urlparse(url).path or "/", type(exc).__name__)
        return None
    return data if _is_credit_summary(data) else None


def reconcile_wallet(summary: dict[str, Any], wallet: Wallet | None = None) -> dict[str, Any]:
    """Align the local display ledger to the backend's authoritative balance.

    The backend owns earnings, spend, maturation and clawbacks; the local ledger
    historically only ever appended earnings, so it drifts upward the moment any
    spend or revocation happens. Rather than try to reconstruct each individual
    backend movement, post a single ``adjust`` entry equal to the gap between the
    local balance and the authoritative balance. The operation is idempotent: a
    second reconcile against an unchanged summary finds a zero gap and writes
    nothing."""
    wallet = wallet or Wallet()
    target_micros = authoritative_micros(summary)
    target_balance = round(target_micros / MICROS_PER_UNIT, 6)
    metadata = {
        "reason": "backend_reconcile",
        **{field: summary.get(field) for field in _BALANCE_FIELDS},
    }
    local_before, local_after, entry = wallet.adjust_to_balance(
        target_balance,
        source=RECONCILE_SOURCE,
        metadata=metadata,
    )
    local_micros = int(round(local_before * MICROS_PER_UNIT))
    delta_micros = int(round((local_after - local_before) * MICROS_PER_UNIT))
    if entry is not None:
        logger.info(
            "wallet reconciled delta_micros=%s local_before_micros=%s authoritative_micros=%s",
            delta_micros,
            local_micros,
            target_micros,
        )
    return {
        "authoritative_balance": round(target_micros / MICROS_PER_UNIT, 6),
        "spendable_balance": spendable_balance(summary),
        "local_balance_before": round(local_micros / MICROS_PER_UNIT, 6),
        "local_balance_after": local_after,
        "adjusted": entry.amount if entry is not None else 0.0,
        "reconciled": entry is not None,
    }


# Last successful sync result, so a non-networking caller (the dashboard render
# path) can surface the authoritative figures the CLI or gateway last confirmed.
_last_summary: dict[str, Any] | None = None
_last_sync = 0.0


def last_summary() -> dict[str, Any] | None:
    """The most recent confirmed backend summary, or None if never synced. Pure
    read of in-process state — never touches the network."""
    return _last_summary


def reset_cache_for_tests() -> None:
    global _last_summary, _last_sync
    _last_summary = None
    _last_sync = 0.0


def sync_local_wallet(
    config: dict[str, Any] | None = None,
    wallet: Wallet | None = None,
    timeout: float = 6.0,
) -> dict[str, Any] | None:
    """Fetch the backend summary and reconcile the local ledger to it.

    Caches the result so :func:`last_summary` can serve it without a round trip.
    Returns the summary enriched with the reconcile result, or None when the
    backend could not confirm a balance (unconfigured, unreachable, or the
    install is not registered yet)."""
    global _last_summary
    summary = fetch_backend_credits(config=config, timeout=timeout)
    if summary is None:
        return None
    result = reconcile_wallet(summary, wallet=wallet)
    enriched = {**summary, "reconcile": result}
    _last_summary = enriched
    return enriched


def maybe_sync_local_wallet(
    config: dict[str, Any] | None = None,
    wallet: Wallet | None = None,
    timeout: float = 2.0,
    min_interval: float = DASHBOARD_SYNC_INTERVAL_SECONDS,
) -> dict[str, Any] | None:
    """Throttled :func:`sync_local_wallet`. Returns the freshest known summary
    (re-fetching once per ``min_interval``, otherwise the cache). Never raises;
    a backend hiccup just leaves the last confirmed state in place."""
    global _last_sync
    now = time.monotonic()
    if _last_summary is not None and now - _last_sync < min_interval:
        return _last_summary
    _last_sync = now
    try:
        sync_local_wallet(config=config, wallet=wallet, timeout=timeout)
    except Exception:  # noqa: BLE001 - callers must never fail on a best-effort sync
        logger.exception("Background wallet reconcile failed")
    return _last_summary
