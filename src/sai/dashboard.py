"""Local display ledger dashboard served by the gateway.

The page is a single self-contained HTML document (inline CSS/JS, no external
assets) so the gateway keeps working offline and adds no dependencies.
"""

from __future__ import annotations

import json
from typing import Any

from .config import kill_switch_active, load_config, runtime_paths
from .credits import last_summary, spendable_balance
from .wallet import Wallet

LEDGER_PAGE_SIZE = 40


def _kill_switch_reason() -> str | None:
    paths = runtime_paths()
    try:
        with paths.kill_switch_file.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) and reason else None


def overview_payload() -> dict[str, Any]:
    """Everything the dashboard renders, in one round trip.

    Only ever served to loopback clients: the payload includes the local API
    key so the page can offer copy-to-clipboard for client setup.
    """
    config = load_config()
    wallet = Wallet()
    entries = wallet.entries()

    total_earned = 0.0
    total_spent = 0.0
    for entry in entries:
        amount = float(entry.get("amount", 0))
        if entry.get("kind") == "earn":
            total_earned += amount
        elif entry.get("kind") == "spend":
            total_spent -= amount

    # Surface the authoritative backend figures the gateway/CLI last confirmed.
    # Read-only and in-process: the dashboard render path never blocks on the
    # network, so an offline page still renders the local ledger instantly.
    summary = last_summary()

    return {
        "balance": wallet.balance(),
        "today_earned": wallet.today_earned(),
        "total_earned": round(total_earned, 6),
        "total_spent": round(total_spent, 6),
        "entry_count": len(entries),
        "entries": list(reversed(entries[-LEDGER_PAGE_SIZE:])),
        "frequency": config.get("frequency", "normal"),
        "ads_enabled": bool(config.get("ads_enabled", True)),
        "kill_switch": kill_switch_active(),
        "kill_reason": _kill_switch_reason(),
        "user_id": config.get("user_id"),
        "api_key": config.get("api_key"),
        "wallet_path": str(runtime_paths().wallet_file),
        "local_wallet_authoritative": False,
        "gateway_spends_wallet": False,
        "backend_confirmed": summary is not None,
        "backend": _backend_block(summary),
    }


def _backend_block(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """The authoritative balance breakdown, trimmed to the figures the dashboard
    shows, or None when the backend has not confirmed a balance this session."""
    if summary is None:
        return None
    return {
        "spendable_balance": spendable_balance(summary),
        "pending_balance": summary.get("pending_balance"),
        "available_balance": summary.get("available_balance"),
        "settled_balance": summary.get("settled_balance"),
        "revoked_balance": summary.get("revoked_balance"),
    }


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>SAI &mdash; wallet</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23ffd83d'/%3E%3Ctext x='8' y='12.5' font-family='monospace' font-size='11' font-weight='700' text-anchor='middle'%3ES%3C/text%3E%3C/svg%3E">
<style>
  :root{
    --paper:#f3efe5;
    --card:#fbf8f1;
    --ink:#1b1812;
    --ink-soft:#6b6453;
    --rule:#d8d1bd;
    --rule-soft:rgba(27,24,18,.09);
    --accent:#ffd83d;
    --earn:#1d7a44;
    --spend:#bf3a2b;
    --mono:ui-monospace,"Cascadia Code","Cascadia Mono","JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{scrollbar-gutter:stable}
  body{
    font:14px/1.6 var(--mono);
    background:var(--paper);
    color:var(--ink);
    -webkit-font-smoothing:antialiased;
    overflow-x:clip;
  }
  /* desk surface: faint dot grid behind the paper cards */
  body::before{
    content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
    background-image:radial-gradient(rgba(27,24,18,.05) 1px,transparent 1px);
    background-size:26px 26px;
  }
  ::selection{background:var(--accent);color:var(--ink)}
  :focus-visible{outline:2px solid var(--ink);outline-offset:2px}
  a{color:inherit}
  button{font:inherit;color:inherit}
  code{overflow-wrap:anywhere}

  .label{font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-soft)}
  .muted{color:var(--ink-soft)}
  .num{font-variant-numeric:tabular-nums}

  /* ---------- top bar ---------- */
  header{
    position:sticky;top:0;z-index:10;
    background:var(--paper);
    border-bottom:1px solid var(--ink);
  }
  .bar{
    max-width:1180px;margin:0 auto;padding:14px 28px;
    display:flex;align-items:center;gap:14px;
  }
  .logo{
    background:var(--ink);color:var(--accent);
    padding:3px 9px;font-weight:700;letter-spacing:.1em;font-size:14px;
  }
  .wordmark{font-size:10px;letter-spacing:.26em;color:var(--ink-soft);text-transform:uppercase}
  .bar-right{margin-left:auto;display:flex;align-items:center;gap:12px;font-size:12px;min-width:0}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--earn);box-shadow:0 0 0 3px rgba(29,122,68,.16)}
  .dot.off{background:var(--spend);box-shadow:0 0 0 3px rgba(191,58,43,.16)}
  .chip-warn{
    font-size:10px;letter-spacing:.14em;padding:2px 8px;
    border:1.5px solid var(--spend);color:var(--spend);
    transform:rotate(-1.5deg);
  }

  main{max-width:1180px;margin:0 auto;padding:0 28px 72px}

  /* ---------- hero ---------- */
  .hero{
    display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:32px;
    align-items:end;padding:64px 0 40px;
  }
  .balance-figure{
    font-size:clamp(56px,9vw,108px);font-weight:700;
    letter-spacing:-.045em;line-height:1.05;
    font-variant-numeric:tabular-nums;
  }
  .balance-figure .hl{box-shadow:inset 0 -.2em 0 0 var(--accent)}
  .balance-unit{font-size:16px;font-weight:400;letter-spacing:0;color:var(--ink-soft);margin-left:10px}
  .hero-sub{margin-top:10px;color:var(--ink-soft)}

  .today{
    border:1px solid var(--ink);background:var(--card);padding:18px 20px;
  }
  .today-take{font-size:30px;font-weight:700;margin:6px 0 12px}
  .meter{position:relative;height:10px;background:transparent;overflow:hidden}
  .meter::before{
    content:"";position:absolute;inset:0;
    background:repeating-linear-gradient(90deg,var(--rule) 0 4px,transparent 4px 9px);
  }
  .meter-fill{
    position:absolute;inset:0;width:0%;background:var(--ink);
    transition:width .6s cubic-bezier(.22,.9,.3,1);
  }
  .meter-fill.capped{background:var(--accent);outline:1px solid var(--ink)}
  .today-cap{margin-top:8px;font-size:12px;color:var(--ink-soft)}

  /* ---------- stat tiles ---------- */
  .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:36px}
  .tile{border:1px solid var(--rule);background:var(--card);padding:16px 18px}
  .tile b{display:block;font-size:24px;font-weight:700;margin-top:6px}
  .tile .earn{color:var(--earn)}
  .tile .spend{color:var(--spend)}

  /* ---------- layout ---------- */
  .cols{display:grid;grid-template-columns:minmax(0,7fr) minmax(0,5fr);gap:28px;align-items:start}
  .stack{display:grid;gap:28px}

  .sec-h{
    display:flex;align-items:baseline;gap:12px;
    border-bottom:2px solid var(--ink);padding-bottom:8px;margin-bottom:4px;
  }
  .sec-h h2{font-size:12px;letter-spacing:.2em;text-transform:uppercase;font-weight:700;min-width:0}
  .sec-h .note{margin-left:auto;font-size:11px;color:var(--ink-soft)}

  .card{border:1px solid var(--rule);background:var(--card);padding:18px 20px}

  /* ---------- the tape (ledger) ---------- */
  .row{
    display:flex;align-items:baseline;gap:12px;
    padding:9px 2px;border-bottom:1px solid var(--rule-soft);
  }
  .row:last-child{border-bottom:0}
  .row .when{flex:0 0 96px;font-size:11px;color:var(--ink-soft)}
  .stamp{
    flex:0 0 auto;font-size:9px;font-weight:700;letter-spacing:.16em;
    padding:1px 7px;border:1.5px solid currentColor;border-radius:1px;
    transform:rotate(-1.5deg);text-transform:uppercase;
  }
  .stamp.earn{color:var(--earn)}
  .stamp.spend{color:var(--spend)}
  .stamp.other{color:var(--ink-soft)}
  .row .what{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
  .row .what small{color:var(--ink-soft);font-size:12px}
  .lead{flex:1 1 24px;border-bottom:2px dotted var(--rule);transform:translateY(-4px)}
  .amt{flex:0 0 auto;font-weight:700;font-variant-numeric:tabular-nums}
  .amt.earn{color:var(--earn)}
  .amt.spend{color:var(--spend)}
  .tape-empty{
    border:1px dashed var(--rule);padding:28px 22px;text-align:center;color:var(--ink-soft);
  }
  .tape-empty code{
    display:inline-block;margin-top:10px;padding:4px 10px;
    background:var(--paper);border:1px solid var(--rule);color:var(--ink);
  }
  .tape-more{margin-top:10px;font-size:11px;color:var(--ink-soft);text-align:right}

  /* ---------- controls ---------- */
  .seg{display:grid;grid-template-columns:repeat(4,1fr);margin-top:12px}
  .seg button{
    padding:8px 0;background:transparent;border:1px solid var(--ink);
    border-left-width:0;cursor:pointer;font-size:11px;letter-spacing:.12em;
    text-transform:uppercase;
  }
  .seg button:first-child{border-left-width:1px}
  .seg button:hover{background:var(--paper)}
  .seg button[aria-pressed="true"]{background:var(--accent);font-weight:700}
  .seg-desc{margin-top:10px;font-size:12px;color:var(--ink-soft);min-height:2.6em}

  .kill-row{display:flex;gap:16px;align-items:flex-start;margin-top:18px;padding-top:16px;border-top:1px dashed var(--rule)}
  .kill-row p{font-size:12px;color:var(--ink-soft);margin-top:2px}
  .switch{
    flex:0 0 auto;width:56px;height:30px;position:relative;cursor:pointer;
    border:2px solid var(--ink);background:var(--card);
  }
  .switch .knob{
    position:absolute;top:2px;left:2px;width:22px;height:22px;
    background:var(--ink);transition:left .15s ease;
  }
  .switch[aria-checked="true"]{background:var(--spend)}
  .switch[aria-checked="true"] .knob{left:28px;background:var(--paper)}
  .kill-reason{font-size:11px;color:var(--spend);margin-top:6px}

  /* ---------- connect receipt ---------- */
  .receipt{padding-bottom:8px}
  .kv{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed var(--rule)}
  .kv .label{flex:0 0 84px}
  .kv .val{
    flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    font-size:12.5px;font-variant-numeric:tabular-nums;
  }
  .btn{
    flex:0 0 auto;font-size:10px;letter-spacing:.14em;text-transform:uppercase;
    border:1px solid var(--ink);background:transparent;padding:4px 9px;cursor:pointer;
  }
  .btn:hover{background:var(--ink);color:var(--card)}
  .btn.ok{background:var(--earn);border-color:var(--earn);color:var(--card)}
  .snippet{
    margin-top:14px;background:var(--ink);color:#efe9d6;
    padding:14px 16px;font-size:12px;line-height:1.7;overflow-x:auto;
  }
  .snippet .c{color:#9a937d}
  .snippet .y{color:var(--accent)}
  .tear{
    margin-top:14px;text-align:center;font-size:11px;letter-spacing:.7em;
    color:var(--ink-soft);user-select:none;
    max-width:100%;overflow:hidden;white-space:nowrap;
  }

  /* ---------- privacy ---------- */
  .never{list-style:none;margin-top:4px}
  .never li{display:flex;align-items:baseline;gap:10px;padding:7px 0;border-bottom:1px solid var(--rule-soft)}
  .never li:last-child{border-bottom:0}
  .never s{text-decoration-color:var(--spend);text-decoration-thickness:2px}
  .never .lead{transform:translateY(-3px)}
  .never .no{font-size:10px;letter-spacing:.14em;color:var(--spend);font-weight:700}
  .privacy-note{margin-top:12px;font-size:12px;color:var(--ink-soft)}

  footer{
    max-width:1180px;margin:0 auto;padding:22px 28px 48px;
    display:flex;gap:16px;flex-wrap:wrap;align-items:baseline;
    border-top:1px solid var(--ink);font-size:11px;color:var(--ink-soft);
  }
  footer .path{font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
  footer .right{margin-left:auto}

  #toast{
    position:fixed;left:24px;bottom:24px;z-index:20;
    background:var(--ink);color:var(--paper);padding:10px 16px;
    font-size:12px;letter-spacing:.05em;
    opacity:0;transform:translateY(8px);transition:opacity .25s,transform .25s;
    pointer-events:none;
  }
  #toast.show{opacity:1;transform:none}

  @media (max-width:920px){
    .hero{grid-template-columns:1fr;align-items:start;padding-top:44px}
    .cols{grid-template-columns:1fr}
    .tiles{grid-template-columns:1fr}
    .sec-h{align-items:flex-start;flex-wrap:wrap}
    .sec-h .note{flex-basis:100%;margin-left:0}
    .bar,.main,main,footer{padding-left:18px;padding-right:18px}
  }
  @media (max-width:560px){
    .wordmark{display:none}
    .bar{align-items:flex-start;flex-wrap:wrap;gap:8px 10px}
    .bar-right{flex-basis:100%;margin-left:0;font-size:11px;flex-wrap:wrap}
    .hero{padding-top:34px}
    .balance-figure{font-size:clamp(42px,16vw,56px);letter-spacing:-.025em}
    .balance-unit{display:block;margin:4px 0 0}
    .today,.tile,.card{padding:16px}
    .sec-h{gap:6px 10px}
    .row{align-items:flex-start;flex-wrap:wrap;gap:5px 10px}
    .row .when{flex:0 0 auto}
    .row .what{order:3;flex:1 1 100%;white-space:normal;overflow-wrap:anywhere}
    .lead{display:none}
    .amt{margin-left:auto}
    .seg{grid-template-columns:repeat(2,1fr)}
    .seg button:nth-child(odd){border-left-width:1px}
    .seg button:nth-child(n+3){border-top-width:0}
    .kill-row{gap:12px}
    .kv{align-items:flex-start;flex-wrap:wrap}
    .kv .label{flex:1 1 100%}
    .kv .val{white-space:normal;overflow-wrap:anywhere}
    .never li{align-items:flex-start;flex-wrap:wrap}
    .never .no{margin-left:auto}
    .snippet{font-size:11.5px;padding:12px}
    .tear{letter-spacing:.32em}
    footer .right{margin-left:0}
  }
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{transition:none !important;animation:none !important}
  }
</style>
</head>
<body>

<header>
  <div class="bar">
    <span class="logo">SAI</span>
    <span class="wordmark">Sponsored AI Credits</span>
    <div class="bar-right">
      <span id="killChip" class="chip-warn" hidden>KILL SWITCH ON</span>
      <span id="statusDot" class="dot" aria-hidden="true"></span>
      <span id="statusText" class="muted num">gateway &middot; connecting&hellip;</span>
    </div>
  </div>
</header>

<main>
  <section class="hero">
    <div>
      <div class="label">Local ledger balance</div>
      <div class="balance-figure num"><span class="hl" id="balance">0.000</span><span class="balance-unit">credits</span></div>
      <p class="hero-sub">Display-only cache of sponsor events. The backend ledger is authoritative.</p>
    </div>
    <div class="today">
      <div class="label">Today&rsquo;s local take</div>
      <div class="today-take num" id="todayTake">+0.000</div>
      <div class="meter"><div class="meter-fill" id="meterFill"></div></div>
      <div class="today-cap num" id="todayCap">beta cap: USD 5/h per installation, applied by the backend</div>
    </div>
  </section>

  <section class="tiles">
    <div class="tile"><span class="label">Local earned all-time</span><b class="earn num" id="totalEarned">+0.000</b></div>
    <div class="tile"><span class="label">Legacy local spend entries</span><b class="spend num" id="totalSpent">&minus;0.000</b></div>
    <div class="tile"><span class="label">Entries on the tape</span><b class="num" id="entryCount">0</b></div>
  </section>

  <div class="cols">

    <section>
      <div class="sec-h"><h2>The tape</h2><span class="note num" id="tapeUpdated"></span></div>
      <div class="card">
        <div id="tape"></div>
        <div class="tape-empty" id="tapeEmpty" hidden>
          Nothing on the tape yet.<br>
          Let a long command idle and the first paid card will appear here:
          <code>sai run -- npm test</code>
        </div>
        <div class="tape-more num" id="tapeMore" hidden></div>
      </div>
    </section>

    <div class="stack">

      <section>
        <div class="sec-h"><h2>Sponsor cards</h2></div>
        <div class="card">
          <span class="label">Frequency</span>
          <div class="seg" role="group" aria-label="Sponsor card frequency" id="freqSeg">
            <button data-f="off" aria-pressed="false">off</button>
            <button data-f="low" aria-pressed="false">low</button>
            <button data-f="normal" aria-pressed="false">normal</button>
            <button data-f="high" aria-pressed="false">high</button>
          </div>
          <p class="seg-desc" id="freqDesc"></p>
          <div class="kill-row">
            <button class="switch" id="killBtn" role="switch" aria-checked="false" aria-label="Kill switch"><span class="knob"></span></button>
            <div>
              <span class="label" style="color:var(--ink)">Kill switch</span>
              <p>Hard stop for every sponsor surface. Overrides frequency until you flip it back.</p>
              <div class="kill-reason" id="killReason" hidden></div>
            </div>
          </div>
        </div>
      </section>

      <section>
        <div class="sec-h"><h2>Point your client here</h2></div>
        <div class="card receipt">
          <div class="kv">
            <span class="label">Base URL</span>
            <span class="val num" id="baseUrl"></span>
            <button class="btn" data-copy="baseUrl">copy</button>
          </div>
          <div class="kv">
            <span class="label">API key</span>
            <span class="val num" id="apiKey">&mdash;</span>
            <button class="btn" id="revealBtn">show</button>
            <button class="btn" data-copy="apiKey">copy</button>
          </div>
          <pre class="snippet" id="snippet"></pre>
          <div class="tear">&lowast; &lowast; &lowast; &lowast; &lowast; &lowast; &lowast; &lowast; &lowast; &lowast; &lowast; &lowast;</div>
        </div>
      </section>

      <section>
        <div class="sec-h"><h2>Never leaves your machine</h2></div>
        <div class="card">
          <ul class="never">
            <li><s>prompts</s><span class="lead"></span><span class="no">NEVER SENT</span></li>
            <li><s>source code</s><span class="lead"></span><span class="no">NEVER SENT</span></li>
            <li><s>file paths</s><span class="lead"></span><span class="no">NEVER SENT</span></li>
            <li><s>terminal output</s><span class="lead"></span><span class="no">NEVER SENT</span></li>
            <li><s>shell history</s><span class="lead"></span><span class="no">NEVER SENT</span></li>
          </ul>
          <p class="privacy-note">Sponsor earnings settle in the backend ledger. The gateway spends them through a per-install provider key spend-limited to your balance &mdash; model traffic never touches SAI. Inspect the contract yourself: <span class="num">sai privacy schema</span></p>
        </div>
      </section>

    </div>
  </div>
</main>

<footer>
  <span>SAI &middot; backend-authoritative sponsored earnings</span>
  <span class="path num" id="walletPath"></span>
  <span class="right">made for terminals</span>
</footer>

<div id="toast" role="status"></div>

<noscript><p style="max-width:1180px;margin:0 auto;padding:20px 28px">The dashboard needs JavaScript to read your local display ledger.</p></noscript>

<script>
(function(){
  "use strict";

  var FREQ_DESC = {
    off:    "Never. Cards stay off until you flip frequency back on.",
    low:    "At most one card every ~90 seconds of waiting.",
    normal: "At most one card every ~45 seconds of waiting.",
    high:   "At most one card every ~25 seconds of waiting."
  };

  var $ = function(id){ return document.getElementById(id); };
  var state = { overview: null, revealed: false, lastBalance: null, online: null };
  var reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function fmtCredits(n, signed){
    var v = Number(n) || 0;
    var s = Math.abs(v).toFixed(3);
    if (!signed) return v < 0 ? "−" + s : s;
    return (v < 0 ? "−" : "+") + s;
  }

  function animateNumber(el, from, to){
    if (reducedMotion || from === null || from === to){
      el.textContent = fmtCredits(to);
      return;
    }
    var t0 = performance.now(), dur = 650;
    function tick(t){
      var p = Math.min(1, (t - t0) / dur);
      var e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmtCredits(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  function fmtWhen(ts){
    var d = new Date(ts);
    if (isNaN(d)) return String(ts || "");
    var day = d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
    var time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return day + " " + time;
  }

  function entryMeta(e){
    var m = e.metadata || {};
    if (m.sponsor) return String(m.sponsor);
    if (m.model || m.total_tokens){
      var bits = [];
      if (m.model) bits.push(String(m.model));
      if (m.total_tokens) bits.push(m.total_tokens + " tok");
      return bits.join(" · ");
    }
    return "";
  }

  function renderTape(entries, total){
    var tape = $("tape");
    tape.textContent = "";
    $("tapeEmpty").hidden = entries.length > 0;
    var more = $("tapeMore");
    more.hidden = total <= entries.length;
    if (!more.hidden) more.textContent = "showing last " + entries.length + " of " + total;

    entries.forEach(function(e){
      var row = document.createElement("div");
      row.className = "row";

      var when = document.createElement("span");
      when.className = "when num";
      when.textContent = fmtWhen(e.timestamp);

      var kind = e.kind === "earn" ? "earn" : e.kind === "spend" ? "spend" : "other";
      var stamp = document.createElement("span");
      stamp.className = "stamp " + kind;
      stamp.textContent = e.kind;

      var what = document.createElement("span");
      what.className = "what";
      what.textContent = String(e.source || "").replace(":", " · ");
      var meta = entryMeta(e);
      if (meta){
        var small = document.createElement("small");
        small.textContent = " — " + meta;
        what.appendChild(small);
      }

      var lead = document.createElement("span");
      lead.className = "lead";

      var amt = document.createElement("span");
      amt.className = "amt num " + kind;
      amt.textContent = fmtCredits(e.amount, true);

      row.append(when, stamp, what, lead, amt);
      tape.appendChild(row);
    });
  }

  function maskKey(key){
    if (!key) return "— run `sai login` first";
    if (state.revealed) return key;
    return key.slice(0, 8) + "…" + key.slice(-4);
  }

  function baseUrl(){ return location.origin + "/v1"; }

  function renderSnippet(key){
    var snip = $("snippet");
    snip.textContent = "";
    function line(parts){
      parts.forEach(function(p){
        var span = document.createElement("span");
        if (p.cls) span.className = p.cls;
        span.textContent = p.t;
        snip.appendChild(span);
      });
      snip.appendChild(document.createTextNode("\n"));
    }
    line([{ t: "# any OpenAI-compatible client", cls: "c" }]);
    line([{ t: "base_url" }, { t: " = " }, { t: '"' + baseUrl() + '"', cls: "y" }]);
    line([{ t: "api_key " }, { t: " = " }, { t: '"' + (key ? maskKey(key) : "sai_…") + '"', cls: "y" }]);
  }

  function render(o){
    state.overview = o;

    animateNumber($("balance"), state.lastBalance, o.balance);
    state.lastBalance = o.balance;

    $("todayTake").textContent = fmtCredits(o.today_earned, true);
    var pct = o.today_earned > 0 ? 100 : 0;
    var fill = $("meterFill");
    fill.style.width = pct.toFixed(1) + "%";
    fill.classList.toggle("capped", false);
    $("todayCap").textContent = "beta cap: USD 5/h per installation - backend ledger is authoritative";

    $("totalEarned").textContent = "+" + fmtCredits(o.total_earned);
    $("totalSpent").textContent = "−" + fmtCredits(o.total_spent);
    $("entryCount").textContent = o.entry_count;

    renderTape(o.entries || [], o.entry_count);
    $("tapeUpdated").textContent = "updated " + new Date().toLocaleTimeString();

    document.querySelectorAll("#freqSeg button").forEach(function(b){
      b.setAttribute("aria-pressed", String(b.dataset.f === o.frequency));
    });
    $("freqDesc").textContent = FREQ_DESC[o.frequency] || "";

    $("killBtn").setAttribute("aria-checked", String(!!o.kill_switch));
    $("killChip").hidden = !o.kill_switch;
    var reason = $("killReason");
    reason.hidden = !(o.kill_switch && o.kill_reason);
    if (!reason.hidden) reason.textContent = "reason: " + o.kill_reason;

    $("baseUrl").textContent = baseUrl();
    $("apiKey").textContent = maskKey(o.api_key);
    renderSnippet(o.api_key);
    $("walletPath").textContent = o.wallet_path || "";
  }

  function setOnline(ok){
    if (state.online === ok) return;
    state.online = ok;
    $("statusDot").classList.toggle("off", !ok);
    $("statusText").textContent = "gateway · " + location.host + " · " + (ok ? "online" : "unreachable");
  }

  var toastTimer = null;
  function toast(msg){
    var el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function(){ el.classList.remove("show"); }, 1800);
  }

  function refresh(){
    return fetch("/api/overview")
      .then(function(r){
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function(o){ render(o); setOnline(true); })
      .catch(function(){ setOnline(false); });
  }

  function postConfig(body, msg){
    return fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function(r){
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function(o){ render(o); setOnline(true); if (msg) toast(msg); })
      .catch(function(){ toast("could not save — gateway unreachable?"); });
  }

  $("freqSeg").addEventListener("click", function(ev){
    var btn = ev.target.closest("button[data-f]");
    if (!btn) return;
    postConfig({ frequency: btn.dataset.f }, "frequency set to " + btn.dataset.f);
  });

  $("killBtn").addEventListener("click", function(){
    var next = this.getAttribute("aria-checked") !== "true";
    postConfig({ kill_switch: next }, next ? "kill switch engaged" : "kill switch released");
  });

  $("revealBtn").addEventListener("click", function(){
    state.revealed = !state.revealed;
    this.textContent = state.revealed ? "hide" : "show";
    if (state.overview) {
      $("apiKey").textContent = maskKey(state.overview.api_key);
      renderSnippet(state.overview.api_key);
    }
  });

  document.querySelectorAll("[data-copy]").forEach(function(btn){
    btn.addEventListener("click", function(){
      var which = btn.dataset.copy;
      var text = which === "baseUrl" ? baseUrl() : (state.overview && state.overview.api_key) || "";
      if (!text) { toast("nothing to copy — run `sai login` first"); return; }
      navigator.clipboard.writeText(text).then(function(){
        btn.classList.add("ok");
        var prev = btn.textContent;
        btn.textContent = "copied";
        setTimeout(function(){ btn.classList.remove("ok"); btn.textContent = prev; }, 1200);
      }, function(){ toast("copy failed"); });
    });
  });

  refresh();
  setInterval(refresh, 6000);
})();
</script>
</body>
</html>
"""
