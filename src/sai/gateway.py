from __future__ import annotations

import hmac
import hashlib
import json
import logging
import math
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .app_logging import configure_logging, log_destination_label
from .config import USER_AGENT, load_config, save_config, set_frequency, set_kill_switch
from .credits import sync_local_wallet
from .dashboard import DASHBOARD_HTML, overview_payload
from .metrics import QP_EVENT, VSCODE_WAIT_SURFACE
from .sponsors import (
    INSTALL_AUTH_SCHEME,
    fetch_placement_card,
    hash_install_id,
    record_placement_event,
    resolve_install_secret,
)


MOCK_MODEL = "sai/mock-gpt-4o-mini"
WALLET_SPEND_PROVIDER = "sai-credits"
WALLET_SPEND_BASE_URL = "https://openrouter.ai/api/v1"
SPEND_SYNC_INTERVAL_SECONDS = 30.0
# After the backend debits the ledger for our spend, pull the authoritative
# balance back down into the local display ledger. Throttled separately from the
# spend nudge so a clawback or maturation also lands without a model call.
WALLET_RECONCILE_INTERVAL_SECONDS = 30.0
DEFAULT_RATES = {
    "input_per_1k": 0.002,
    "output_per_1k": 0.006,
}
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_PLACEMENT_BODY_BYTES = 64 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
logger = logging.getLogger(__name__)


def _request_route(path: Any) -> str:
    route = urlparse(str(path or "")).path
    return route or "/"


def _log_id(value: Any, length: int = 16) -> str:
    text = str(value or "").strip()
    return text[:length] if text else "-"


def _log_hash(value: Any, length: int = 12) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


class UpstreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayProvider:
    name: str
    label: str
    base_url: str
    api_key_env: str


@dataclass(frozen=True)
class UpstreamConfig:
    provider: str
    base_url: str
    api_key_env: str
    api_key: str | None


GATEWAY_PROVIDERS: dict[str, GatewayProvider] = {
    "openai": GatewayProvider(
        name="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    ),
    "openrouter": GatewayProvider(
        name="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    ),
    "groq": GatewayProvider(
        name="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
    ),
    "mistral": GatewayProvider(
        name="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
    ),
    "together": GatewayProvider(
        name="together",
        label="Together AI",
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
    ),
    "fireworks": GatewayProvider(
        name="fireworks",
        label="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
    ),
    "deepseek": GatewayProvider(
        name="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
    ),
    "xai": GatewayProvider(
        name="xai",
        label="xAI",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
    ),
}


def provider_catalog() -> list[dict[str, Any]]:
    selected = selected_provider_name()
    return [
        {
            "name": provider.name,
            "label": provider.label,
            "base_url": provider.base_url,
            "api_key_env": provider.api_key_env,
            "selected": provider.name == selected,
            "key_set": bool(os.environ.get(provider.api_key_env)),
        }
        for provider in GATEWAY_PROVIDERS.values()
    ]


def selected_provider_name() -> str | None:
    raw = os.environ.get("SAI_GATEWAY_PROVIDER") or os.environ.get("SAI_PROVIDER")
    if raw:
        return raw.strip().lower()
    return None


def resolve_upstream_config() -> UpstreamConfig | None:
    custom_base = os.environ.get("SAI_UPSTREAM_BASE_URL")
    if custom_base:
        return UpstreamConfig(
            provider=os.environ.get("SAI_UPSTREAM_PROVIDER", "custom"),
            base_url=_legacy_upstream_base(custom_base),
            api_key_env="SAI_UPSTREAM_API_KEY",
            api_key=os.environ.get("SAI_UPSTREAM_API_KEY"),
        )

    name = selected_provider_name()
    if not name:
        return None
    provider = GATEWAY_PROVIDERS.get(name)
    if provider is None:
        valid = ", ".join(sorted(GATEWAY_PROVIDERS))
        raise UpstreamError(f"Unknown SAI_GATEWAY_PROVIDER '{name}'. Valid providers: {valid}")
    return UpstreamConfig(
        provider=provider.name,
        base_url=provider.base_url,
        api_key_env=provider.api_key_env,
        api_key=os.environ.get(provider.api_key_env),
    )


def _legacy_upstream_base(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def estimate_usage(payload: dict[str, Any], response_text: str = "") -> dict[str, int]:
    messages = payload.get("messages", [])
    prompt_chars = len(json.dumps(messages, ensure_ascii=False))
    prompt_tokens = max(1, prompt_chars // 4)
    max_tokens = _coerce_int(payload.get("max_tokens"), 64)
    if max_tokens <= 0:
        max_tokens = 64
    completion_tokens = max(1, min(max_tokens, max(8, len(response_text) // 4)))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def usage_cost(usage: dict[str, Any], rates: dict[str, float] | None = None) -> float:
    rate = rates or DEFAULT_RATES
    prompt = _coerce_int(usage.get("prompt_tokens")) / 1000 * rate["input_per_1k"]
    completion = _coerce_int(usage.get("completion_tokens")) / 1000 * rate["output_per_1k"]
    return round(max(0.001, prompt + completion), 6)


def _mock_delay_seconds() -> float:
    """Dev/testing knob: hold the mock response open this long so an external
    surface (the VS Code sponsor banner) can observe an in-flight wait via
    /v1/status. Off unless SAI_GATEWAY_MOCK_DELAY_MS is set; capped at 30s so a
    typo cannot wedge the gateway."""
    raw = os.environ.get("SAI_GATEWAY_MOCK_DELAY_MS", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0, min(30000, int(raw))) / 1000.0
    except ValueError:
        return 0.0


def mock_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    delay = _mock_delay_seconds()
    if delay > 0:
        time.sleep(delay)
    response_text = (
        "SAI mock response. Set SAI_GATEWAY_PROVIDER and the provider's API key "
        "to proxy a real model provider, or earn sponsored credits and the "
        "gateway will provision a spend-limited key for you."
    )
    usage = estimate_usage(payload, response_text)
    return {
        "id": f"chatcmpl-sai-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or MOCK_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def wallet_spend_disabled() -> bool:
    return os.environ.get("SAI_NO_WALLET_SPEND", "").strip().lower() in {"1", "true", "yes", "on"}


def wallet_upstream_config() -> UpstreamConfig | None:
    """Wallet-funded upstream: a per-installation provider key provisioned by the
    backend with a cumulative spend limit equal to the developer's credit
    balance. The gateway calls the provider directly; the backend only ever
    reads the key's usage counter. An explicitly configured provider wins."""
    if wallet_spend_disabled():
        return None
    config = load_config()
    api_key = config.get("spend_api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return None
    base_url = str(config.get("spend_base_url") or WALLET_SPEND_BASE_URL)
    return UpstreamConfig(
        provider=WALLET_SPEND_PROVIDER,
        base_url=base_url.rstrip("/"),
        api_key_env="(sai credits)",
        api_key=api_key.strip(),
    )


def active_upstream_config() -> UpstreamConfig | None:
    return resolve_upstream_config() or wallet_upstream_config()


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}
_LOCAL_CLIENT_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_LOCAL_HOST_HEADERS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_origin(origin: str) -> bool:
    """True only when an Origin header names a loopback host. An opaque "null"
    origin (sandboxed frames, file://) and any remote host return False."""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in _LOOPBACK_HOSTS


def _host_header_host(raw_host: str) -> str:
    raw = raw_host.strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        if not raw.startswith("[::1]"):
            return ""
        rest = raw[len("[::1]") :]
        if not rest:
            return "::1"
        if rest.startswith(":") and _valid_host_port(rest[1:]):
            return "::1"
        return ""
    host, sep, port = raw.partition(":")
    if host not in {"127.0.0.1", "localhost"}:
        return ""
    if sep and not _valid_host_port(port):
        return ""
    return host


def _valid_host_port(port: str) -> bool:
    if not port.isdigit():
        return False
    try:
        value = int(port)
    except ValueError:
        return False
    return 0 <= value <= 65535


def _metadata_label(value: Any, default: str = "-", limit: int = 64) -> str:
    text = str(value if value is not None else default).strip()
    if not text:
        text = default
    text = " ".join(text.split())
    return text[:limit]


def _payload_string(payload: dict[str, Any], key: str, default: str) -> str | None:
    if key not in payload:
        return default
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or default


def _payload_bool(payload: dict[str, Any], key: str, default: bool = False) -> bool | None:
    if key not in payload:
        return default
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _payload_float(payload: dict[str, Any], key: str, default: float = 0.0) -> float | None:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _post_backend_json(
    url: str, payload: dict[str, Any], timeout: float, auth_secret: str | None = None
) -> dict[str, Any] | None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    if auth_secret:
        request.add_header("Authorization", f"{INSTALL_AUTH_SCHEME} {auth_secret}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug(
            "backend json request failed route=%s duration_ms=%s error=%s",
            _request_route(url),
            int((time.monotonic() - started) * 1000),
            type(exc).__name__,
        )
        return None
    return data if isinstance(data, dict) else None


def refresh_spend_key(timeout: float = 6.0) -> dict[str, Any] | None:
    """Ask the backend to provision or refresh the wallet-funded upstream key
    and store the secret in the local config. Best-effort: returns None when
    spend is disabled, unconfigured, or the backend is unreachable."""
    if wallet_spend_disabled():
        return None
    config = load_config()
    backend_url = str(config.get("backend_url") or "").rstrip("/")
    install_id = config.get("install_id")
    if not backend_url or not install_id:
        return None
    auth_secret = resolve_install_secret(config)
    payload = {"install_id_hash": hash_install_id(str(install_id))}
    response = _post_backend_json(
        f"{backend_url}/v1/developer/spend/provision", payload, timeout, auth_secret=auth_secret
    )
    if response is None:
        return None
    if response.get("provisioned") and not response.get("key") and not config.get("spend_api_key"):
        # The backend already has a key for this installation but the secret is
        # gone locally (it is only returned once): rotate to get a new one.
        rotated = _post_backend_json(
            f"{backend_url}/v1/developer/spend/provision",
            {**payload, "rotate": True},
            timeout,
            auth_secret=auth_secret,
        )
        if rotated is not None:
            response = rotated
    key = response.get("key")
    if isinstance(key, str) and key.strip():
        config["spend_api_key"] = key.strip()
        config["spend_base_url"] = str(response.get("base_url") or WALLET_SPEND_BASE_URL)
        config["spend_key_hash"] = str(response.get("key_hash") or "")
        save_config(config)
    logger.info(
        "spend key refresh completed provisioned=%s rotated=%s reason=%s key_hash=%s limit_micros=%s",
        bool(response.get("provisioned")),
        bool(response.get("rotated")),
        response.get("reason") or "-",
        _log_id(response.get("key_hash"), 24),
        response.get("limit_micros") or 0,
    )
    return response


def start_install_link(timeout: float = 6.0) -> dict[str, Any] | None:
    """Ask the backend to mint a short-lived pairing code for this installation
    so it can be linked to a logged-in account on the hosted dashboard. The
    per-install secret authenticates the request and never leaves the machine.
    Best-effort: returns None when the backend is unreachable/unconfigured or the
    install is not registered yet. The returned dict carries the dashboard URL."""
    config = load_config()
    backend_url = str(config.get("backend_url") or "").rstrip("/")
    install_id = config.get("install_id")
    if not backend_url or not install_id:
        return None
    auth_secret = resolve_install_secret(config)
    payload = {"install_id_hash": hash_install_id(str(install_id))}
    response = _post_backend_json(
        f"{backend_url}/v1/developer/link/start", payload, timeout, auth_secret=auth_secret
    )
    if response is None or not isinstance(response.get("code"), str):
        logger.info("install link start failed reason=backend_unavailable_or_unregistered")
        return None
    result = dict(response)
    result["backend_url"] = backend_url
    result["dashboard_url"] = f"{backend_url}/dashboard?{urlencode({'code': str(response.get('code') or '')})}"
    logger.info("install link code created expires_in_seconds=%s", result.get("expires_in_seconds") or 0)
    return result


_last_spend_sync = 0.0
_last_spend_key_refresh = 0.0
SPEND_KEY_REFRESH_INTERVAL_SECONDS = 30.0


def _maybe_sync_spend_usage(force: bool = False) -> None:
    """Nudge the backend to read the provider usage counter and debit the
    ledger. Throttled and best-effort; never blocks or fails a completion."""
    global _last_spend_sync
    now = time.monotonic()
    if not force and now - _last_spend_sync < SPEND_SYNC_INTERVAL_SECONDS:
        return
    _last_spend_sync = now
    config = load_config()
    backend_url = str(config.get("backend_url") or "").rstrip("/")
    install_id = config.get("install_id")
    if not backend_url or not install_id:
        return
    _post_backend_json(
        f"{backend_url}/v1/developer/spend/sync",
        {"install_id_hash": hash_install_id(str(install_id))},
        timeout=3.0,
        auth_secret=resolve_install_secret(config),
    )


def _maybe_refresh_spend_key_inline(force: bool = False) -> dict[str, Any] | None:
    """Best-effort refresh of the wallet spend key before a request.

    This lets a long-running gateway pick up newly-matured credits or a resized
    provider limit without a restart. Explicit provider config still wins.
    """
    global _last_spend_key_refresh
    if resolve_upstream_config() is not None or wallet_spend_disabled():
        return None
    now = time.monotonic()
    if not force and now - _last_spend_key_refresh < SPEND_KEY_REFRESH_INTERVAL_SECONDS:
        return None
    _last_spend_key_refresh = now
    try:
        return refresh_spend_key(timeout=2.0)
    except Exception:
        logger.exception("Inline spend-key refresh failed")
        return None


_last_wallet_reconcile = 0.0


def _maybe_reconcile_wallet() -> None:
    """Pull the authoritative backend balance into the local display ledger so it
    reflects spend, maturation and clawbacks. Throttled and run off-thread; never
    blocks or fails a completion."""
    global _last_wallet_reconcile
    now = time.monotonic()
    if now - _last_wallet_reconcile < WALLET_RECONCILE_INTERVAL_SECONDS:
        return
    _last_wallet_reconcile = now
    thread = threading.Thread(target=_reconcile_wallet_best_effort, name="sai-wallet-reconcile", daemon=True)
    thread.start()


def _reconcile_wallet_best_effort() -> None:
    try:
        sync_local_wallet(timeout=3.0)
    except Exception:
        logger.exception("Background wallet reconcile failed")


def upstream_configured() -> bool:
    return active_upstream_config() is not None


def upstream_url(path: str) -> str | None:
    config = active_upstream_config()
    if config is None:
        return None
    return f"{config.base_url.rstrip('/')}{path}"


def proxy_json(path: str, payload: dict[str, Any] | None = None, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    config = active_upstream_config()
    url = upstream_url(path)
    if config is None or not url:
        raise UpstreamError("Upstream provider is not configured")
    if not config.api_key:
        raise UpstreamError(
            f"Upstream provider '{config.provider}' is missing an API key: "
            f"set {config.api_key_env}"
        )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {config.api_key}")
    request.add_header("Content-Type", "application/json")
    for key, value in upstream_extra_headers(config).items():
        request.add_header(key, value)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            headers = {"content-type": response.headers.get("content-type", "application/json")}
            body_bytes = response.read()
            logger.debug(
                "upstream response provider=%s route=%s status=%s duration_ms=%s content_type=%s",
                config.provider,
                path,
                response.status,
                int((time.monotonic() - started) * 1000),
                headers["content-type"],
            )
            return response.status, headers, body_bytes
    except urllib.error.HTTPError as exc:
        headers = {"content-type": exc.headers.get("content-type", "application/json")}
        body_bytes = exc.read()
        logger.warning(
            "upstream response provider=%s route=%s status=%s duration_ms=%s content_type=%s request_id=%s",
            config.provider,
            path,
            exc.code,
            int((time.monotonic() - started) * 1000),
            headers["content-type"],
            _log_id(exc.headers.get("x-request-id") or exc.headers.get("cf-ray"), 32),
        )
        return exc.code, headers, body_bytes
    except urllib.error.URLError as exc:
        raise UpstreamError(f"Upstream request failed: {exc.reason}") from exc


def upstream_extra_headers(config: UpstreamConfig) -> dict[str, str]:
    if config.provider != "openrouter":
        return {}
    headers = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER") or os.environ.get("SAI_APP_URL")
    title = os.environ.get("OPENROUTER_APP_TITLE") or os.environ.get("SAI_APP_TITLE")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-OpenRouter-Title"] = title
    return headers


def upstream_status_line() -> str:
    config = resolve_upstream_config()
    if config is None:
        wallet = wallet_upstream_config()
        if wallet is not None:
            return f"Upstream: sponsored credits ({wallet.base_url}) - per-install key, spend-limited to your balance"
        return (
            "Upstream: local mock (set SAI_GATEWAY_PROVIDER and the provider key, "
            "or earn credits and the gateway provisions a spend key)"
        )
    key_state = "key set" if config.api_key else f"missing {config.api_key_env}"
    return f"Upstream: {config.provider} ({config.base_url}) - {key_state}"


# In-flight chat-completion counter. /v1/status exposes it so an external
# surface (the VS Code extension) can show a sponsor ad while the agent is
# waiting on the model, without ever reading the agent's output.
_inflight_lock = threading.Lock()
_inflight_requests = 0


def _inflight_begin() -> None:
    global _inflight_requests
    with _inflight_lock:
        _inflight_requests += 1


def _inflight_end() -> None:
    global _inflight_requests
    with _inflight_lock:
        _inflight_requests = max(0, _inflight_requests - 1)


def inflight_requests() -> int:
    with _inflight_lock:
        return _inflight_requests


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "SAIGateway/0.1"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(self._handle_post)

    def send_response(self, code: int, message: str | None = None) -> None:
        self._sai_status = int(code)
        super().send_response(code, message)

    def _dispatch(self, handler: Callable[[], None]) -> None:
        started = time.monotonic()
        request_id = f"req_{secrets.token_hex(8)}"
        self._sai_status = 0
        try:
            handler()
        except Exception as exc:  # keep the gateway alive instead of dropping the connection
            if isinstance(exc, UpstreamError):
                status, message = HTTPStatus.BAD_GATEWAY, "Upstream provider request failed"
                logger.warning(
                    "Gateway upstream error method=%s route=%s error=%s",
                    self.command,
                    _request_route(self.path),
                    type(exc).__name__,
                )
            else:
                status, message = HTTPStatus.INTERNAL_SERVER_ERROR, "Internal gateway error"
                logger.exception("Unhandled gateway request error method=%s route=%s", self.command, _request_route(self.path))
            try:
                self._send_json(status, {"error": {"message": message}})
            except OSError:
                logger.debug("Could not send gateway error response method=%s route=%s", self.command, _request_route(self.path))
                pass
        finally:
            route = _request_route(self.path)
            if route != "/healthz":
                logger.info(
                    "gateway request request_id=%s method=%s route=%s status=%s duration_ms=%s client=%s",
                    request_id,
                    self.command,
                    route,
                    self._sai_status,
                    int((time.monotonic() - started) * 1000),
                    _log_hash(self.client_address[0]),
                )

    def _handle_get(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/v1/status":
            # Unauthenticated on purpose: it exposes only an in-flight count for
            # the local ad surface, never request content. Bound to localhost.
            self._send_json(HTTPStatus.OK, {"status": "ok", "active_requests": inflight_requests()})
            return
        if self.path in {"/", "/dashboard"}:
            if not self._dashboard_allowed():
                return
            self._send_raw(
                HTTPStatus.OK,
                {"content-type": "text/html; charset=utf-8"},
                DASHBOARD_HTML.encode("utf-8"),
            )
            return
        if self.path == "/api/overview":
            if not self._dashboard_allowed():
                return
            self._send_json(HTTPStatus.OK, overview_payload())
            return
        if self.path == "/v1/models":
            if not self._authorized():
                return
            if upstream_configured():
                status, headers, body = proxy_json("/models")
                self._send_raw(status, headers, body)
                return
            self._send_json(
                HTTPStatus.OK,
                {"object": "list", "data": [{"id": MOCK_MODEL, "object": "model", "owned_by": "sai"}]},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Not found"}})

    def _handle_post(self) -> None:
        if self.path == "/api/config":
            self._handle_config_update()
            return
        if self.path == "/v1/sai/placements/next":
            self._handle_sai_placement_next()
            return
        if self.path == "/v1/sai/placements/event":
            self._handle_sai_placement_event()
            return
        if self.path != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Not found"}})
            return
        if not self._authorized():
            return
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("stream"):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"message": "Streaming is not implemented in the MVP gateway"}},
            )
            return

        # Count this as in-flight for the whole upstream round trip so /v1/status
        # reports the wait the external ad surface should fill.
        _inflight_begin()
        try:
            explicit_config = resolve_upstream_config()
            if explicit_config is None:
                _maybe_refresh_spend_key_inline()
            config = explicit_config or wallet_upstream_config()
            if config is not None:
                status, headers, body = proxy_json("/chat/completions", payload=payload, method="POST")
                try:
                    self._send_raw(status, headers, body)
                finally:
                    if config.provider == WALLET_SPEND_PROVIDER:
                        _maybe_sync_spend_usage(force=True)
                        _maybe_reconcile_wallet()
                return

            response = mock_chat_completion(payload)
            self._send_json(HTTPStatus.OK, response)
        finally:
            _inflight_end()

    def _handle_sai_placement_next(self) -> None:
        started = time.monotonic()
        surface = VSCODE_WAIT_SURFACE
        tool = "codex"
        try:
            if not self._local_client_allowed():
                return
            if not self._placement_csrf_safe_post():
                return
            payload = self._read_json(max_body_bytes=MAX_PLACEMENT_BODY_BYTES)
            if payload is None:
                return
            parsed_surface = _payload_string(payload, "surface", VSCODE_WAIT_SURFACE)
            parsed_tool = _payload_string(payload, "tool", "codex")
            attended = _payload_bool(payload, "attended", default=False)
            if parsed_surface is None or parsed_tool is None or attended is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Invalid placement request"}})
                return
            surface = parsed_surface
            tool = parsed_tool
            result = fetch_placement_card(load_config(), tool=tool, surface=surface, attended=attended)
            self._send_json(HTTPStatus.OK, result)
        finally:
            self._log_placement_request("next", surface, tool, started)

    def _handle_sai_placement_event(self) -> None:
        started = time.monotonic()
        surface = VSCODE_WAIT_SURFACE
        tool = "codex"
        try:
            if not self._local_client_allowed():
                return
            if not self._placement_csrf_safe_post():
                return
            payload = self._read_json(max_body_bytes=MAX_PLACEMENT_BODY_BYTES)
            if payload is None:
                return
            ticket = payload.get("ticket")
            event = _payload_string(payload, "event", QP_EVENT)
            visible_seconds = _payload_float(payload, "visible_seconds", default=0.0)
            attended = _payload_bool(payload, "attended", default=False)
            if not isinstance(ticket, dict) or event is None or visible_seconds is None or attended is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Invalid placement event request"}})
                return
            if event != QP_EVENT:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Unsupported placement event"}})
                return
            surface = _metadata_label(ticket.get("surface"), VSCODE_WAIT_SURFACE)
            tool = _metadata_label(ticket.get("tool"), "codex")
            result = record_placement_event(
                load_config(),
                ticket,
                event=event,
                visible_seconds=visible_seconds,
                attended=attended,
            )
            self._send_json(HTTPStatus.OK, result)
        finally:
            self._log_placement_request("event", surface, tool, started)

    def _handle_config_update(self) -> None:
        if not self._dashboard_allowed():
            return
        if not self._csrf_safe_write():
            return
        payload = self._read_json()
        if payload is None:
            return
        if "frequency" in payload:
            try:
                set_frequency(str(payload["frequency"]))
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
                return
        if "kill_switch" in payload:
            reason = payload.get("reason")
            set_kill_switch(bool(payload["kill_switch"]), reason=str(reason) if reason else None)
        logger.info(
            "gateway config updated keys=%s",
            ",".join(sorted(key for key in payload.keys() if key in {"frequency", "kill_switch"})) or "-",
        )
        self._send_json(HTTPStatus.OK, overview_payload())

    def _dashboard_allowed(self) -> bool:
        # The dashboard and its API expose the local API key and accept config
        # writes, so they are restricted to loopback clients. The Host header
        # must also be a localhost name: that blocks DNS-rebinding pages that
        # point a public hostname at 127.0.0.1 to read these endpoints.
        client = self.client_address[0]
        raw_host = self.headers.get("Host", "")
        if raw_host.startswith("["):
            host = raw_host.partition("]")[0].lstrip("[")
        else:
            host = raw_host.partition(":")[0]
        if client in {"127.0.0.1", "::1", "::ffff:127.0.0.1"} and host.lower() in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            return True
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": {"message": "The SAI dashboard is only served to localhost"}},
        )
        logger.warning("gateway dashboard denied client=%s host=%s route=%s", _log_hash(client), host, _request_route(self.path))
        return False

    def _local_client_allowed(self) -> bool:
        # Placement endpoints are unauthenticated compatibility shims for local
        # VS Code. They do not expose gateway secrets, but they can transport a
        # billable placement event, so keep them loopback-only even if the
        # gateway itself is bound to 0.0.0.0 for model proxying.
        client = self.client_address[0]
        host = _host_header_host(self.headers.get("Host", ""))
        if client in _LOCAL_CLIENT_HOSTS and host in _LOCAL_HOST_HEADERS:
            return True
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": {"message": "SAI placement endpoints are only served to localhost"}},
        )
        logger.warning("gateway placement denied client=%s host=%s route=%s", _log_hash(client), host, _request_route(self.path))
        return False

    def _placement_csrf_safe_post(self) -> bool:
        # A hostile website can send a no-CORS "simple" POST to localhost even
        # though it cannot read the response. Requiring JSON blocks simple form
        # or text/plain posts, and the Origin/Sec-Fetch checks reject browsers
        # that do identify a cross-site caller.
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._reject_placement_csrf("application/json content-type required")
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin and not _is_loopback_origin(origin):
            self._reject_placement_csrf("cross-origin request rejected")
            return False
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site in {"cross-site", "same-site"}:
            self._reject_placement_csrf("cross-site request rejected")
            return False
        return True

    def _reject_placement_csrf(self, message: str) -> None:
        logger.warning("gateway placement csrf rejected route=%s reason=%s", _request_route(self.path), message)
        self._send_json(HTTPStatus.FORBIDDEN, {"error": {"message": message}})

    def _log_placement_request(self, action: str, surface: str, tool: str, started: float) -> None:
        logger.info(
            "gateway placement request action=%s surface=%s tool=%s status=%s duration_ms=%s",
            action,
            _metadata_label(surface),
            _metadata_label(tool),
            self._sai_status,
            int((time.monotonic() - started) * 1000),
        )

    def _csrf_safe_write(self) -> bool:
        # Loopback alone does not stop CSRF: a page on any origin can POST to
        # 127.0.0.1 and flip frequency/kill_switch even though the same-origin
        # policy hides the response. Defend the config write with checks a
        # cross-site request cannot satisfy:
        #   - require Content-Type: application/json. A cross-origin fetch with
        #     this type is not a "simple" request, so the browser must clear a
        #     CORS preflight that this server never answers; simple form or
        #     text/plain posts are rejected here.
        #   - reject a non-loopback (or opaque "null") Origin when present.
        #   - reject Sec-Fetch-Site values that mark a cross-origin caller.
        # The dashboard's own fetch sends application/json from a loopback,
        # same-origin context, so it is unaffected.
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._reject_csrf("application/json content-type required")
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin and not _is_loopback_origin(origin):
            self._reject_csrf("cross-origin request rejected")
            return False
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site in {"cross-site", "same-site"}:
            self._reject_csrf("cross-site request rejected")
            return False
        return True

    def _reject_csrf(self, message: str) -> None:
        logger.warning("gateway csrf rejected route=%s reason=%s", _request_route(self.path), message)
        self._send_json(HTTPStatus.FORBIDDEN, {"error": {"message": message}})

    def _authorized(self) -> bool:
        config = load_config()
        expected = config.get("api_key")
        header = self.headers.get("Authorization", "")
        supplied = header.removeprefix("Bearer ").strip()
        if (
            isinstance(expected, str)
            and expected
            and supplied
            and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        ):
            return True
        logger.warning("gateway auth failed route=%s", _request_route(self.path))
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": {"message": "Invalid or missing SAI API key"}})
        return False

    def _read_json(self, max_body_bytes: int = MAX_BODY_BYTES) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Invalid Content-Length"}})
            return None
        if length > max_body_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": {"message": f"Request body exceeds {max_body_bytes} bytes"}},
            )
            return None
        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Invalid JSON"}})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "JSON body must be an object"}})
            return None
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_raw(status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))

    def _send_raw(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(int(status))
        supplied = {key.lower() for key in headers}
        for key, value in SECURITY_HEADERS.items():
            if key.lower() not in supplied:
                self.send_header(key, value)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Do not log requests because model payloads may contain prompts.
        return


def gateway_running(host: str = "127.0.0.1", port: int = 8787, timeout: float = 0.2) -> bool:
    """True when an SAI gateway answers /healthz on the given address."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def start_gateway_in_background(host: str = "127.0.0.1", port: int = 8787, wait_seconds: float = 8.0) -> bool:
    """Spawn `sai gateway serve` detached from this process and wait for /healthz.

    The live agent-wrapper autostart is the copy in cli.py (kept urllib-free for
    a cheap `sai claude` startup); keep this one's behaviour in step with it.
    """
    if getattr(sys, "frozen", False):
        command = [sys.executable, "gateway", "serve", "--host", host, "--port", str(port)]
    else:
        command = [sys.executable, "-m", "sai", "gateway", "serve", "--host", host, "--port", str(port)]
    # Drop the PyInstaller onefile marker so the detached child unpacks its own
    # runtime instead of binding to (and being orphaned by) the parent's temp
    # extraction dir. See cli._gateway_child_env.
    child_env = dict(os.environ)
    child_env.pop("_MEIPASS2", None)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": child_env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError:
        logger.exception("Failed to start gateway process in background")
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if gateway_running(host, port):
            return True
        time.sleep(0.1)
    return False


def maybe_refresh_spend_key_in_background() -> None:
    if resolve_upstream_config() is not None or wallet_spend_disabled():
        return
    thread = threading.Thread(target=_refresh_spend_key_best_effort, name="sai-spend-key-refresh", daemon=True)
    thread.start()


def _refresh_spend_key_best_effort() -> None:
    _maybe_refresh_spend_key_inline(force=True)


def serve_gateway(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False) -> None:
    configure_logging(service="gateway")
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    url = f"http://{host}:{server.server_address[1]}"
    logger.info("Gateway listening url=%s log=%s", url, log_destination_label())
    print(f"SAI gateway listening on {url}/v1")
    print(f"Dashboard: {url}/")
    print(f"Logs: {log_destination_label()}")
    maybe_refresh_spend_key_in_background()
    print(upstream_status_line())
    print("Set OpenAI-compatible clients to this base URL and use your SAI API key.")
    if open_browser:
        # Delay slightly so the server is accepting connections first.
        threading.Timer(0.5, webbrowser.open, args=(f"{url}/",)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSAI gateway stopped.")
    finally:
        server.server_close()
