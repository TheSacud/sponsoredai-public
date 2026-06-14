# SponsoredAI Public Trust Boundary

This repository is the public transparency surface for SAI - Sponsored AI
Credits.

SAI turns coding-agent wait states into sponsor-funded AI credits. This public
repo does not contain the full private implementation. It documents the data
boundary, the npm launcher surface, and the event schema SAI uses to prove that
prompts, source code, terminal output, model responses, screenshots, and window
contents are not sent to SAI or sponsors.

Public service:

```text
https://sponsoredai.dev
```

Trust boundary:

```text
https://sponsoredai.dev/trust
```

## What This Repo Contains

- `TRUST.md`: the public data-boundary contract.
- `SECURITY.md`: vulnerability and privacy-boundary report process.
- `schemas/`: public placement-event schemas and examples.
- `npm/`: the small npm launcher package surface.
- `site/trust.html`: the static public trust page.

## What This Repo Does Not Contain

This repo intentionally does not publish:

- backend anti-abuse internals
- fraud thresholds
- sponsor accounting internals
- placement issuance and signature internals
- private deployment config
- production secrets or environment values
- code that would make qualified-placement spoofing easier

## Install

```bash
npm install -g @sponsoredai/cli
sai claude
sai overlay both
```

The desktop overlay release has been tested locally on Windows x64 and macOS
arm64 with Claude Desktop and the Codex app. Linux remains supported for the
terminal CLI package, but the desktop overlay is not claimed as a tested Linux
surface for this release.

## Trust Boundary

SAI measures the wait, not the work.

SAI does not upload:

- prompts
- source code
- model responses
- terminal output or logs
- commands or shell history
- full file paths
- repository URLs
- screenshots
- window titles or contents

Inspect the local event schema:

```bash
sai privacy schema
```

Stop every sponsor surface:

```bash
sai config kill-switch on --reason "local preview"
sai config set frequency off
```

## npm Launcher

The public npm package is a small launcher. npm installs the matching optional
platform binary package from the registry, then `bin/sai.js` resolves and
executes that binary. There is no separate binary download during install.

```text
macOS arm64 -> @sponsoredai/cli-darwin-arm64
Linux x64   -> @sponsoredai/cli-linux-x64
Windows x64 -> @sponsoredai/cli-win32-x64
```

## License

MIT. See `LICENSE`.
