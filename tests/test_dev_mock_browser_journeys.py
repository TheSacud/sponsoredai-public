import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from sai.dev_mock import start_mock_lab


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional local browser harness
    sync_playwright = None


ENV_KEYS = (
    "SAI_HOME",
    "SAI_SESSION_SECRET",
    "SAI_DEV_MODE",
    "SAI_ADMIN_EMAILS",
    "SAI_ENABLE_PASSWORD_AUTH",
    "SAI_ENABLE_PASSWORD_REGISTRATION",
    "SAI_ALLOW_NON_LAUNCH_PASSWORD_AUTH",
    "SAI_AUTH_EMAIL_OUTBOX",
    "SAI_NO_WALLET_SPEND",
    "SAI_GATEWAY_PROVIDER",
    "SAI_PROVIDER",
    "SAI_UPSTREAM_BASE_URL",
    "SAI_UPSTREAM_API_KEY",
    "SAI_UPSTREAM_PROVIDER",
    "SAI_GATEWAY_MOCK_DELAY_MS",
    "SAI_DEVELOPER_SETTLEMENT_HOLD_SECONDS",
    "SAI_ALLOW_UNSAFE_SEASONING",
    "SAI_DEVELOPER_FUNDING_SEASONING_SECONDS",
    "SAI_BETA_DEVELOPER_HOURLY_CAP_MICROS",
    "SAI_BETA_GLOBAL_HOURLY_CAP_MICROS",
    "SAI_BETA_DEVELOPER_DAILY_CAP_MICROS",
    "SAI_BETA_GLOBAL_DAILY_CAP_MICROS",
)

SPONSOR_CAMPAIGN_SELECTORS = (
    'data-testid="sponsor-campaign-form"',
    'data-testid="campaign-ad-line"',
    'data-testid="campaign-destination-url"',
    'data-testid="campaign-brand-name"',
    'data-testid="campaign-brand-icon-url"',
    'data-testid="campaign-target-countries"',
    'data-testid="campaign-bid-per-block"',
    'data-testid="campaign-blocks"',
    'data-testid="campaign-public-leaderboard"',
    'data-testid="campaign-estimated-total"',
    'data-testid="campaign-forecast-views"',
    'data-testid="campaign-forecast-developer"',
    'data-testid="campaign-forecast-clicks"',
    'data-testid="campaign-forecast-position"',
    'data-testid="campaign-forecast-position-note"',
)

DEVELOPER_DASHBOARD_SELECTORS = (
    'data-testid="developer-dashboard-header"',
    'data-testid="developer-earnings-stats"',
    'data-testid="developer-activation-card"',
    'data-testid="developer-activation-steps"',
    'data-testid="developer-command-list"',
    'data-testid="developer-payouts-card"',
    'data-testid="developer-payouts-checklist"',
    'data-testid="developer-earnings-chart"',
    'data-testid="developer-chart-range-24h"',
    'data-testid="developer-chart-range-7d"',
    'data-testid="developer-chart-range-30d"',
    'data-testid="developer-chart-svg"',
    'data-testid="developer-earning-limits"',
    'data-testid="developer-installations-card"',
    'data-testid="developer-installations-table"',
    'data-testid="developer-link-form"',
    'data-testid="developer-pairing-code"',
    'data-testid="developer-link-label"',
    'data-testid="developer-link-submit"',
    'data-testid="developer-recent-activity-card"',
    'data-testid="developer-recent-activity-table"',
)

ADMIN_CONSOLE_SELECTORS = (
    'data-testid="admin-dashboard-header"',
    'data-testid="admin-stats-grid"',
    'data-testid="admin-accounts-card"',
    'data-testid="admin-web-analytics-card"',
    'data-testid="admin-system-logs-card"',
    'data-testid="admin-installations-card"',
    'data-testid="admin-campaign-status-card"',
    'data-testid="admin-placement-events-card"',
    'data-testid="admin-landing-funnel-card"',
    'data-testid="admin-invalid-placements-card"',
    'data-testid="admin-payout-queue-card"',
    'data-testid="admin-campaign-queue-card"',
    'data-testid="admin-ledger-card"',
)


class MockLabEnvMixin:
    def setUp(self):
        self._previous_env = {key: os.environ.get(key) for key in ENV_KEYS}

    def tearDown(self):
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def start_lab(self, root: Path):
        return start_mock_lab(
            host="127.0.0.1",
            backend_port=0,
            gateway_port=0,
            lab_port=0,
            home=root,
            wait_seconds=1.0,
        )


class MockLabBrowserJourneyContractTests(MockLabEnvMixin, unittest.TestCase):
    def test_mock_lab_root_exposes_stable_browser_selectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            try:
                with urllib.request.urlopen(running.state.lab_url, timeout=10) as response:
                    body = response.read().decode("utf-8")
            finally:
                running.stop()

        for selector in (
            'data-testid="mock-lab"',
            'data-testid="open-developer-dashboard"',
            'data-testid="open-sponsor-console"',
            'data-testid="open-admin-console"',
            'data-testid="start-wait"',
            'data-testid="simulate-placement"',
            'data-testid="wallet-balance"',
            'data-testid="commands-json"',
            'data-testid="market-rows"',
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, body)

    def test_auto_login_links_land_on_expected_backend_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            try:
                self.assert_app_page(
                    opener,
                    f"{running.state.lab_url}/as/developer",
                    expected_path="/dashboard",
                    expected_page="developer-dashboard",
                    expected_text="Earnings",
                )
                self.assert_app_page(
                    opener,
                    f"{running.state.lab_url}/as/sponsor",
                    expected_path="/sponsor",
                    expected_page="sponsor",
                    expected_text="Create a sponsored placement",
                )
                self.assert_app_page(
                    opener,
                    f"{running.state.lab_url}/as/admin",
                    expected_path="/admin",
                    expected_page="admin",
                    expected_text="Admin dashboard",
                )
            finally:
                running.stop()

    def assert_app_page(
        self,
        opener: urllib.request.OpenerDirector,
        url: str,
        *,
        expected_path: str,
        expected_page: str,
        expected_text: str,
    ) -> None:
        with opener.open(url, timeout=10) as response:
            final_url = response.geturl()
            body = response.read().decode("utf-8")

        self.assertEqual(urlparse(final_url).path, expected_path)
        self.assertIn(f'data-page="{expected_page}"', body)
        self.assertIn(expected_text, body)

    def test_sponsor_console_exposes_campaign_checkout_selectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            try:
                with opener.open(f"{running.state.lab_url}/as/sponsor", timeout=10) as response:
                    final_url = response.geturl()
                    body = response.read().decode("utf-8")
            finally:
                running.stop()

        self.assertEqual(urlparse(final_url).path, "/sponsor")
        self.assertIn('data-page="sponsor"', body)
        for selector in SPONSOR_CAMPAIGN_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn(selector, body)
        self.assertRegex(body, r'data-testid="campaign-checkout-(stripe|crypto)"')

    def test_developer_dashboard_exposes_browser_journey_selectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            try:
                with opener.open(f"{running.state.lab_url}/as/developer", timeout=10) as response:
                    final_url = response.geturl()
                    body = response.read().decode("utf-8")
            finally:
                running.stop()

        self.assertEqual(urlparse(final_url).path, "/dashboard")
        self.assertIn('data-page="developer-dashboard"', body)
        for selector in DEVELOPER_DASHBOARD_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn(selector, body)

    def test_admin_console_exposes_browser_journey_selectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            try:
                with opener.open(f"{running.state.lab_url}/as/admin", timeout=10) as response:
                    final_url = response.geturl()
                    body = response.read().decode("utf-8")
            finally:
                running.stop()

        self.assertEqual(urlparse(final_url).path, "/admin")
        self.assertIn('data-page="admin"', body)
        for selector in ADMIN_CONSOLE_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn(selector, body)


@unittest.skipUnless(sync_playwright is not None, "playwright is not installed")
class MockLabPlaywrightJourneyTests(MockLabEnvMixin, unittest.TestCase):
    def _launch_chromium_or_skip(self, playwright):
        try:
            return playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - depends on local browser install
            self.skipTest(f"Playwright Chromium is unavailable: {type(exc).__name__}")

    def _wallet_balance(self, page) -> float:
        return float(page.get_by_test_id("wallet-balance").inner_text(timeout=5000))

    def test_auto_login_links_render_backend_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            try:
                with sync_playwright() as p:
                    browser = self._launch_chromium_or_skip(p)
                    try:
                        page = browser.new_page()
                        journeys = (
                            ("open-developer-dashboard", "**/dashboard", '[data-page="developer-dashboard"]'),
                            ("open-sponsor-console", "**/sponsor", '[data-page="sponsor"]'),
                            ("open-admin-console", "**/admin", '[data-page="admin"]'),
                        )
                        for test_id, url_pattern, page_selector in journeys:
                            with self.subTest(test_id=test_id):
                                page.goto(running.state.lab_url)
                                page.get_by_test_id(test_id).click()
                                page.wait_for_url(url_pattern, timeout=5000)
                                page.locator(page_selector).wait_for(timeout=5000)
                    finally:
                        browser.close()
            finally:
                running.stop()

    def test_root_controls_update_browser_state_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            try:
                with sync_playwright() as p:
                    browser = self._launch_chromium_or_skip(p)
                    try:
                        page = browser.new_page()
                        page.goto(running.state.lab_url)
                        page.get_by_test_id("mock-lab").wait_for(timeout=5000)
                        page.wait_for_function(
                            """() => {
                                const backend = document.querySelector('[data-testid="backend-url"]');
                                const gateway = document.querySelector('[data-testid="gateway-url"]');
                                return backend?.textContent?.startsWith('http://127.0.0.1:')
                                    && gateway?.textContent?.startsWith('http://127.0.0.1:');
                            }""",
                            timeout=5000,
                        )
                        page.wait_for_function(
                            """() => {
                                const text = document.querySelector('[data-testid="commands-json"]')?.textContent || '';
                                return text.includes('"vscode_settings"')
                                    && text.includes('"sai.gateway.port"');
                            }""",
                            timeout=5000,
                        )
                        page.wait_for_function(
                            """() => document.querySelectorAll('[data-testid="market-rows"] tr').length > 0""",
                            timeout=5000,
                        )

                        initial_balance = self._wallet_balance(page)
                        page.get_by_test_id("start-wait").click()
                        page.wait_for_function(
                            """() => {
                                const text = document.querySelector('[data-testid="wait-state"]')?.textContent || '';
                                return text.includes('"status": "running"')
                                    || text.includes('"status": "completed"');
                            }""",
                            timeout=5000,
                        )
                        page.wait_for_function(
                            """() => {
                                const text = document.querySelector('[data-testid="wait-state"]')?.textContent || '';
                                return text.includes('"status": "completed"');
                            }""",
                            timeout=7000,
                        )

                        page.get_by_test_id("add-local-credit").click()
                        page.wait_for_function(
                            """(initial) => {
                                const value = Number(document.querySelector('[data-testid="wallet-balance"]')?.textContent);
                                const action = document.querySelector('[data-testid="last-action"]')?.textContent || '';
                                return value >= initial + 0.249
                                    && action.includes('"kind": "local_credit"');
                            }""",
                            arg=initial_balance,
                            timeout=5000,
                        )

                        credited_balance = self._wallet_balance(page)
                        page.get_by_test_id("spend-local-credit").click()
                        page.wait_for_function(
                            """(credited) => {
                                const value = Number(document.querySelector('[data-testid="wallet-balance"]')?.textContent);
                                const action = document.querySelector('[data-testid="last-action"]')?.textContent || '';
                                return value <= credited - 0.099
                                    && action.includes('"kind": "local_spend"');
                            }""",
                            arg=credited_balance,
                            timeout=5000,
                        )
                    finally:
                        browser.close()
            finally:
                running.stop()

    def test_sponsor_checkout_forecast_and_validation_journey(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            try:
                with sync_playwright() as p:
                    browser = self._launch_chromium_or_skip(p)
                    try:
                        page = browser.new_page()
                        page.goto(f"{running.state.lab_url}/as/sponsor")
                        page.wait_for_url("**/sponsor", timeout=5000)
                        page.get_by_test_id("sponsor-campaign-form").wait_for(timeout=5000)

                        page.get_by_test_id("campaign-bid-per-block").fill("30.00")
                        page.get_by_test_id("campaign-blocks").fill("2")
                        page.wait_for_function(
                            """() =>
                                document.querySelector('[data-testid="campaign-estimated-total"]')?.textContent === "$60.00"
                                && document.querySelector('[data-testid="campaign-forecast-views"]')?.textContent === "2,000"
                                && document.querySelector('[data-testid="campaign-forecast-developer"]')?.textContent === "$36.00"
                                && document.querySelector('[data-testid="campaign-forecast-clicks"]')?.textContent === "$30.00"
                            """,
                            timeout=5000,
                        )

                        destination = page.get_by_test_id("campaign-destination-url")
                        destination.fill("http://localhost/callback")
                        validation_message = destination.evaluate("(input) => input.validationMessage")
                        self.assertIn("public HTTPS URL", validation_message)
                    finally:
                        browser.close()
            finally:
                running.stop()

    def test_developer_and_admin_console_browser_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = self.start_lab(Path(tmp))
            try:
                with sync_playwright() as p:
                    browser = self._launch_chromium_or_skip(p)
                    try:
                        page = browser.new_page()

                        page.goto(f"{running.state.lab_url}/as/developer")
                        page.wait_for_url("**/dashboard", timeout=5000)
                        page.get_by_test_id("developer-dashboard-header").wait_for(timeout=5000)
                        page.get_by_test_id("developer-earnings-stats").wait_for(timeout=5000)
                        page.get_by_test_id("developer-payouts-card").wait_for(timeout=5000)
                        page.get_by_test_id("developer-installations-card").wait_for(timeout=5000)
                        page.get_by_test_id("developer-chart-range-24h").click()
                        self.assertEqual(
                            page.get_by_test_id("developer-chart-range-24h").get_attribute("aria-pressed"),
                            "true",
                        )

                        page.goto(f"{running.state.lab_url}/as/admin")
                        page.wait_for_url("**/admin", timeout=5000)
                        for test_id in (
                            "admin-dashboard-header",
                            "admin-stats-grid",
                            "admin-system-logs-card",
                            "admin-payout-queue-card",
                            "admin-campaign-queue-card",
                        ):
                            page.get_by_test_id(test_id).wait_for(timeout=5000)
                    finally:
                        browser.close()
            finally:
                running.stop()


if __name__ == "__main__":
    unittest.main()
