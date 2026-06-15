# SAI — Sponsored AI Credits (client)

SAI turns coding-agent wait states into sponsor-funded AI credits.

It wraps terminal AI workflows such as Codex CLI, Claude Code, and long-running
commands, and can run a small desktop overlay over supported apps such as Claude
Desktop and the Codex app. When an agent is waiting, SAI shows one quiet sponsor
placement, records a qualified view, and credits the developer through the
server-side earnings ledger.

```text
Service:        https://sponsoredai.dev
Trust boundary: https://sponsoredai.dev/trust
Source:         https://github.com/TheSacud/sponsoredai-public
```

> **This repository is the open-source SAI client** — the CLI, the gateway, and
> the desktop overlay that run on your machine. It is licensed under **AGPL-3.0**
> (see [`LICENSE`](LICENSE)). The hosted backend (sponsor server, billing, fraud
> and payout engine) is a separate, proprietary service operated by SAI; it is
> not part of this repository. The client talks to it over the documented public
> endpoints only.

## Trust and privacy boundary

The CLI and desktop overlay do not upload prompts, source code, model responses,
terminal logs, full file paths, repository URLs, shell history, screenshots,
window titles, or window contents.

The terminal runner measures output *timing*, not terminal *content*. The
desktop overlay checks local window visibility and recent input to decide whether
a placement can be shown and qualified. Raw window state stays on the machine.

Sponsor event metadata is limited to wait-state fields:

```json
{
  "surface": "cli_agent_wait",
  "tool": "codex",
  "event": "agent_thinking",
  "duration_bucket": "10-30s",
  "terminal_interactive": true,
  "ci": false,
  "country": "PT",
  "code_uploaded": false,
  "prompt_uploaded": false,
  "logs_uploaded": false
}
```

Desktop overlay events use the same allowlisted shape with `surface` set to
`desktop_overlay` and `attended_interactive` instead of `terminal_interactive`.

Requests may also include technical transport identifiers (`install_id_hash`,
`session_id`, `placement_id`, `campaign_id`, `signature`, `cli_version`). These
identify the install/session/placement; they never include prompts, code,
terminal output, file paths, commands, or shell history.

Inspect the schema locally — and read the source in this repo to verify it:

```bash
sai privacy schema
```

## Install

The public npm package is a small launcher. npm installs the matching optional
platform binary package from the registry; it does not contain the Python source
tree and does not download release binaries from a separate host.

```bash
npm install -g @sponsoredai/cli
sai claude
sai overlay both
```

## CLI

```bash
sai login
sai --version
sai wallet
sai run -- npm test
sai run -- pytest
sai codex
sai claude
sai overlay codex
sai overlay claude
sai overlay both
```

Sponsor cards only appear for interactive terminals or attended desktop overlays
over supported apps. CI and headless runs are left alone.

Use frequency settings or the kill switch to suppress sponsor surfaces:

```bash
sai config set frequency low
sai config set frequency off
sai config kill-switch on --reason "incident"
```

Override the backend URL for local testing:

```bash
sai config set backend-url http://127.0.0.1:8790
sai config set backend-url none
```

## Logs and error triage

SAI writes local application logs to a rotating file by default:

```bash
sai logs path
sai logs tail --lines 120
```

The default file is `SAI_HOME/logs/sai.log` (`%APPDATA%\SAI\logs\sai.log` on
Windows unless `SAI_HOME` is set). HTTP request bodies are not logged because
gateway traffic may contain prompts or API keys.

Useful overrides:

```bash
SAI_LOG_LEVEL=DEBUG
SAI_LOG_FILE=/var/log/sai/sai.log
SAI_LOG_FILE=stderr
SAI_LOG_FILE=off
```

## Qualified placement contract

The paid unit is fixed:

```text
1 QP = 1 qualified five-second placement
```

A placement is billable only when:

- The backend issued the `placement_id`.
- The campaign is live, approved, paid, and funded.
- The card rendered in an interactive terminal or attended desktop overlay.
- The run is not CI/headless.
- The card stayed visible for at least 5 seconds.
- The event arrived before placement expiry.
- The placement event is not a duplicate.
- Basic fraud filters pass.

The live contract is published, unauthenticated:

```bash
curl -fsS https://sponsoredai.dev/v1/metric-contract
```

### Paid clicks

The sponsor URL in the terminal card is an OSC 8 hyperlink to a tracked redirect,
`GET /c/<placement_id>/<click_token>`, which records the click and forwards to the
sponsor destination. A click bills the sponsor a click multiplier (default 50)
times the QP rate and pays the developer the same 60% net split. It only pays when
the placement already qualified, the campaign is live and funded, and it is the
first click for the placement; the redirect always forwards either way. Set
`SAI_NO_HYPERLINKS=1` to render the plain URL instead.

## Dashboard

```bash
sai dashboard
```

Serves the local ledger dashboard and opens `http://127.0.0.1:8787/`. It shows
local display entries, gateway connection details, sponsor-card frequency, and
the kill switch. The local ledger is not authoritative for spend, settlement, or
cash-out — the backend ledger is.

The dashboard and its `/api/overview` and `/api/config` endpoints are only served
to loopback clients with a localhost `Host` header, because they expose the local
API key.

## Gateway

`sai claude` and `sai codex` start the gateway automatically in the background on
the default port if nothing is listening there yet (set `SAI_NO_AUTO_GATEWAY=1` to
opt out). To run it manually:

```bash
sai login
sai gateway serve --host 127.0.0.1 --port 8787
```

Configure OpenAI-compatible clients with:

```text
base_url = "http://127.0.0.1:8787/v1"
api_key  = "<output from sai gateway key>"
```

With no upstream configured, the gateway returns a deterministic local mock. To
proxy a real provider, select a preset and set that provider's key:

```bash
export SAI_GATEWAY_PROVIDER="openai"
export OPENAI_API_KEY="..."
```

Built-in provider presets:

```text
provider    key env             base URL
openai      OPENAI_API_KEY      https://api.openai.com/v1
openrouter  OPENROUTER_API_KEY  https://openrouter.ai/api/v1
groq        GROQ_API_KEY        https://api.groq.com/openai/v1
mistral     MISTRAL_API_KEY     https://api.mistral.ai/v1
together    TOGETHER_API_KEY    https://api.together.ai/v1
fireworks   FIREWORKS_API_KEY   https://api.fireworks.ai/inference/v1
deepseek    DEEPSEEK_API_KEY    https://api.deepseek.com
xai         XAI_API_KEY         https://api.x.ai/v1
```

Inspect local provider status without printing secrets:

```bash
sai gateway providers
```

Custom OpenAI-compatible upstreams also work:

```bash
export SAI_UPSTREAM_BASE_URL="https://api.example.com/v1"
export SAI_UPSTREAM_API_KEY="..."
```

Server-side developer earnings are spendable on model calls without SAI ever
seeing model traffic: the gateway asks the backend for a per-installation provider
key whose cumulative spend limit equals the installation's spendable balance, then
model calls go straight from your machine to the provider with that key. Set
`SAI_NO_WALLET_SPEND=1` to opt out.

## Build from source

```bash
python -m pip install -e ".[test]"
PYTHONPATH=src python -m sai --help
python -m pytest
```

Build the standalone binary (per-OS; PyInstaller does not cross-compile):

```bash
python -m PyInstaller --onefile --name sai --paths src --exclude-module sai.backend \
  scripts/pyinstaller_entry.py
```

CI in `.github/workflows/` builds the Linux/macOS/Windows binaries and publishes
the npm launcher plus the platform packages on tagged releases.

## Implementation notes

The POSIX runner uses a real PTY and tracks output timing, not output content. On
Windows, a ConPTY compositor pins the sponsor line via `pywinpty`; without it, the
runner falls back to passthrough process execution.

The desktop overlay has been tested on Windows x64 and macOS arm64 with Claude
Desktop and the Codex app. Linux is supported for the terminal CLI; the desktop
overlay is not claimed as a tested Linux surface for this release.

## License

AGPL-3.0-or-later — see [`LICENSE`](LICENSE). Copyright © 2026 SAI.

The AGPL covers this client. Because SAI holds the copyright, SAI also operates a
proprietary hosted backend; the AGPL's network-use and copyleft terms bind
third-party redistributors, not the original author.
