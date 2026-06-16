from __future__ import annotations

from typing import Any


QP_UNIT = "qualified five-second attended placement"
QP_EVENT = "qualified_5s"
RENDERED_EVENT = "rendered"
CLICK_EVENT = "click"
CLICK_COST_MULTIPLIER = 50
QUALIFIED_VISIBLE_SECONDS = 5.0
PLACEMENT_TTL_SECONDS = 120

# Ad surfaces. Each attests attendance in its own truthful way: the terminal via
# terminal_interactive (a real interactive tty), GUI surfaces (the desktop overlay
# and the VS Code webview) via attended_interactive (a verified foreground +
# on-screen + user-present banner).
CLI_WAIT_SURFACE = "cli_agent_wait"
OVERLAY_SURFACE = "desktop_overlay"
VSCODE_WAIT_SURFACE = "vscode_ai_wait"

# Surfaces that are graphical (not a tty) and therefore attest with
# attended_interactive rather than terminal_interactive.
GUI_SURFACES = (OVERLAY_SURFACE, VSCODE_WAIT_SURFACE)


def surface_attended(payload: dict[str, Any]) -> bool:
    """Whether an event/placement payload truthfully attests that the user is
    attending an interactive session, for that payload's surface. A GUI surface
    can only qualify via attended_interactive (never terminal_interactive), and a
    terminal only via terminal_interactive -- so neither surface can borrow the
    other's attestation."""
    if payload.get("surface") in GUI_SURFACES:
        return bool(payload.get("attended_interactive"))
    return bool(payload.get("terminal_interactive"))

CAMPAIGN_REVIEW = "REVIEW"
CAMPAIGN_LIVE = "LIVE"
CAMPAIGN_QUEUED = "QUEUED"
CAMPAIGN_LIMITED = "LIMITED"
CAMPAIGN_DONE = "DONE"
CAMPAIGN_PAUSED = "PAUSED"

CAMPAIGN_STATUSES = {
    CAMPAIGN_REVIEW,
    CAMPAIGN_LIVE,
    CAMPAIGN_QUEUED,
    CAMPAIGN_LIMITED,
    CAMPAIGN_DONE,
    CAMPAIGN_PAUSED,
}


def metric_contract() -> dict[str, Any]:
    return {
        "unit": f"1 QP = 1 {QP_UNIT}",
        "qualified_event": QP_EVENT,
        "minimum_visible_seconds": QUALIFIED_VISIBLE_SECONDS,
        "placement_ttl_seconds": PLACEMENT_TTL_SECONDS,
        "billable_requirements": [
            "backend_issued_placement_id",
            "campaign_live_approved_and_funded",
            "rendered_while_attending_an_interactive_surface",
            "not_ci_or_headless",
            "card_visible_for_at_least_5_seconds",
            "event_received_before_placement_expiry",
            "not_duplicate_for_placement_id",
            "passes_basic_fraud_filters",
        ],
        "reporting_fields": [
            "served",
            "rendered",
            "qualified_5s",
            "billable",
            "invalid",
            "clicks",
        ],
        "paid_click": {
            "event": CLICK_EVENT,
            "cost_multiplier_vs_qp": CLICK_COST_MULTIPLIER,
            "requirements": [
                "placement_already_billable",
                "campaign_live_and_funded",
                "first_click_for_placement",
                "event_received_before_placement_expiry",
            ],
        },
    }
