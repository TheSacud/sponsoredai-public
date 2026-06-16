import os
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from sai.config import load_config, login, save_config
from sai.gateway import (
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


class BackendRequestHeaderTests(unittest.TestCase):
    def test_post_backend_json_sends_sai_user_agent(self):
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
            return FakeResponse()

        with mock.patch("sai.gateway.urllib.request.urlopen", side_effect=fake_urlopen):
            _post_backend_json("https://backend.test/v1/x", {"a": 1}, timeout=1.0)

        self.assertEqual(captured["request"].get_header("User-agent"), USER_AGENT)


if __name__ == "__main__":
    unittest.main()
