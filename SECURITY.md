# Security And Privacy Boundary Reports

Please report SAI security issues, privacy-boundary issues, or cases where a
client appears to send prompts, code, terminal output, model responses,
screenshots, window contents, or other forbidden work context.

## What To Report

- A payload that includes forbidden fields such as `prompt`, `source_code`,
  `terminal_output`, `repo_url`, `file_path`, `stdout`, or `stderr`.
- A sponsor event that contains fields outside the public allowlist.
- A sponsor logo or destination flow that contacts a sponsor-controlled host
  directly before the user clicks.
- A gateway flow that proxies prompt or response bodies through SAI when wallet
  spend is expected to call the provider directly.
- A desktop overlay behavior that appears to qualify when the supported app is
  not visible or attended.

## What To Include

- SAI CLI version.
- Operating system and architecture.
- The command or surface used, for example `sai claude` or `sai overlay both`.
- A redacted event payload if available.
- Steps to reproduce.

Do not send private prompts, source code, API keys, terminal logs, screenshots,
or secrets in the initial report.

## Response Scope

This public repository documents the trust boundary and the npm launcher
surface. The private product repository contains backend anti-abuse,
accounting, placement issuance, and deployment internals that are not published
here because exposing them would make qualified-placement spoofing easier.
