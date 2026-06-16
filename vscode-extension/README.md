# SAI - Sponsored AI Credits

Launch SAI from VS Code and keep your wallet status visible while Claude Code and Codex wait.

## Features

- Status bar wallet state: `SAI: checking...`, `SAI: 3.420 credits eligible` (backend confirmed), `SAI: 3.420 credits (unconfirmed)` (local only), `SAI: CLI not found`, or `SAI: wallet unavailable`.
- The eligible figure is the backend-authoritative spendable balance (available + settled AI credits). When the backend cannot be confirmed, the status bar shows the local display balance and labels it `(unconfirmed)`.
- Command Palette commands for starting Codex, Claude, the SAI overlay, the wallet view, dashboard, refresh, and CLI install.
- Integrated terminal launchers for `sai codex`, `sai claude`, `sai overlay both`, and `sai dashboard`.
- Wallet refresh through `sai wallet --json` with a timeout. The status bar refreshes automatically once when VS Code finishes starting, and whenever you run a wallet command.
- Sponsor banner (the **SAI** activity-bar view): while Claude or Codex is waiting on the model, a sponsor placement appears in the side panel. It earns real credits when the wait qualifies (the card is held for at least five seconds while you are attending). This is the in-editor equivalent of the terminal/overlay sponsor card - VS Code does not let an extension change another extension's "thinking" spinner, so SAI shows the ad in its own view.
- VSIX packaging with `@vscode/vsce`.

## Commands

| Command | Behavior |
| --- | --- |
| `SAI: Start Codex` | Opens a VS Code integrated terminal and runs `sai codex`. |
| `SAI: Start Claude` | Opens a VS Code integrated terminal and runs `sai claude`. |
| `SAI: Start Overlay` | Opens a VS Code integrated terminal and runs `sai overlay both`. |
| `SAI: Wallet` | Runs `sai wallet --json` and shows balance, earned today, and recent entries. |
| `SAI: Refresh Wallet` | Refreshes the status bar wallet state. |
| `SAI: Open Dashboard` | Opens a VS Code integrated terminal and runs `sai dashboard`. |
| `SAI: Install CLI` | Asks for confirmation, then opens a visible terminal with `npm install -g @sponsoredai/cli`. |

Clicking the status bar opens:

```text
Start Codex
Start Claude
Start Overlay
Wallet
Open Dashboard
Install / Update CLI
```

## Install SAI CLI

This extension does not install the SAI CLI silently. Use `SAI: Install CLI` to confirm the install/update command in a visible integrated terminal:

```bash
npm install -g @sponsoredai/cli
```

## Install The Extension

After Marketplace publication, install `Sacud.sai-sponsored-ai-credits` from VS Code. Tagged GitHub releases also attach a `.vsix` package that can be installed with VS Code's "Install from VSIX..." command.

## Privacy And Security

The extension is a launcher, wallet, and sponsor surface only.

- It does not read workspace files.
- It does not read open editor text.
- It does not capture integrated terminal output.
- It does not read prompts, source code, model responses, logs, shell history, or repository URLs.
- It does not send telemetry.
- It does not run commands built from user-provided text.
- The wallet command is `sai wallet --json`, run with a timeout. It runs automatically once at startup and on wallet refresh. To locate `sai`, on Windows the extension also runs the system `where.exe sai` (and `cmd.exe` to invoke a resolved `sai.cmd`); the resolved path must be an absolute file under a trusted install location (your npm global directory or a system program directory) or it is not run. The extension may inspect candidate executable paths from `PATH` only to verify the CLI location. The optional `sai.cliPath` override is machine-scoped and workspace settings are ignored.
- For the sponsor banner, the extension polls the local SAI gateway's `http://127.0.0.1:8787/v1/status` for an in-flight request count only (never request content), and runs `sai placement next`/`sai placement event` to fetch a sponsor card and report a billable impression. Gateway host/port settings are machine-scoped, and the extension only connects to loopback hosts (`127.0.0.1`, `localhost`, or `::1`). The banner does not load remote sponsor images; sponsor clicks open only backend-issued HTTPS redirect URLs. Like the terminal and desktop-overlay surfaces, attendance is client-attested: credit only accrues when the window is focused, the banner is visible, and you produced genuine keyboard/mouse activity in the last 30 seconds (programmatic editor events and the agent's own file edits do not count). The backend independently enforces the five-second hold, surface pinning, and per-install caps, so the worst a spoofed client can do is bounded by those caps.
- CLI installation always requires confirmation and runs in a visible integrated terminal, started in your home directory so a workspace `.npmrc` cannot redirect the install.

SAI itself is designed to measure wait time, not your work. See the [SAI trust boundary](https://sponsoredai.dev/trust) for the full client behavior.

## Development

```bash
npm install
npm run compile
npm run lint
npm test
npx vsce package
```

The generated VSIX can be installed locally with VS Code's "Install from VSIX..." command.
