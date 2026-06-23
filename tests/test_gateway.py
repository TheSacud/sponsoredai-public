from __future__ import annotations

import os
from email.message import Message
from io import BytesIO
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from sai.config import load_config, login, save_config
from sai.gateway import (
    MAX_PLACEMENT_BODY_BYTES,
    UpstreamConfig,
    WALLET_SPEND_PROVIDER,
    active_upstream_config,
    estimate_usage,
    provider_catalog,
    mock_chat_completion,
    maybe_refresh_spend_key_in_background,
    refresh_spend_key,
    start_gateway_in_background,
    start_install_link,
    upstream_url,
    usage_cost,
    wallet_upstream_config,
)
from sai.gateway import GatewayHandler
from sai.metrics import VSCODE_WAIT_SURFACE
from sai.sponsors import install_auth_secret
from sai.wallet import Wallet


class GatewayTests(unittest.TestCase):
    PROVIDER_ENV_KEYS = (
        "SAI_GATEWAY_PROVIDER",
        "SAI_PROVIDER",
        "SAI_UPSTREAM_BASE_URL",
        "SAI_UPSTREAM_API_KEY",
        "SAI_UPSTREAM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "TOGETHER_API_KEY",
        "FIREWORKS_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "SAI_NO_WALLET_SPEND",
        "SAI_HOME",
    )

    def setUp(self):
        self._previous_env = {key: os.environ.get(key) for key in self.PROVIDER_ENV_KEYS}
        for key in self.PROVIDER_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_mock_completion_is_openai_compatible_shape(self):
        response = mock_chat_completion({"model": "sai/mock", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response["object"], "chat.completion")
        self.assertIn("choices", response)
        self.assertIn("usage", response)

    def test_usage_cost_has_minimum(self):
        usage = estimate_usage({"messages": []})
        self.assertGreaterEqual(usage_cost(usage), 0.001)

    def test_estimate_usage_tolerates_bad_max_tokens(self):
        for bad_value in ("abc", None, -5, {}):
            usage = estimate_usage({"messages": [], "max_tokens": bad_value})
            self.assertGreaterEqual(usage["completion_tokens"], 1)

    def test_usage_cost_tolerates_null_token_counts(self):
        cost = usage_cost({"prompt_tokens": None, "completion_tokens": None})
        self.assertEqual(cost, 0.001)

    def test_provider_url_uses_exact_preset_base(self):
        os.environ["SAI_GATEWAY_PROVIDER"] = "deepseek"

        self.assertEqual(upstream_url("/chat/completions"), "https://api.deepseek.com/chat/completions")

    def test_legacy_upstream_env_still_defaults_to_v1(self):
        os.environ["SAI_UPSTREAM_BASE_URL"] = "https://api.openai.com"

        self.assertEqual(upstream_url("/chat/completions"), "https://api.openai.com/v1/chat/completions")

    def test_provider_catalog_marks_selected_key_status(self):
        os.environ["SAI_GATEWAY_PROVIDER"] = "groq"
        os.environ["GROQ_API_KEY"] = "test-key"

        groq = next(provider for provider in provider_catalog() if provider["name"] == "groq")
        self.assertTrue(groq["selected"])
        self.assertTrue(groq["key_set"])

    def _temp_home(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["SAI_HOME"] = tmp.name
        return tmp.name

    def test_wallet_upstream_used_when_no_provider_configured(self):
        self._temp_home()
        config = load_config()
        config["spend_api_key"] = "sk-or-wallet"
        save_config(config)

        upstream = active_upstream_config()

        self.assertIsNotNone(upstream)
        self.assertEqual(upstream.provider, WALLET_SPEND_PROVIDER)
        self.assertEqual(upstream.api_key, "sk-or-wallet")
        self.assertEqual(upstream.base_url, "https://openrouter.ai/api/v1")

    def test_explicit_provider_wins_over_wallet_key(self):
        self._temp_home()
        config = load_config()
        config["spend_api_key"] = "sk-or-wallet"
        save_config(config)
        os.environ["SAI_GATEWAY_PROVIDER"] = "groq"
        os.environ["GROQ_API_KEY"] = "groq-key"

        self.assertEqual(active_upstream_config().provider, "groq")

    def test_wallet_spend_opt_out_env(self):
        self._temp_home()
        config = load_config()
        config["spend_api_key"] = "sk-or-wallet"
        save_config(config)
        os.environ["SAI_NO_WALLET_SPEND"] = "1"

        self.assertIsNone(wallet_upstream_config())
        self.assertIsNone(active_upstream_config())

    def test_refresh_spend_key_stores_secret(self):
        self._temp_home()
        save_config(load_config())
        response = {
            "provisioned": True,
            "key": "sk-or-new",
            "key_hash": "kh_1",
            "base_url": "https://openrouter.ai/api/v1",
        }

        with mock.patch("sai.gateway._post_backend_json", return_value=response) as post:
            result = refresh_spend_key()

        self.assertEqual(result["key"], "sk-or-new")
        post.assert_called_once()
        config = load_config()
        self.assertEqual(config["spend_api_key"], "sk-or-new")
        self.assertEqual(config["spend_base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(config["spend_key_hash"], "kh_1")

    def test_refresh_spend_key_rotates_when_secret_lost(self):
        self._temp_home()
        save_config(load_config())
        responses = [
            {"provisioned": True, "key": None, "key_hash": "kh_1"},
            {
                "provisioned": True,
                "rotated": True,
                "key": "sk-or-rotated",
                "key_hash": "kh_2",
                "base_url": "https://openrouter.ai/api/v1",
            },
        ]

        with mock.patch("sai.gateway._post_backend_json", side_effect=responses) as post:
            result = refresh_spend_key()

        self.assertEqual(result["key"], "sk-or-rotated")
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[1].args[1].get("rotate"))
        self.assertEqual(load_config()["spend_api_key"], "sk-or-rotated")

    def test_backend_calls_use_issued_secret_then_derived_fallback(self):
        self._temp_home()
        config = load_config()
        config["install_secret"] = "issued-link-secret"
        save_config(config)

        with mock.patch(
            "sai.gateway._post_backend_json", return_value={"code": "ABCDEFGH", "expires_in_seconds": 600}
        ) as post:
            result = start_install_link()

        self.assertEqual(result["code"], "ABCDEFGH")
        self.assertEqual(result["dashboard_url"], "https://sponsoredai.dev/dashboard?code=ABCDEFGH")
        self.assertEqual(post.call_args.kwargs["auth_secret"], "issued-link-secret")

        # An install that has not rotated yet falls back to the derived secret.
        config = load_config()
        config.pop("install_secret", None)
        save_config(config)
        with mock.patch(
            "sai.gateway._post_backend_json", return_value={"code": "ABCDEFGH", "expires_in_seconds": 600}
        ) as post:
            start_install_link()
        self.assertEqual(
            post.call_args.kwargs["auth_secret"], install_auth_secret(load_config()["install_id"])
        )

    def test_spend_key_refresh_runs_in_background(self):
        with mock.patch("sai.gateway.resolve_upstream_config", return_value=None), \
                mock.patch("sai.gateway.wallet_spend_disabled", return_value=False), \
                mock.patch("sai.gateway.threading.Thread") as thread_class:
            maybe_refresh_spend_key_in_background()

        thread_class.assert_called_once()
        self.assertTrue(thread_class.call_args.kwargs["daemon"])
        thread_class.return_value.start.assert_called_once()

    def test_spend_key_refresh_skips_when_provider_configured(self):
        with mock.patch("sai.gateway.resolve_upstream_config", return_value=mock.Mock()), \
                mock.patch("sai.gateway.threading.Thread") as thread_class:
            maybe_refresh_spend_key_in_background()

        thread_class.assert_not_called()

    def test_wallet_spend_sync_runs_when_client_write_fails(self):
        handler = object.__new__(GatewayHandler)
        handler.path = "/v1/chat/completions"
        handler._authorized = mock.Mock(return_value=True)
        handler._read_json = mock.Mock(return_value={"model": "x", "messages": []})
        handler._send_raw = mock.Mock(side_effect=BrokenPipeError)
        upstream = UpstreamConfig(
            provider=WALLET_SPEND_PROVIDER,
            base_url="https://openrouter.ai/api/v1",
            api_key_env="(sai credits)",
            api_key="sk-or-wallet",
        )

        with mock.patch("sai.gateway.resolve_upstream_config", return_value=None), \
                mock.patch("sai.gateway._maybe_refresh_spend_key_inline"), \
                mock.patch("sai.gateway.wallet_upstream_config", return_value=upstream), \
                mock.patch("sai.gateway.proxy_json", return_value=(200, {"content-type": "application/json"}, b"{}")), \
                mock.patch("sai.gateway._maybe_sync_spend_usage") as sync, \
                mock.patch("sai.gateway._maybe_reconcile_wallet") as reconcile:
            with self.assertRaises(BrokenPipeError):
                handler._handle_post()

        sync.assert_called_once_with(force=True)
        reconcile.assert_called_once()

    def test_chat_completion_attempts_inline_spend_key_refresh_before_mock(self):
        handler = object.__new__(GatewayHandler)
        handler.path = "/v1/chat/completions"
        handler._authorized = mock.Mock(return_value=True)
        handler._read_json = mock.Mock(return_value={"model": "x", "messages": []})
        handler._send_json = mock.Mock()

        with mock.patch("sai.gateway.resolve_upstream_config", return_value=None), \
                mock.patch("sai.gateway._maybe_refresh_spend_key_inline") as refresh, \
                mock.patch("sai.gateway.wallet_upstream_config", return_value=None):
            handler._handle_post()

        refresh.assert_called_once()
        handler._send_json.assert_called_once()

    def test_mock_chat_completion_does_not_spend_local_wallet(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SAI_HOME"] = tmp
            config = login()
            server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = {"model": "sai/mock", "messages": [{"role": "user", "content": "hi"}]}
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                )
                request.add_header("Content-Type", "application/json")
                request.add_header("Authorization", f"Bearer {config['api_key']}")

                with urllib.request.urlopen(request, timeout=10) as response:
                    body = json.loads(response.read().decode("utf-8"))

                self.assertEqual(response.status, 200)
                self.assertEqual(body["object"], "chat.completion")
                self.assertEqual(Wallet().entries(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def _placement_handler(
        self,
        *,
        path: str = "/v1/sai/placements/next",
        body: bytes | None = None,
        client: str = "127.0.0.1",
        host: str = "127.0.0.1:8787",
        content_type: str = "application/json",
        origin: str | None = None,
        sec_fetch_site: str | None = None,
        content_length: int | None = None,
    ):
        payload = body if body is not None else json.dumps(
            {"surface": VSCODE_WAIT_SURFACE, "tool": "codex", "attended": True}
        ).encode("utf-8")
        headers = Message()
        headers["Host"] = host
        headers["Content-Length"] = str(len(payload) if content_length is None else content_length)
        headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        if sec_fetch_site is not None:
            headers["Sec-Fetch-Site"] = sec_fetch_site
        handler = object.__new__(GatewayHandler)
        handler.path = path
        handler.command = "POST"
        handler.client_address = (client, 45678)
        handler.headers = headers
        handler.rfile = BytesIO(payload)
        handler._sai_status = 0
        sent = []

        def send_json(status, response):
            handler._sai_status = int(status)
            sent.append((int(status), response))

        handler._send_json = mock.Mock(side_effect=send_json)
        handler.sent = sent
        return handler

    def test_placement_next_rejects_non_loopback_client(self):
        handler = self._placement_handler(client="192.0.2.10")

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 403)
        fetch.assert_not_called()

    def test_placement_next_rejects_non_loopback_host(self):
        handler = self._placement_handler(host="example.com")

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 403)
        fetch.assert_not_called()

    def test_placement_next_rejects_malformed_loopback_host_headers(self):
        for host in ("[::1]evil:8787", "localhost:notaport", "127.0.0.1:8787:evil", "localhost:"):
            with self.subTest(host=host):
                handler = self._placement_handler(host=host)
                with mock.patch("sai.gateway.fetch_placement_card") as fetch:
                    handler._handle_sai_placement_next()
                self.assertEqual(handler.sent[-1][0], 403)
                fetch.assert_not_called()

    def test_placement_next_rejects_simple_cross_site_post_content_type(self):
        handler = self._placement_handler(content_type="text/plain")

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 403)
        fetch.assert_not_called()

    def test_placement_next_rejects_remote_origin(self):
        handler = self._placement_handler(origin="https://evil.example")

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 403)
        fetch.assert_not_called()

    def test_placement_next_rejects_cross_site_fetch_metadata(self):
        handler = self._placement_handler(sec_fetch_site="cross-site")

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 403)
        fetch.assert_not_called()

    def test_placement_next_accepts_ipv6_loopback_host(self):
        handler = self._placement_handler(client="::1", host="[::1]:8787")

        with mock.patch("sai.gateway.load_config", return_value={"country": "PT"}) as load, \
                mock.patch("sai.gateway.fetch_placement_card", return_value={"placement": None}) as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1], (200, {"placement": None}))
        load.assert_called_once()
        fetch.assert_called_once_with({"country": "PT"}, tool="codex", surface=VSCODE_WAIT_SURFACE, attended=True)

    def test_placement_next_calls_existing_helper_with_surface_and_attended(self):
        handler = self._placement_handler(
            body=json.dumps({"surface": VSCODE_WAIT_SURFACE, "tool": "codex", "attended": False}).encode("utf-8")
        )
        response = {"placement": {"placement_id": "plc_1", "signature": "sig_1"}}

        with mock.patch("sai.gateway.load_config", return_value={"country": "PT"}) as load, \
                mock.patch("sai.gateway.fetch_placement_card", return_value=response) as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1], (200, response))
        load.assert_called_once()
        fetch.assert_called_once_with({"country": "PT"}, tool="codex", surface=VSCODE_WAIT_SURFACE, attended=False)

    def test_placement_logs_normalize_control_characters(self):
        handler = self._placement_handler(
            body=json.dumps({"surface": "vscode_ai_wait\n forged=1", "tool": "codex\ttool", "attended": True}).encode("utf-8")
        )

        with mock.patch("sai.gateway.load_config", return_value={}), \
                mock.patch("sai.gateway.fetch_placement_card", return_value={"placement": None}), \
                mock.patch("sai.gateway.logger.info") as log_info:
            handler._handle_sai_placement_next()

        logged = " ".join(str(arg) for call in log_info.call_args_list for arg in call.args)
        self.assertIn("vscode_ai_wait forged=1", logged)
        self.assertIn("codex tool", logged)
        self.assertNotIn("\n", logged)
        self.assertNotIn("\t", logged)

    def test_placement_next_invalid_json_returns_400(self):
        handler = self._placement_handler(body=b"{not-json")

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 400)
        fetch.assert_not_called()

    def test_placement_next_limits_body_size(self):
        handler = self._placement_handler(body=b"{}", content_length=MAX_PLACEMENT_BODY_BYTES + 1)

        with mock.patch("sai.gateway.fetch_placement_card") as fetch:
            handler._handle_sai_placement_next()

        self.assertEqual(handler.sent[-1][0], 413)
        fetch.assert_not_called()

    def test_placement_event_sends_ticket_from_body_without_logging_it(self):
        ticket = {
            "placement_id": "plc_1",
            "signature": "sig_secret",
            "campaign_id": "cmp_1",
            "surface": VSCODE_WAIT_SURFACE,
            "tool": "codex",
            "url": "https://sponsor.example/private",
        }
        body = json.dumps(
            {"ticket": ticket, "event": "qualified_5s", "visible_seconds": 5.2, "attended": True}
        ).encode("utf-8")
        handler = self._placement_handler(path="/v1/sai/placements/event", body=body)

        with mock.patch("sai.gateway.load_config", return_value={"country": "PT"}), \
                mock.patch("sai.gateway.record_placement_event", return_value={"billable": True}) as record, \
                mock.patch("sai.gateway.logger.info") as log_info:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1], (200, {"billable": True}))
        record.assert_called_once_with(
            {"country": "PT"},
            ticket,
            event="qualified_5s",
            visible_seconds=5.2,
            attended=True,
        )
        logged = " ".join(str(arg) for call in log_info.call_args_list for arg in call.args)
        self.assertNotIn("sig_secret", logged)
        self.assertNotIn("sponsor.example", logged)

    def test_placement_event_invalid_body_returns_400(self):
        handler = self._placement_handler(
            path="/v1/sai/placements/event",
            body=json.dumps({"ticket": "not-a-ticket", "visible_seconds": "5.2", "attended": True}).encode("utf-8"),
        )

        with mock.patch("sai.gateway.record_placement_event") as record:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1][0], 400)
        record.assert_not_called()

    def test_placement_event_rejects_unsupported_event(self):
        handler = self._placement_handler(
            path="/v1/sai/placements/event",
            body=json.dumps({"ticket": {}, "event": "rendered", "visible_seconds": 0.0, "attended": True}).encode("utf-8"),
        )

        with mock.patch("sai.gateway.record_placement_event") as record:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1][0], 400)
        record.assert_not_called()

    def test_placement_event_rejects_simple_cross_site_post_content_type(self):
        handler = self._placement_handler(
            path="/v1/sai/placements/event",
            body=json.dumps({"ticket": {}, "event": "qualified_5s", "visible_seconds": 5.2, "attended": True}).encode("utf-8"),
            content_type="text/plain",
        )

        with mock.patch("sai.gateway.record_placement_event") as record:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1][0], 403)
        record.assert_not_called()

    def test_placement_event_rejects_remote_origin(self):
        handler = self._placement_handler(
            path="/v1/sai/placements/event",
            body=json.dumps({"ticket": {}, "event": "qualified_5s", "visible_seconds": 5.2, "attended": True}).encode("utf-8"),
            origin="https://evil.example",
        )

        with mock.patch("sai.gateway.record_placement_event") as record:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1][0], 403)
        record.assert_not_called()

    def test_placement_event_rejects_cross_site_fetch_metadata(self):
        handler = self._placement_handler(
            path="/v1/sai/placements/event",
            body=json.dumps({"ticket": {}, "event": "qualified_5s", "visible_seconds": 5.2, "attended": True}).encode("utf-8"),
            sec_fetch_site="cross-site",
        )

        with mock.patch("sai.gateway.record_placement_event") as record:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1][0], 403)
        record.assert_not_called()

    def test_placement_event_rejects_non_finite_visible_seconds(self):
        handler = self._placement_handler(
            path="/v1/sai/placements/event",
            body=b'{"ticket":{},"event":"qualified_5s","visible_seconds":NaN,"attended":true}',
        )

        with mock.patch("sai.gateway.record_placement_event") as record:
            handler._handle_sai_placement_event()

        self.assertEqual(handler.sent[-1][0], 400)
        record.assert_not_called()

    def test_gateway_autostart_uses_python_module_in_source(self):
        with mock.patch("sai.gateway.gateway_running", return_value=True), \
                mock.patch("sai.gateway.subprocess.Popen") as popen, \
                mock.patch("sai.gateway.sys.executable", "/usr/bin/python"), \
                mock.patch("sai.gateway.sys.frozen", False, create=True):
            self.assertTrue(start_gateway_in_background())

        command = popen.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/bin/python", "-m", "sai", "gateway"])

    def test_gateway_autostart_uses_cli_args_when_frozen(self):
        with mock.patch("sai.gateway.gateway_running", return_value=True), \
                mock.patch("sai.gateway.subprocess.Popen") as popen, \
                mock.patch("sai.gateway.sys.executable", "C:\\Program Files\\sai\\sai.exe"), \
                mock.patch("sai.gateway.sys.frozen", True, create=True):
            self.assertTrue(start_gateway_in_background())

        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["C:\\Program Files\\sai\\sai.exe", "gateway", "serve"])

    def test_cli_gateway_running_detects_a_live_healthz(self):
        # Regression: cli.gateway_running used a single recv(256), but a healthy
        # /healthz response is ~530+ bytes (security headers) with the JSON body
        # at the end, so the body marker fell outside the read window and a live
        # gateway was reported as down -- the core of issue #2. Run the real CLI
        # probe against a real GatewayHandler and require a True.
        from sai.cli import gateway_running as cli_gateway_running

        server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(cli_gateway_running(port=port, timeout=2.0))
            self.assertFalse(cli_gateway_running(port=port + 1, timeout=0.2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class BackendRequestHeaderTests(unittest.TestCase):
    def test_post_backend_json_uses_shared_http_client_and_sends_sai_user_agent(self):
        from sai.config import USER_AGENT
        from sai.gateway import _post_backend_json

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch("sai.gateway.http_urlopen", side_effect=fake_urlopen) as urlopen:
            _post_backend_json("https://backend.test/v1/x", {"a": 1}, timeout=1.0)

        urlopen.assert_called_once()
        self.assertEqual(captured["timeout"], 1.0)
        self.assertEqual(captured["request"].get_header("User-agent"), USER_AGENT)

if __name__ == "__main__":
    unittest.main()
