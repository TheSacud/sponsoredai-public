from __future__ import annotations

import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from urllib.parse import urlparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .app_logging import configure_logging, log_destination_label
from .config import USER_AGENT, load_config, save_config, set_frequency, set_kill_switch
from .credits import sync_local_wallet
from .dashboard import DASHBOARD_HTML, overview_payload
from .sponsors import INSTALL_AUTH_SCHEME, hash_install_id, resolve_install_secret


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


def mock_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
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


def _post_backend_json(
    url: str, payload: dict[str, Any], timeout: float, auth_secret: str | None = None
) -> dict[str, Any] | None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    if auth_secret:
        request.add_header("Authorization", f"{INSTALL_AUTH_SCHEME} {auth_secret}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("Backend JSON request failed url=%s: %s", url, exc)
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
        return None
    result = dict(response)
    result["backend_url"] = backend_url
    result["dashboard_url"] = f"{backend_url}/dashboard"
    return result


_last_spend_sync = 0.0


def _maybe_sync_spend_usage() -> None:
    """Nudge the backend to read the provider usage counter and debit the
    ledger. Throttled and best-effort; never blocks or fails a completion."""
    global _last_spend_sync
    now = time.monotonic()
    if now - _last_spend_sync < SPEND_SYNC_INTERVAL_SECONDS:
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
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            headers = {"content-type": response.headers.get("content-type", "application/json")}
            return response.status, headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {"content-type": exc.headers.get("content-type", "application/json")}, exc.read()
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


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "SAIGateway/0.1"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(self._handle_post)

    def _dispatch(self, handler: Callable[[], None]) -> None:
        try:
            handler()
        except Exception as exc:  # keep the gateway alive instead of dropping the connection
            if isinstance(exc, UpstreamError):
                status, message = HTTPStatus.BAD_GATEWAY, "Upstream provider request failed"
                logger.warning("Gateway upstream error method=%s path=%s: %s", self.command, self.path, exc)
            else:
                status, message = HTTPStatus.INTERNAL_SERVER_ERROR, "Internal gateway error"
                logger.exception("Unhandled gateway request error method=%s path=%s", self.command, self.path)
            try:
                self._send_json(status, {"error": {"message": message}})
            except OSError:
                logger.debug("Could not send gateway error response method=%s path=%s", self.command, self.path)
                pass

    def _handle_get(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
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

        config = active_upstream_config()
        if config is not None:
            status, headers, body = proxy_json("/chat/completions", payload=payload, method="POST")
            self._send_raw(status, headers, body)
            if config.provider == WALLET_SPEND_PROVIDER:
                _maybe_sync_spend_usage()
                _maybe_reconcile_wallet()
            return

        response = mock_chat_completion(payload)
        self._send_json(HTTPStatus.OK, response)

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
        return False

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
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": {"message": "Invalid or missing SAI API key"}})
        return False

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "Invalid Content-Length"}})
            return None
        if length > MAX_BODY_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": {"message": f"Request body exceeds {MAX_BODY_BYTES} bytes"}},
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


def start_gateway_in_background(host: str = "127.0.0.1", port: int = 8787, wait_seconds: float = 1.5) -> bool:
    """Spawn `sai gateway serve` detached from this process and wait for /healthz."""
    if getattr(sys, "frozen", False):
        command = [sys.executable, "gateway", "serve", "--host", host, "--port", str(port)]
    else:
        command = [sys.executable, "-m", "sai", "gateway", "serve", "--host", host, "--port", str(port)]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
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
    try:
        refresh_spend_key()
    except Exception:
        logger.exception("Background spend-key refresh failed")
        pass  # gateway startup should not depend on backend availability


def serve_gateway(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False) -> None:
    configure_logging(service="gateway")
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    url = f"http://{host}:{server.server_address[1]}"
    logger.info("Gateway listening url=%s", url)
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
