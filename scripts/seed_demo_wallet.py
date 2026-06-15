"""Seed a demo SAI_HOME with ledger data for dashboard screenshots.

Usage (PowerShell):
    $env:SAI_HOME="$env:TEMP\sai-demo"; $env:PYTHONPATH="src"
    python scripts\seed_demo_wallet.py
    python -m sai dashboard
"""
import random
from datetime import datetime, timedelta, timezone

from sai.config import login
from sai.wallet import Wallet

login(name="demo")
w = Wallet()
now = datetime.now(timezone.utc)
data = w.load()


def add(kind, amount, source, ts, meta):
    data["ledger"].append(
        {
            "id": f"led_demo{len(data['ledger'])}",
            "timestamp": ts.replace(microsecond=0).isoformat(),
            "kind": kind,
            "amount": round(amount, 6),
            "source": source,
            "session_id": "sess_demo",
            "metadata": meta,
        }
    )


sponsors = [
    ("sponsor:neon_preview_branches", "Neon", 0.006),
    ("sponsor:sentry_errors", "Sentry", 0.006),
    ("sponsor:infisical_secrets", "Infisical", 0.005),
]
random.seed(7)
t = now - timedelta(days=3, hours=2)
while t < now - timedelta(minutes=9):
    src, name, amt = random.choice(sponsors)
    add("earn", amt, src, t, {"sponsor": name, "event_count": random.randint(1, 4)})
    if random.random() < 0.45:
        tok = random.randint(300, 4200)
        add(
            "spend",
            -round(tok / 1000 * 0.004, 6),
            "gateway:model_call",
            t + timedelta(minutes=3),
            {
                "model": "gpt-4o-mini",
                "prompt_tokens": tok // 2,
                "completion_tokens": tok - tok // 2,
                "total_tokens": tok,
                "prompt_stored": False,
                "response_stored": False,
            },
        )
    t += timedelta(minutes=random.randint(40, 200))

add("earn", 0.006, "sponsor:sentry_errors", now - timedelta(minutes=6), {"sponsor": "Sentry", "event_count": 2})
add("earn", 0.005, "sponsor:infisical_secrets", now - timedelta(minutes=2), {"sponsor": "Infisical", "event_count": 1})
w.save(data)
print("entries:", len(data["ledger"]), "balance:", w.balance(), "today:", w.today_earned())
