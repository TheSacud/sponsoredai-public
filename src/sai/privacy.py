from __future__ import annotations

from typing import Any


ALLOWED_EVENT_KEYS = {
    "surface",
    "tool",
    "event",
    "duration_bucket",
    "terminal_interactive",
    "attended_interactive",
    "ci",
    "country",
    "code_uploaded",
    "prompt_uploaded",
    "logs_uploaded",
}

TRANSPORT_KEYS = {
    "install_id_hash",
    "cli_version",
    "session_id",
    "placement_id",
    "campaign_id",
    "signature",
    "click_token",
}

FORBIDDEN_EVENT_KEYS = {
    "api_key",
    "args",
    "argv",
    "command",
    "cwd",
    "email",
    "env",
    "environment",
    "environment_variables",
    "file_path",
    "prompt",
    "model_response",
    "hostname",
    "terminal_output",
    "file_paths",
    "git_remote",
    "repo_url",
    "source_code",
    "shell_history",
    "working_directory",
    "stdout",
    "stderr",
    "username",
}


def sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = sorted(set(payload) & FORBIDDEN_EVENT_KEYS)
    if forbidden:
        raise ValueError(f"Forbidden event keys present: {', '.join(forbidden)}")
    sanitized = {key: payload[key] for key in ALLOWED_EVENT_KEYS if key in payload}
    sanitized.setdefault("code_uploaded", False)
    sanitized.setdefault("prompt_uploaded", False)
    sanitized.setdefault("logs_uploaded", False)
    return sanitized


def public_event_schema() -> dict[str, Any]:
    return {
        "event_allowed_keys": sorted(ALLOWED_EVENT_KEYS),
        "transport_keys": sorted(TRANSPORT_KEYS),
        "forbidden_keys": sorted(FORBIDDEN_EVENT_KEYS),
        "guarantees": [
            "No code upload",
            "No prompt upload",
            "No model response upload",
            "No terminal log upload",
            "No full file paths",
            "No shell history",
        ],
    }
