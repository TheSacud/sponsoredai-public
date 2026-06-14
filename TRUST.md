# SAI Trust Boundary

SAI is designed to measure agent wait states, not developer work.

This document is the public data-boundary contract for the CLI, desktop overlay,
sponsor placements, and credit gateway. It is intentionally narrower than the
full implementation, so users can understand what SAI can and cannot send before
more of the project is opened.

## Short Version

- Prompts are not uploaded.
- Source code is not uploaded.
- Terminal output, model responses, shell history, repository URLs, and full file
  paths are not uploaded.
- The terminal runner measures output timing, not terminal content.
- The desktop overlay checks local window visibility and recent input, then sends
  only a small allowlisted placement event.
- Sponsors receive aggregate campaign reporting, not developer work context.
- Model calls made through the local gateway go from the developer machine to the
  selected provider; SAI does not proxy prompt or response bodies for wallet
  spend.

## Current Release Surface

The overlay release has been tested locally on:

- Windows x64 with the Codex app and Claude Desktop.
- macOS arm64 with the Codex app and Claude Desktop.

The terminal wrapper remains available for:

- Claude Code with `sai claude`.
- Codex CLI with `sai codex`.
- Arbitrary terminal commands with `sai run -- <command>`.

Linux is supported for the terminal CLI package. The desktop overlay is not being
claimed as a tested Linux surface for this release.

## What Stays Local

SAI does not upload:

- Prompts.
- Source code.
- Model responses.
- Terminal logs or terminal output.
- Commands or shell history.
- Full file paths.
- Repository URLs.
- Window titles.
- Window contents.
- Screenshots or screen recordings.
- Clipboard contents.

The desktop overlay reads local operating-system window state only to decide
whether a placement can be shown and whether it can qualify. Those raw local
signals are not sent to sponsors.

## What Can Be Sent

Sponsor placement events are limited to operational metadata needed to issue,
bill, cap, and settle a placement:

```json
{
  "surface": "desktop_overlay",
  "tool": "codex",
  "event": "qualified_5s",
  "duration_bucket": "10-30s",
  "attended_interactive": true,
  "ci": false,
  "country": "PT",
  "code_uploaded": false,
  "prompt_uploaded": false,
  "logs_uploaded": false
}
```

Transport identifiers can also be sent:

- `install_id_hash`
- `session_id`
- `placement_id`
- `campaign_id`
- `cli_version`
- event signature fields
- click token, when the developer clicks a sponsor destination

These identifiers are for replay prevention, campaign accounting, caps, credit
settlement, and click attribution. They are not prompts, code, commands, paths,
terminal output, or model responses.

You can inspect the local schema with:

```bash
sai privacy schema
```

## How The Desktop Overlay Qualifies A Placement

The overlay can show one sponsor banner over a supported desktop app. For a
placement to qualify, SAI checks locally that:

- A supported app is the foreground app.
- The overlay banner is on screen.
- The banner is on the same monitor as the supported app.
- There was recent keyboard or mouse input.
- The banner stayed visible for at least five continuous seconds.
- Frequency and campaign caps allow the placement.

Only the allowlisted result is sent. SAI does not upload the foreground window
title, window contents, screenshots, or input events.

## Sponsor Logos And Destinations

Sponsor artwork is fetched and cached by the SAI backend before the client uses
it. The client does not contact arbitrary sponsor image hosts directly for banner
icons.

Sponsor destination clicks go through a tracked redirect so the campaign can
record eligible clicks and forward the developer to the sponsor URL. Sponsors do
not receive prompts, code, terminal output, or local file context from SAI.

## Gateway And Model Traffic

SAI credits can be spent through the local OpenAI-compatible gateway.

When wallet spend is used, SAI provisions or updates a provider key limit and the
developer machine calls the provider directly. Prompt and response bodies are not
proxied through the SAI backend for wallet spend.

If a developer explicitly configures their own upstream provider key, that local
provider configuration wins.

## Controls

Stop every sponsor surface:

```bash
sai config kill-switch on --reason "local preview"
```

Reduce or disable sponsor frequency:

```bash
sai config set frequency low
sai config set frequency off
```

Run overlay only for a selected desktop app:

```bash
sai overlay codex
sai overlay claude
sai overlay both
```

Uninstall:

```bash
npm uninstall -g @sponsoredai/cli
```

## What Sponsors See

Sponsors can see aggregate campaign reporting such as:

- served placements
- rendered placements
- qualified five-second placements
- eligible clicks
- spend
- remaining placement volume

Sponsors do not receive prompts, source code, terminal output, shell history,
repository URLs, full file paths, model responses, screenshots, window titles, or
window contents.

## What Is Not Open Yet

The full project is not being opened in one step. The first public trust surface
is the data boundary: what SAI sends, what it never sends, how users can inspect
the schema, and how they can stop sponsor surfaces.

Parts that are intentionally not published as a public trust proof yet include
backend anti-abuse internals, fraud thresholds, sponsor accounting internals, and
other details that would make qualified-placement spoofing easier.
