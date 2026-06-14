# SAI CLI

Install SAI from npm:

```bash
npm install -g @sponsoredai/cli
```

Then run your agent through SAI:

```bash
sai codex
sai claude
sai overlay both
```

You do not need to run `sai login` first. SAI creates its local install state
when it is needed. `sai login` is only useful if you explicitly want to print or
refresh the local SAI API key used by the local gateway/dashboard flow.

## Common Commands

```bash
sai codex
sai claude
sai overlay codex
sai overlay claude
sai overlay both
sai run -- npm test
sai wallet
sai config show
sai --help
```

## Trust Boundary

SAI measures the wait, not the work.

SAI does not upload prompts, source code, terminal output, model responses,
screenshots, window titles, window contents, full file paths, repository URLs,
commands, or shell history.

The terminal runner measures output timing. The desktop overlay checks local
window visibility and recent input before a placement can qualify. Only the
allowlisted placement event reaches the backend.

Inspect the local event schema:

```bash
sai privacy schema
```

Read the public trust boundary:

```text
https://sponsoredai.dev/trust
```

## How The npm Package Works

`@sponsoredai/cli` is a small launcher. npm also installs one matching optional
binary package for your machine:

```text
macOS arm64 -> @sponsoredai/cli-darwin-arm64
Linux x64   -> @sponsoredai/cli-linux-x64
Windows x64 -> @sponsoredai/cli-win32-x64
```

When you run `sai`, the launcher finds that platform package and executes the
bundled binary. There is no separate binary download during install.

## Requirements

- Node.js 18 or newer.
- npm optional dependencies enabled.
- macOS arm64, Linux x64, or Windows x64.

The desktop overlay release has been tested locally on Windows x64 and macOS
arm64 with Claude Desktop and the Codex app. Linux remains supported for the
terminal CLI package, but the desktop overlay is not claimed as a tested Linux
surface for this release.

Avoid installing with `--omit=optional` or `--no-optional`, because that skips
the platform binary package.

## Troubleshooting

If `sai` says `missing optional dependency`, reinstall normally:

```bash
npm uninstall -g @sponsoredai/cli
npm install -g @sponsoredai/cli
```

If it still fails, check that npm is not omitting optional dependencies:

```bash
npm config get optional
npm config get omit
```

`optional` should not be `false`, and `omit` should not include `optional`.
