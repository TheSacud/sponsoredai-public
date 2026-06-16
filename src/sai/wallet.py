from __future__ import annotations

import json
import math
import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import runtime_paths, utc_now_iso, write_json_atomic


# Serializes mutations between gateway threads; the sidecar file lock in
# _locked() covers concurrent processes (CLI runner + gateway).
_THREAD_LOCK = threading.Lock()


class WalletError(RuntimeError):
    pass


class InsufficientCredits(WalletError):
    pass


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    timestamp: str
    kind: str
    amount: float
    source: str
    session_id: str | None
    metadata: dict[str, Any]


def _lock_file(fh: Any) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file(fh: Any) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class Wallet:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_paths().wallet_file
        # Lock a sidecar file: locking wallet.json itself would block the
        # atomic os.replace() that save() performs on Windows.
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _THREAD_LOCK:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as lock_fh:
                _lock_file(lock_fh)
                try:
                    yield
                finally:
                    _unlock_file(lock_fh)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "ledger": []}
        try:
            # utf-8-sig: tolerate a BOM left by Windows editors.
            with self.path.open("r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise WalletError(f"Wallet file is corrupt: {self.path} ({exc})") from exc
        if not isinstance(data, dict):
            raise WalletError(f"Wallet file is corrupt: {self.path} (expected a JSON object)")
        if "ledger" not in data or not isinstance(data["ledger"], list):
            data["ledger"] = []
        return data

    def save(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.path, data)

    def balance(self) -> float:
        return self._balance_of(self.load())

    def entries(self) -> list[dict[str, Any]]:
        return list(self.load()["ledger"])

    def today_earned(self) -> float:
        return self._today_earned(self.load())

    def record(
        self,
        kind: str,
        amount: float,
        source: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        if kind not in {"earn", "spend", "expire", "adjust"}:
            raise ValueError(f"Unsupported ledger entry kind: {kind}")
        if not math.isfinite(float(amount)):
            raise ValueError("Ledger amount must be finite")
        with self._locked():
            data = self.load()
            return self._append(data, kind, amount, source, session_id, metadata)

    def adjust_to_balance(
        self,
        target_balance: float,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[float, float, LedgerEntry | None]:
        """Atomically append an adjust entry that aligns the local balance."""
        if not math.isfinite(float(target_balance)):
            raise ValueError("Target balance must be finite")
        with self._locked():
            data = self.load()
            local_before = self._balance_of(data)
            delta = round(float(target_balance) - local_before, 6)
            if delta == 0:
                return local_before, local_before, None
            entry_metadata = dict(metadata or {})
            entry_metadata.setdefault("local_balance_before", local_before)
            entry = self._append(data, "adjust", delta, source, None, entry_metadata)
            return local_before, self._balance_of(data), entry

    def earn(
        self,
        amount: float,
        source: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        daily_cap: float | None = None,
    ) -> LedgerEntry | None:
        """Append a local display earning.

        daily_cap is accepted for backward compatibility with older configs,
        but payout limits must be enforced by the authoritative backend.
        """
        if not math.isfinite(float(amount)):
            raise ValueError("Earn amount must be finite")
        if amount <= 0:
            return None
        with self._locked():
            data = self.load()
            payable = round(float(amount), 6)
            if payable <= 0:
                return None
            return self._append(data, "earn", payable, source, session_id, metadata)

    def spend(
        self,
        amount: float,
        source: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        if not math.isfinite(float(amount)):
            raise ValueError("Spend amount must be finite")
        if amount <= 0:
            raise ValueError("Spend amount must be positive")
        with self._locked():
            data = self.load()
            if self._balance_of(data) < amount:
                raise InsufficientCredits("Not enough SAI credits")
            return self._append(data, "spend", -amount, source, session_id, metadata)

    def spend_up_to(
        self,
        amount: float,
        source: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry | None:
        """Spend at most the available balance instead of failing the charge."""
        if not math.isfinite(float(amount)):
            raise ValueError("Spend amount must be finite")
        if amount <= 0:
            return None
        with self._locked():
            data = self.load()
            payable = round(min(float(amount), self._balance_of(data)), 6)
            if payable <= 0:
                return None
            return self._append(data, "spend", -payable, source, session_id, metadata)

    def _append(
        self,
        data: dict[str, Any],
        kind: str,
        amount: float,
        source: str,
        session_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> LedgerEntry:
        if not math.isfinite(float(amount)):
            raise ValueError("Ledger amount must be finite")
        entry = LedgerEntry(
            id=f"led_{secrets.token_urlsafe(12)}",
            timestamp=utc_now_iso(),
            kind=kind,
            amount=round(float(amount), 6),
            source=source,
            session_id=session_id,
            metadata=metadata or {},
        )
        data["ledger"].append(entry.__dict__)
        self.save(data)
        return entry

    @staticmethod
    def _balance_of(data: dict[str, Any]) -> float:
        total = 0.0
        for entry in data["ledger"]:
            amount = float(entry.get("amount", 0))
            if not math.isfinite(amount):
                raise WalletError("Wallet file is corrupt: ledger amount must be finite")
            total += amount
        return round(total, 6)

    @staticmethod
    def _today_earned(data: dict[str, Any]) -> float:
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for entry in data["ledger"]:
            if entry.get("kind") != "earn":
                continue
            try:
                ts = datetime.fromisoformat(entry["timestamp"]).date()
            except (KeyError, ValueError, TypeError):
                continue
            if ts == today:
                amount = float(entry.get("amount", 0))
                if not math.isfinite(amount):
                    raise WalletError("Wallet file is corrupt: ledger amount must be finite")
                total += amount
        return round(total, 6)
