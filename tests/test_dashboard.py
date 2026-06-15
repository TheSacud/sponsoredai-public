import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from sai.config import load_config, login
from sai.dashboard import overview_payload
from sai.gateway import GatewayHandler
from sai.wallet import Wallet


class SaiHomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_home = os.environ.get("SAI_HOME")
        os.environ["SAI_HOME"] = self._tmp.name

    def tearDown(self):
        if self._previous_home is None:
            os.environ.pop("SAI_HOME", None)
        else:
            os.environ["SAI_HOME"] = self._previous_home
        self._tmp.cleanup()


class OverviewPayloadTests(SaiHomeTestCase):
    def test_overview_totals_match_ledger(self):
        wallet = Wallet()
        wallet.record("earn", 0.010, source="sponsor:test_card")
        wallet.record("spend", -0.004, source="legacy:local_spend")

        payload = overview_payload()

        self.assertAlmostEqual(payload["balance"], 0.006)
        self.assertAlmostEqual(payload["total_earned"], 0.010)
        self.assertAlmostEqual(payload["total_spent"], 0.004)
        self.assertEqual(payload["entry_count"], 2)
        self.assertFalse(payload["local_wallet_authoritative"])
        self.assertFalse(payload["gateway_spends_wallet"])
        # Most recent entry first.
        self.assertEqual(payload["entries"][0]["kind"], "spend")
        self.assertIn("frequency", payload)
        self.assertIn("kill_switch", payload)

    def test_overview_works_without_login_or_wallet(self):
        payload = overview_payload()
        self.assertEqual(payload["balance"], 0)
        self.assertEqual(payload["entries"], [])
        self.assertIsNone(payload["api_key"])

class DashboardHttpTests(SaiHomeTestCase):
    def setUp(self):
        super().setUp()
        login()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _request(self, path, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_serves_dashboard_html(self):
        status, body = self._request("/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Sponsored AI Credits", text)
        self.assertIn("/api/overview", text)

    def test_gateway_responses_include_security_headers(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            headers = response.headers

        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_overview_endpoint_returns_wallet_state(self):
        Wallet().record("earn", 0.008, source="sponsor:test_card")
        status, body = self._request("/api/overview")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertAlmostEqual(payload["balance"], 0.008)
        self.assertEqual(payload["api_key"], load_config()["api_key"])

    def test_config_endpoint_updates_frequency_and_kill_switch(self):
        status, body = self._request("/api/config", payload={"frequency": "low"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["frequency"], "low")
        self.assertEqual(load_config()["frequency"], "low")

        status, body = self._request(
            "/api/config", payload={"kill_switch": True, "reason": "testing"}
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["kill_switch"])
        self.assertEqual(payload["kill_reason"], "testing")

    def test_config_endpoint_rejects_unknown_frequency(self):
        status, _body = self._request("/api/config", payload={"frequency": "warp"})
        self.assertEqual(status, 400)
        self.assertEqual(load_config()["frequency"], "normal")

    def test_config_endpoint_rejects_non_json_content_type(self):
        # A cross-site form/text-plain POST (which needs no CORS preflight) must
        # not be able to flip frequency/kill_switch via the loopback endpoint.
        body = json.dumps({"frequency": "low"}).encode("utf-8")
        request = urllib.request.Request(self.base + "/api/config", data=body)
        request.add_header("Content-Type", "text/plain")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 403)
        self.assertEqual(load_config()["frequency"], "normal")

    def test_config_endpoint_rejects_cross_origin(self):
        status, _body = self._request(
            "/api/config",
            payload={"frequency": "low"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(load_config()["frequency"], "normal")

    def test_config_endpoint_allows_same_origin_loopback(self):
        status, body = self._request(
            "/api/config",
            payload={"frequency": "low"},
            headers={"Origin": f"http://{urlparse(self.base).netloc}", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(load_config()["frequency"], "low")

    def test_dashboard_rejects_foreign_host_header(self):
        # A DNS-rebinding page reaches 127.0.0.1 but carries its own Host.
        status, _body = self._request("/api/overview", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_models_endpoint_still_requires_api_key(self):
        status, _body = self._request("/v1/models")
        self.assertEqual(status, 401)

    def test_upstream_errors_are_generic(self):
        previous_provider = os.environ.get("SAI_GATEWAY_PROVIDER")
        previous_key = os.environ.get("OPENAI_API_KEY")
        os.environ["SAI_GATEWAY_PROVIDER"] = "openai"
        os.environ.pop("OPENAI_API_KEY", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("SAI_GATEWAY_PROVIDER", previous_provider)
            if previous_provider is not None
            else os.environ.pop("SAI_GATEWAY_PROVIDER", None)
        )
        self.addCleanup(
            lambda: os.environ.__setitem__("OPENAI_API_KEY", previous_key)
            if previous_key is not None
            else os.environ.pop("OPENAI_API_KEY", None)
        )
        Wallet().record("earn", 1.0, source="test")

        status, body = self._request(
            "/v1/chat/completions",
            payload={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {load_config()['api_key']}"},
        )
        text = body.decode("utf-8")
        payload = json.loads(text)

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["message"], "Upstream provider request failed")
        self.assertNotIn("OPENAI_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
