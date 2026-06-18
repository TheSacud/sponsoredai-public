from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .ansi import (
    ACCENT,
    ARROW,
    BOLD,
    DIM,
    ELLIPSIS,
    GREEN,
    MIDDOT,
    RAIL,
    UNDERLINE,
    style,
    visible_length,
)
from .config import (
    FREQUENCY_PROFILES,
    USER_AGENT,
    ci_environment,
    kill_switch_active,
    store_install_secret,
)
from .metrics import (
    CLICK_EVENT,
    CLI_WAIT_SURFACE,
    GUI_SURFACES,
    OVERLAY_SURFACE,
    QP_EVENT,
    QUALIFIED_VISIBLE_SECONDS,
    RENDERED_EVENT,
    VSCODE_WAIT_SURFACE,
)
from .http_client import urlopen
from .privacy import sanitize_event
from .wallet import Wallet

logger = logging.getLogger(__name__)


NO_PLACEMENT_RETRY_SECONDS = 10.0

# Carousel AFK guard. A pinned card keeps cycling to the next placement every
# rotate_seconds while the terminal stays idle, so a walked-away session would
# bill a fresh impression forever. After this many rotations with no keypress in
# between, pause the carousel until the user interacts again. stdin is the only
# reliable presence signal (agent output means the agent is alive, not the user),
# so a present-but-silent reader also pauses after the cap and resumes on a key.
AFK_ROTATION_LIMIT = 3


@dataclass(frozen=True)
class SponsorCard:
    id: str
    sponsor: str
    message: str
    url: str
    credit_amount: float
    placement_id: str | None = None
    campaign_id: str | None = None
    bid_per_1000: float | None = None
    expires_at: str | None = None
    signature: str | None = None
    click_token: str | None = None
    brand_icon_url: str | None = None
    click_url: str | None = None

    @property
    def is_example(self) -> bool:
        return self.placement_id is None

    def footer(self, width: int | None = None) -> str:
        # An accent rail marks the sponsored zone; the sponsor name and link
        # carry the brand accent and the earned credit stays green, while the
        # "sponsored" tag, separators and link chrome stay dim. Example cards
        # render fully dim (no accent, no green) so they never look paid.
        if self.is_example:
            accent = DIM
            suffix = style("example placement - no paid demand", DIM)
        else:
            accent = ACCENT
            suffix = style(f"+{self.credit_amount:.3f} AI credits", GREEN, BOLD)
        rail = style(RAIL, accent)
        tag = style("sponsored", DIM)
        name = style(self.sponsor, accent, BOLD)
        sep = style(MIDDOT, DIM)
        link_label = hyperlink(display_url(self.url), self.click_url)
        if ARROW:
            link_label += f" {ARROW}"
        link = style(link_label, accent, UNDERLINE)
        head = f"{rail} {tag} {name} {sep} "
        tail = f" {sep} {link}  {suffix}"
        message = self.message
        if width is not None:
            # The sponsor name and credit amount are the card's value; when
            # space runs out, ellipsize the creative text instead.
            room = width - visible_length(head) - visible_length(tail)
            if len(message) > room:
                keep = room - len(ELLIPSIS)
                message = message[:keep].rstrip() + ELLIPSIS if keep >= 8 else ELLIPSIS
        return f"{head}{message}{tail}"


def display_url(url: str) -> str:
    """Compact form for the one-line card: host plus path, no scheme and no
    query string - tracking parameters are noise on a status line."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    compact = parsed.netloc + parsed.path.rstrip("/")
    return compact if len(compact) <= 40 else parsed.netloc


def hyperlink(display: str, target: str | None) -> str:
    """Wrap display text in an OSC 8 terminal hyperlink. Clicking it in a
    supporting terminal opens the tracked redirect, which records the paid
    click and forwards to the sponsor. Terminals without OSC 8 support show
    the plain text."""
    if not target or os.environ.get("SAI_NO_HYPERLINKS", "").lower() in {"1", "true", "yes", "on"}:
        return display
    return f"\x1b]8;;{target}\x1b\\{display}\x1b]8;;\x1b\\"


# Example cards shown when there is no backend or no paid demand. They credit
# nothing: every unit that lands in the wallet must be backed by sponsor spend.
LOCAL_SPONSORS = [
    SponsorCard(
        id="build_cache",
        sponsor="Your Brand",
        message="Ship faster agent workflows",
        url="https://sponsoredai.dev/sponsor",
        credit_amount=0.0,
    ),
    SponsorCard(
        id="qualified_waits",
        sponsor="Paid Sponsor",
        message="Reach developers during qualified waits",
        url="https://sponsoredai.dev/sponsor",
        credit_amount=0.0,
    ),
    SponsorCard(
        id="agent_sessions",
        sponsor="Launch Partner",
        message="Fund AI credits for real agent sessions",
        url="https://sponsoredai.dev/sponsor",
        credit_amount=0.0,
    ),
]


@dataclass
class ShownCard:
    card: SponsorCard
    event: dict[str, Any]
    shown_at: float
    visible_until: float | None = None
    settled: bool = False

    def visible_seconds(self, now: float) -> float:
        end = self.visible_until if self.visible_until is not None else now
        return max(0.0, end - self.shown_at)


class RemotePlacementClient:
    def __init__(
        self,
        base_url: str,
        install_id_hash: str,
        install_secret: str,
        timeout: float = 2.0,
        secret_is_issued: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.install_id_hash = install_id_hash
        self.install_secret = install_secret
        self.timeout = timeout
        # True once install_secret is the random secret the backend issued (as
        # opposed to the legacy install_id-derived fallback). Drives whether we
        # still ask the backend to issue one on register.
        self.secret_is_issued = secret_is_issued

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RemotePlacementClient | None":
        backend_url = config.get("backend_url")
        install_id = config.get("install_id")
        if not isinstance(backend_url, str) or not backend_url.strip() or not install_id:
            return None
        secret = resolve_install_secret(config)
        if not secret:
            return None
        issued = config.get("install_secret")
        return cls(
            backend_url,
            hash_install_id(str(install_id)),
            secret,
            secret_is_issued=bool(isinstance(issued, str) and issued.strip()),
        )

    def next_placement(
        self,
        tool: str,
        config: dict[str, Any],
        terminal_is_interactive: bool,
        surface: str = CLI_WAIT_SURFACE,
    ) -> SponsorCard | None:
        payload = {
            "install_id_hash": self.install_id_hash,
            "tool": tool,
            "cli_version": __version__,
            "country": config.get("country"),
            "surface": surface,
            "ci": ci_environment(),
        }
        # Each surface attests attendance with its own key; a GUI surface must
        # never claim terminal_interactive (a false attestation the backend's
        # fraud checks are built to catch).
        if surface in GUI_SURFACES:
            payload["attended_interactive"] = bool(terminal_is_interactive)
        else:
            payload["terminal_interactive"] = bool(terminal_is_interactive)
        register = self._post_or_none(
            "/v1/installations/register",
            # Ask for a server-issued secret until we hold one; once adopted this
            # stays False so the backend never re-issues.
            {**payload, "request_install_secret": not self.secret_is_issued},
            surface=surface,
        )
        if register is None:
            return None
        self._adopt_issued_secret(register)
        response = self._post_or_none("/v1/placements/next", payload, surface=surface)
        if response is None:
            return None

        placement = response.get("placement") if isinstance(response, dict) else None
        if not isinstance(placement, dict):
            logger.debug(
                "remote placement unavailable path=/v1/placements/next surface=%s reason=%s",
                surface,
                response.get("reason") if isinstance(response, dict) else "invalid_response",
            )
            return None
        placement_id = str(placement.get("placement_id") or "")
        campaign_id = str(placement.get("campaign_id") or "")
        creative = str(placement.get("creative") or "")
        sponsor = str(placement.get("sponsor") or campaign_id or "Sponsor")
        url = str(placement.get("url") or "")
        if not placement_id or not campaign_id or not creative or not url:
            return None
        bid_per_1000 = _float_or_none(placement.get("bid_per_1000"))
        credit_amount = _float_or_none(placement.get("credit_amount"))
        click_token = _optional_str(placement.get("click_token"))
        click_url = f"{self.base_url}/c/{placement_id}/{click_token}" if click_token else None
        return SponsorCard(
            id=placement_id,
            sponsor=sponsor,
            message=creative,
            url=url,
            credit_amount=credit_amount or 0.0,
            placement_id=placement_id,
            campaign_id=campaign_id,
            bid_per_1000=bid_per_1000,
            expires_at=str(placement.get("expires_at") or ""),
            signature=str(placement.get("signature") or ""),
            click_token=click_token,
            brand_icon_url=_optional_str(placement.get("brand_icon_url")),
            click_url=click_url,
        )

    def record_event(self, placement_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        payload = dict(payload)
        payload.setdefault("install_id_hash", self.install_id_hash)
        surface = str(payload.get("surface") or "")
        path = f"/v1/placements/{placement_id}/events"
        return self._post_or_none(path, payload, surface=surface)

    def _post_or_none(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        surface: str,
    ) -> dict[str, Any] | None:
        try:
            return self._post(path, payload)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            logger.info(
                "remote placement unavailable path=%s surface=%s error=%s",
                path,
                surface or "-",
                remote_error_detail(exc),
            )
            return None

    def _adopt_issued_secret(self, register_response: Any) -> None:
        """Switch to and persist the random secret the backend issues at
        registration, replacing the legacy install_id-derived fallback.

        The backend returns ``install_secret`` only the first time it mints one,
        so this runs once per install. Updating ``self.install_secret`` makes the
        rest of this session (placement fetch, event posts) authenticate with the
        issued secret immediately. Best effort: a config write failure leaves the
        derived fallback in place, which still authenticates during the migration
        window, and the next run retries."""
        if not isinstance(register_response, dict):
            return
        issued = register_response.get("install_secret")
        if not isinstance(issued, str) or not issued.strip():
            return
        issued = issued.strip()
        self.install_secret = issued
        self.secret_is_issued = True
        try:
            store_install_secret(issued)
        except OSError:
            pass

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        # The per-install secret authenticates this install to the backend. It
        # travels in the Authorization header (never the JSON body) so it cannot
        # be persisted in placement_events.payload_json or surface in logs that
        # capture request bodies.
        if self.install_secret:
            request.add_header("Authorization", f"{INSTALL_AUTH_SCHEME} {self.install_secret}")
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        return data


def remote_error_detail(exc: BaseException) -> str:
    """Compact, credential-free reason for backend transport failures."""
    if isinstance(exc, urllib.error.HTTPError):
        return _clip_error(f"HTTPError:{exc.code}:{exc.reason}")
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        return _clip_error(f"{exc.__class__.__name__}:{reason.__class__.__name__}:{reason}")
    return _clip_error(f"{exc.__class__.__name__}:{exc}")


def _clip_error(text: str, limit: int = 180) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "..."


def hash_install_id(install_id: str) -> str:
    return hashlib.sha256(install_id.encode("utf-8")).hexdigest()


# Authorization scheme that carries the per-install secret to the backend.
INSTALL_AUTH_SCHEME = "SAI-Install"
_INSTALL_AUTH_DERIVATION = b"sai-install-auth-v1"


def install_auth_secret(install_id: str) -> str:
    """Legacy per-install credential derived deterministically from ``install_id``.

    Superseded by the random secret the backend issues at registration (stored in
    config as ``install_secret`` and preferred by :func:`resolve_install_secret`).
    Because the open-source client's derivation is public, anyone who learns a
    machine's ``install_id`` can reproduce this secret, so it is kept only as the
    migration fallback for installs that have not yet rotated and will be removed
    once the migration window closes. The backend stores only
    ``sha256(install_secret)``; neither the secret nor ``install_id`` is
    recoverable from the public ``install_id_hash``."""
    return hmac.new(
        install_id.encode("utf-8"), _INSTALL_AUTH_DERIVATION, hashlib.sha256
    ).hexdigest()


def resolve_install_secret(config: dict[str, Any]) -> str | None:
    """The per-install credential to authenticate backend calls with.

    Prefers the random secret the backend issued at registration (persisted in
    config as ``install_secret``), falling back to the legacy
    :func:`install_auth_secret` derived from ``install_id`` for installs that have
    not rotated yet. Returns None when there is no install_id to derive from."""
    issued = config.get("install_secret")
    if isinstance(issued, str) and issued.strip():
        return issued.strip()
    install_id = config.get("install_id")
    if not install_id:
        return None
    return install_auth_secret(str(install_id))


def duration_bucket(seconds: float) -> str:
    if seconds < 10:
        return "0-10s"
    if seconds < 30:
        return "10-30s"
    if seconds < 120:
        return "30-120s"
    return "120s+"


def sponsor_enabled(config: dict[str, Any]) -> bool:
    if os.environ.get("SAI_DISABLE_SPONSORS", "").lower() in {"1", "true", "yes", "on"}:
        return False
    if kill_switch_active():
        return False
    if ci_environment():
        return False
    if not config.get("ads_enabled", True):
        return False
    if config.get("frequency") == "off":
        return False
    return True


class SponsorSession:
    def __init__(
        self,
        tool: str,
        config: dict[str, Any],
        wallet: Wallet | None = None,
        placement_client: RemotePlacementClient | None = None,
        surface: str = CLI_WAIT_SURFACE,
    ) -> None:
        self.id = f"sess_{secrets.token_urlsafe(12)}"
        self.tool = tool
        self.surface = surface
        self.config = config
        self.wallet = wallet or Wallet()
        self.placement_client = placement_client or RemotePlacementClient.from_config(config)
        self.qualified_waits = 0
        self.earned = 0.0
        self.cards: list[ShownCard] = []
        self.events: list[dict[str, Any]] = []
        # None instead of 0.0: time.monotonic() can start near zero on some
        # platforms, which would wrongly suppress the first card.
        self._last_card_at: float | None = None
        self._next_card_retry_at = 0.0
        # Cards shown since the user last touched stdin; gates the AFK carousel.
        self._cards_since_input = 0
        # Round-robin cursor for the no-backend example cards.
        self._example_index = 0

    @property
    def profile(self) -> dict[str, float]:
        name = self.config.get("frequency", "normal")
        return FREQUENCY_PROFILES.get(name, FREQUENCY_PROFILES["normal"])

    def maybe_card(self, now: float, idle_for: float, terminal_is_interactive: bool) -> SponsorCard | None:
        if not terminal_is_interactive:
            return None
        if idle_for < self.profile["idle_seconds"]:
            return None
        # Carousel cadence: once a card is pinned, keep it for rotate_seconds and
        # then, if the terminal is still idle, advance to the next placement. The
        # same gate throttles the first card of a fresh wait that arrives right
        # after the previous one, so a brief burst of output can't double-bill.
        if self._last_card_at is not None and now - self._last_card_at < self.profile["rotate_seconds"]:
            return None
        # AFK guard: once the carousel has rotated AFK_ROTATION_LIMIT times with
        # no keypress, stop so a walked-away terminal can't bill a fresh
        # impression every rotate_seconds. note_user_input resets the count.
        if self._cards_since_input >= AFK_ROTATION_LIMIT:
            return None
        if now < self._next_card_retry_at:
            return None
        # Checked last because it reads the kill-switch file from disk and this
        # method runs inside the 0.2s runner loop.
        if not sponsor_enabled(self.config):
            return None

        event_fields = {
            "surface": self.surface,
            "tool": self.tool,
            "event": "agent_thinking" if self.tool in {"codex", "claude"} else "command_wait",
            "duration_bucket": duration_bucket(idle_for),
            "ci": ci_environment(),
            "country": self.config.get("country"),
            "code_uploaded": False,
            "prompt_uploaded": False,
            "logs_uploaded": False,
        }
        if self.surface in GUI_SURFACES:
            event_fields["attended_interactive"] = bool(terminal_is_interactive)
        else:
            event_fields["terminal_interactive"] = bool(terminal_is_interactive)
        event = sanitize_event(event_fields)
        card = self._next_card(terminal_is_interactive)
        if card is None:
            self._next_card_retry_at = now + NO_PLACEMENT_RETRY_SECONDS
            return None

        self._next_card_retry_at = 0.0
        self.mark_cards_hidden(now)
        self.events.append(event)
        self.cards.append(ShownCard(card=card, event=event, shown_at=now))
        self._last_card_at = now
        self._cards_since_input += 1
        self._record_remote_event(card, event, RENDERED_EVENT, visible_seconds=0.0)
        return card

    def note_user_input(self) -> None:
        """The user touched stdin, so they are present: reset the AFK guard that
        pauses the carousel after AFK_ROTATION_LIMIT unattended rotations."""
        self._cards_since_input = 0

    def mark_cards_hidden(self, now: float | None = None) -> None:
        end = time.monotonic() if now is None else now
        for shown in self.cards:
            if shown.visible_until is None:
                shown.visible_until = end

    def reward_progress(self, now: float | None = None) -> dict[str, float | bool] | None:
        """Progress toward the five-second qualification bar for the active card.

        This is display-only. The backend remains the authority for whether a
        placement is actually billable.
        """
        current = next(
            (
                shown for shown in reversed(self.cards)
                if shown.visible_until is None and not shown.settled and not shown.card.is_example
            ),
            None,
        )
        if current is None:
            return None
        at = time.monotonic() if now is None else now
        visible_seconds = current.visible_seconds(at)
        remaining = max(0.0, QUALIFIED_VISIBLE_SECONDS - visible_seconds)
        progress = min(1.0, visible_seconds / QUALIFIED_VISIBLE_SECONDS)
        return {
            "visible_seconds": visible_seconds,
            "remaining_seconds": remaining,
            "progress": progress,
            "eligible": remaining <= 0.0,
        }

    def settle(self, now: float | None = None) -> float:
        settle_at = time.monotonic() if now is None else now
        earned = 0.0
        for shown in self.cards:
            if shown.settled:
                continue
            visible_seconds = shown.visible_seconds(settle_at)
            if visible_seconds < QUALIFIED_VISIBLE_SECONDS:
                continue
            qualification = self._qualification_result(shown, visible_seconds)
            if qualification is None:
                shown.settled = True
                continue
            shown.settled = True
            self.qualified_waits += 1
            confirmed_credit = _float_or_none(qualification.get("earned"))
            credit_amount = shown.card.credit_amount if confirmed_credit is None else confirmed_credit
            entry = self.wallet.earn(
                credit_amount,
                source=f"sponsor:{shown.card.id}",
                session_id=self.id,
                metadata={
                    "sponsor": shown.card.sponsor,
                    "event_count": len(self.events),
                    "placement_id": shown.card.placement_id,
                    "visible_seconds": round(visible_seconds, 3),
                },
            )
            if entry:
                earned += entry.amount
        self.earned = round(earned, 6)
        return self.earned

    def _next_card(self, terminal_is_interactive: bool) -> SponsorCard | None:
        if self.placement_client is not None:
            return self.placement_client.next_placement(
                self.tool, self.config, terminal_is_interactive, surface=self.surface
            )
        # No backend: cycle the example cards round-robin so the demo visibly
        # rotates through all of them instead of random.choice repeating one.
        card = LOCAL_SPONSORS[self._example_index % len(LOCAL_SPONSORS)]
        self._example_index += 1
        return card

    def _qualification_result(self, shown: ShownCard, visible_seconds: float) -> dict[str, Any] | None:
        if not shown.card.placement_id:
            return {"accepted": True, "billable": True, "earned": shown.card.credit_amount}
        response = self._record_remote_event(shown.card, shown.event, QP_EVENT, visible_seconds=visible_seconds)
        if response and response.get("billable"):
            return response
        logger.warning(
            "sponsor settle not billable tool=%s surface=%s session=%s placement=%s visible_seconds=%.3f reason=%s",
            self.tool,
            self.surface,
            self.id,
            shown.card.placement_id[:16] if shown.card.placement_id else "-",
            visible_seconds,
            (response or {}).get("invalid_reason") or "remote_unreachable",
        )
        return None

    def _record_remote_event(
        self,
        card: SponsorCard,
        event: dict[str, Any],
        placement_event: str,
        visible_seconds: float,
    ) -> dict[str, Any] | None:
        if not card.placement_id or self.placement_client is None:
            return None
        payload = {
            **{key: value for key, value in event.items() if key != "event"},
            "event": placement_event,
            "visible_seconds": round(visible_seconds, 3),
            "session_id": self.id,
            "campaign_id": card.campaign_id,
            "signature": card.signature,
        }
        if placement_event == CLICK_EVENT and card.click_token:
            payload["click_token"] = card.click_token
        return self.placement_client.record_event(card.placement_id, payload)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def external_event_fields(tool: str, surface: str, attended: bool, config: dict[str, Any]) -> dict[str, Any]:
    """Sanitized event fields for a placement event recorded from an external
    (non-carousel) surface such as the VS Code webview. The attestation key
    matches the surface so the backend's surface_attended check passes."""
    fields: dict[str, Any] = {
        "surface": surface,
        "tool": tool,
        "ci": ci_environment(),
        "country": config.get("country"),
        "code_uploaded": False,
        "prompt_uploaded": False,
        "logs_uploaded": False,
    }
    if surface in GUI_SURFACES:
        fields["attended_interactive"] = bool(attended)
    else:
        fields["terminal_interactive"] = bool(attended)
    return sanitize_event(fields)


def fetch_placement_card(
    config: dict[str, Any],
    *,
    tool: str,
    surface: str = VSCODE_WAIT_SURFACE,
    attended: bool = True,
) -> dict[str, Any]:
    """Fetch one placement for an external surface and record the ``rendered``
    event so the backend sets rendered_at_ts (a later ``qualified_5s`` requires a
    prior rendered plus a >=5s gap). Returns a JSON-serialisable dict with the
    card and the ticket fields the caller echoes back to qualify the impression,
    or ``{"placement": None, "reason": ...}``."""
    client = RemotePlacementClient.from_config(config)
    if client is None:
        return {"placement": None, "reason": "backend_unconfigured"}
    card = client.next_placement(tool, config, attended, surface=surface)
    if card is None or not card.placement_id:
        return {"placement": None, "reason": "no_placement"}
    session_id = f"sess_{secrets.token_urlsafe(12)}"
    event = external_event_fields(tool, surface, attended, config)
    # Best effort: a failed rendered event only means the qualifying event will
    # be rejected later (missing_rendered) -- never a wrong bill.
    client.record_event(
        card.placement_id,
        {
            **{key: value for key, value in event.items() if key != "event"},
            "event": RENDERED_EVENT,
            "visible_seconds": 0.0,
            "session_id": session_id,
            "campaign_id": card.campaign_id,
            "signature": card.signature,
        },
    )
    return {
        "placement": {
            "placement_id": card.placement_id,
            "campaign_id": card.campaign_id,
            "sponsor": card.sponsor,
            "message": card.message,
            "url": card.url,
            "click_url": card.click_url,
            "brand_icon_url": card.brand_icon_url,
            "credit_amount": card.credit_amount,
            "expires_at": card.expires_at,
            "signature": card.signature,
            "surface": surface,
            "tool": tool,
            "session_id": session_id,
        },
        "minimum_visible_seconds": QUALIFIED_VISIBLE_SECONDS,
    }


def record_placement_event(
    config: dict[str, Any],
    ticket: dict[str, Any],
    *,
    event: str = QP_EVENT,
    visible_seconds: float = 0.0,
    attended: bool = True,
) -> dict[str, Any]:
    """Record a placement event from an external surface. ``ticket`` is the
    placement dict returned by :func:`fetch_placement_card`, echoed back by the
    caller. ``attended`` is re-attested at event time (truthful at the moment of
    qualifying). Returns the backend response, or a ``{"billable": False, ...}``
    dict when the backend is unreachable or the ticket is incomplete."""
    client = RemotePlacementClient.from_config(config)
    if client is None:
        return {"billable": False, "reason": "backend_unconfigured"}
    placement_id = _optional_str(ticket.get("placement_id"))
    signature = _optional_str(ticket.get("signature"))
    if not placement_id or not signature:
        return {"billable": False, "reason": "incomplete_ticket"}
    surface = _optional_str(ticket.get("surface")) or VSCODE_WAIT_SURFACE
    tool = _optional_str(ticket.get("tool")) or "claude"
    fields = external_event_fields(tool, surface, attended, config)
    payload = {
        **{key: value for key, value in fields.items() if key != "event"},
        "event": event,
        "visible_seconds": round(float(visible_seconds), 3),
        "session_id": ticket.get("session_id"),
        "campaign_id": ticket.get("campaign_id"),
        "signature": signature,
    }
    if event == CLICK_EVENT:
        click_token = _optional_str(ticket.get("click_token"))
        if click_token:
            payload["click_token"] = click_token
    response = client.record_event(placement_id, payload)
    return response or {"billable": False, "reason": "remote_unreachable"}
