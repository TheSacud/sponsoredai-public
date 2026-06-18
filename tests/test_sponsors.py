import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from sai.ansi import ELLIPSIS, visible_length
from sai.config import USER_AGENT, load_config
from sai.sponsors import (
    AFK_ROTATION_LIMIT,
    LOCAL_SPONSORS,
    RemotePlacementClient,
    SponsorCard,
    SponsorSession,
    display_url,
    hash_install_id,
    install_auth_secret,
    resolve_install_secret,
)
from sai.wallet import Wallet


class _FakeResponse:
    def __init__(self, body=b'{"status": "ok"}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


class RemotePlacementClientHeaderTests(unittest.TestCase):
    def test_post_sends_sai_user_agent_not_urllib_default(self):
        # The edge's bot filter 403s the default "Python-urllib" UA, which would
        # silently break every placement fetch. The client must identify itself.
        captured = {}

        def fake_urlopen(request, timeout=None, **_kwargs):
            captured["request"] = request
            return _FakeResponse()

        client = RemotePlacementClient("https://backend.test", "deadbeef", "install-secret-xyz", timeout=1.0)
        with patch("sai.http_client.urllib.request.urlopen", side_effect=fake_urlopen):
            client._post("/v1/placements/next", {"x": 1})

        request = captured["request"]
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertTrue(USER_AGENT.startswith("sai-cli/"))
        self.assertNotIn("urllib", USER_AGENT.lower())

    def test_post_sends_install_secret_in_authorization_header(self):
        # The per-install credential authenticates the install to the backend and
        # must travel in the Authorization header, never the JSON body (which the
        # backend persists for placement events).
        captured = {}

        def fake_urlopen(request, timeout=None, **_kwargs):
            captured["request"] = request
            return _FakeResponse()

        client = RemotePlacementClient("https://backend.test", "deadbeef", "install-secret-xyz", timeout=1.0)
        with patch("sai.http_client.urllib.request.urlopen", side_effect=fake_urlopen):
            client._post("/v1/placements/next", {"x": 1})

        request = captured["request"]
        self.assertEqual(request.get_header("Authorization"), "SAI-Install install-secret-xyz")

    def test_next_placement_logs_remote_error_detail(self):
        client = RemotePlacementClient("https://backend.test", "deadbeef", "install-secret-xyz", timeout=1.0)
        with (
            patch(
                "sai.http_client.urllib.request.urlopen",
                side_effect=urllib.error.URLError(OSError("certificate verify failed")),
            ),
            self.assertLogs("sai.sponsors", level="INFO") as logs,
        ):
            self.assertIsNone(
                client.next_placement(
                    "desktop_overlay",
                    {},
                    terminal_is_interactive=True,
                    surface="desktop_overlay",
                )
            )

        line = "\n".join(logs.output)
        self.assertIn("path=/v1/installations/register", line)
        self.assertIn("surface=desktop_overlay", line)
        self.assertIn("URLError:OSError:certificate verify failed", line)

    def test_next_placement_logs_http_status_for_placement_fetch(self):
        client = RemotePlacementClient("https://backend.test", "deadbeef", "install-secret-xyz", timeout=1.0)
        calls = []

        def fake_urlopen(request, timeout=None, **_kwargs):
            calls.append(request.full_url)
            if request.full_url.endswith("/v1/installations/register"):
                return _FakeResponse(b'{"status": "registered"}')
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with (
            patch("sai.http_client.urllib.request.urlopen", side_effect=fake_urlopen),
            self.assertLogs("sai.sponsors", level="INFO") as logs,
        ):
            self.assertIsNone(
                client.next_placement(
                    "desktop_overlay",
                    {},
                    terminal_is_interactive=True,
                    surface="desktop_overlay",
                )
            )

        self.assertTrue(calls[-1].endswith("/v1/placements/next"))
        line = "\n".join(logs.output)
        self.assertIn("path=/v1/placements/next", line)
        self.assertIn("HTTPError:403:Forbidden", line)

    def test_install_auth_secret_is_deterministic_and_decoupled_from_hash(self):
        secret = install_auth_secret("ins_example")
        self.assertEqual(secret, install_auth_secret("ins_example"))
        self.assertNotEqual(secret, install_auth_secret("ins_other"))
        # The credential is not derivable from the public install_id_hash.
        self.assertNotEqual(secret, hash_install_id("ins_example"))
        self.assertEqual(len(secret), 64)


class InstallSecretResolutionTests(unittest.TestCase):
    def test_prefers_issued_secret_then_falls_back_to_derived(self):
        self.assertEqual(
            resolve_install_secret({"install_id": "ins_x", "install_secret": "issued-xyz"}),
            "issued-xyz",
        )
        # Blank/whitespace issued value falls back to the derived secret.
        self.assertEqual(
            resolve_install_secret({"install_id": "ins_x", "install_secret": "  "}),
            install_auth_secret("ins_x"),
        )
        self.assertEqual(resolve_install_secret({"install_id": "ins_x"}), install_auth_secret("ins_x"))
        self.assertIsNone(resolve_install_secret({}))

    def test_from_config_flags_issued_secret(self):
        issued = RemotePlacementClient.from_config(
            {"backend_url": "https://b.test", "install_id": "ins_x", "install_secret": "issued-xyz"}
        )
        self.assertEqual(issued.install_secret, "issued-xyz")
        self.assertTrue(issued.secret_is_issued)

        derived = RemotePlacementClient.from_config({"backend_url": "https://b.test", "install_id": "ins_x"})
        self.assertEqual(derived.install_secret, install_auth_secret("ins_x"))
        self.assertFalse(derived.secret_is_issued)


class IssuedSecretAdoptionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._prev_home = os.environ.get("SAI_HOME")
        os.environ["SAI_HOME"] = tmp.name
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._prev_home is None:
            os.environ.pop("SAI_HOME", None)
        else:
            os.environ["SAI_HOME"] = self._prev_home

    def _client(self, **kwargs):
        return RemotePlacementClient(
            "https://backend.test", "deadbeef", install_auth_secret("ins_x"), timeout=1.0, **kwargs
        )

    def _run_next_placement(self, client, register_body):
        captured = []

        def fake_urlopen(request, timeout=None, **_kwargs):
            body = request.data.decode("utf-8") if request.data else ""
            captured.append((request.full_url, body))
            if request.full_url.endswith("/v1/installations/register"):
                return _FakeResponse(register_body)
            return _FakeResponse(b'{"placement": null}')

        with patch("sai.http_client.urllib.request.urlopen", side_effect=fake_urlopen):
            card = client.next_placement("codex", {}, terminal_is_interactive=True)
        register_payload = next(body for url, body in captured if url.endswith("/register"))
        return card, register_payload

    def test_next_placement_requests_and_adopts_issued_secret(self):
        client = self._client()
        self.assertFalse(client.secret_is_issued)
        card, register_payload = self._run_next_placement(
            client, b'{"status": "registered", "install_secret": "issued-rand-abcdef123456"}'
        )

        self.assertIsNone(card)
        self.assertIn('"request_install_secret": true', register_payload)
        # Switched in-memory (so the same session's event posts use it)...
        self.assertEqual(client.install_secret, "issued-rand-abcdef123456")
        self.assertTrue(client.secret_is_issued)
        # ...and persisted as a credential for the next run.
        self.assertEqual(load_config()["install_secret"], "issued-rand-abcdef123456")

    def test_next_placement_skips_request_once_secret_is_issued(self):
        client = self._client(secret_is_issued=True)
        client.install_secret = "issued-existing"
        card, register_payload = self._run_next_placement(client, b'{"status": "registered"}')

        self.assertIsNone(card)
        self.assertIn('"request_install_secret": false', register_payload)
        # No issued secret in the response: nothing adopted or persisted.
        self.assertEqual(client.install_secret, "issued-existing")
        self.assertNotIn("install_secret", load_config())

    def test_adopt_ignores_response_without_issued_secret(self):
        client = self._client()
        for response in ({}, {"install_secret": ""}, {"install_secret": "   "}, "not-a-dict", None):
            client._adopt_issued_secret(response)
        self.assertEqual(client.install_secret, install_auth_secret("ins_x"))
        self.assertFalse(client.secret_is_issued)
        self.assertNotIn("install_secret", load_config())


def clear_ci_env():
    return {
        "BUILDKITE": "",
        "CI": "",
        "GITHUB_ACTIONS": "",
        "GITLAB_CI": "",
        "SAI_DISABLE_SPONSORS": "",
        "SAI_KILL_SWITCH": "",
        "TF_BUILD": "",
    }


class SponsorTests(unittest.TestCase):
    def test_local_sponsors_are_launch_placeholders(self):
        names = {card.sponsor for card in LOCAL_SPONSORS}
        urls = {card.url for card in LOCAL_SPONSORS}
        ids = {card.id for card in LOCAL_SPONSORS}

        self.assertEqual(names, {"Your Brand", "Paid Sponsor", "Launch Partner"})
        self.assertEqual(urls, {"https://sponsoredai.dev/sponsor"})
        self.assertTrue(all(not card_id.startswith("pilot_") for card_id in ids))

    def test_local_sponsors_are_unfunded_examples(self):
        # Every wallet unit must be backed by sponsor spend: example cards
        # shown without paid demand credit nothing and say so.
        for card in LOCAL_SPONSORS:
            self.assertTrue(card.is_example)
            self.assertEqual(card.credit_amount, 0.0)
            self.assertIn("example placement - no paid demand", card.footer())
            self.assertNotIn("+0.000", card.footer())

    def test_paid_card_footer_renders_tracked_hyperlink(self):
        card = SponsorCard(
            id="plc_1",
            sponsor="Acme",
            message="Ship faster",
            url="https://acme.example/sai",
            credit_amount=0.012,
            placement_id="plc_1",
            campaign_id="cmp_1",
            click_url="https://sponsoredai.dev/c/plc_1/clt_tok",
        )
        with patch.dict(os.environ, {"SAI_NO_HYPERLINKS": ""}):
            footer = card.footer()
        # The visible text is the compact URL; the click target is the
        # tracked redirect.
        self.assertIn("\x1b]8;;https://sponsoredai.dev/c/plc_1/clt_tok\x1b\\acme.example/sai\x1b]8;;\x1b\\", footer)

    def test_paid_card_footer_budgets_message_and_keeps_credits(self):
        card = SponsorCard(
            id="plc_1",
            sponsor="Acme",
            message="m" * 200,
            url="https://acme.example/sai?utm_source=sai",
            credit_amount=0.012,
            placement_id="plc_1",
            campaign_id="cmp_1",
        )
        footer = card.footer(width=100)
        self.assertLessEqual(visible_length(footer), 100)
        self.assertIn(ELLIPSIS, footer)
        self.assertIn("Acme", footer)
        self.assertIn("+0.012 AI credits", footer)
        self.assertIn("acme.example/sai", footer)

    def test_footer_uses_accent_rail_and_drops_nerd_font_icon(self):
        # The accent-rail design (variant A) replaced the fragile Nerd Font
        # bullhorn; the line now leads with a rail + a dim "sponsored" tag.
        card = SponsorCard(
            id="plc_1",
            sponsor="Acme",
            message="Ship faster",
            url="https://acme.example/sai",
            credit_amount=0.012,
            placement_id="plc_1",
            campaign_id="cmp_1",
        )
        footer = card.footer()
        self.assertNotIn("\uf0a1", footer)  # no more Nerd Font glyph
        self.assertIn("sponsored", footer)
        self.assertIn("Acme", footer)

    def test_display_url_drops_scheme_and_query(self):
        self.assertEqual(
            display_url("https://sponsoredai.dev/sponsor?sai_placement=plc_1"),
            "sponsoredai.dev/sponsor",
        )
        self.assertEqual(display_url("https://acme.example/" + "p" * 60), "acme.example")

    def test_paid_card_footer_is_plain_without_click_url_or_when_disabled(self):
        card = SponsorCard(
            id="plc_1",
            sponsor="Acme",
            message="Ship faster",
            url="https://acme.example/sai",
            credit_amount=0.012,
            placement_id="plc_1",
            campaign_id="cmp_1",
            click_url="https://sponsoredai.dev/c/plc_1/clt_tok",
        )
        plain = SponsorCard(
            id="plc_2",
            sponsor="Acme",
            message="Ship faster",
            url="https://acme.example/sai",
            credit_amount=0.012,
            placement_id="plc_2",
            campaign_id="cmp_1",
        )
        with patch.dict(os.environ, {"SAI_NO_HYPERLINKS": "1"}):
            self.assertNotIn("\x1b]8;", card.footer())
        self.assertNotIn("\x1b]8;", plain.footer())

    def test_session_settles_example_cards_without_earning(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                card = session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True)

            self.assertIsNotNone(card)
            self.assertEqual(session.qualified_waits, 0)
            self.assertEqual(session.settle(now=14.0), 0.0)
            self.assertEqual(session.qualified_waits, 0)
            self.assertEqual(session.settle(now=16.0), 0.0)
            self.assertEqual(session.qualified_waits, 1)
            self.assertEqual(wallet.balance(), 0.0)

    def test_session_does_not_qualify_card_hidden_before_five_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                card = session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True)

            self.assertIsNotNone(card)
            session.mark_cards_hidden(now=14.0)
            self.assertEqual(session.settle(now=30.0), 0.0)
            self.assertEqual(wallet.balance(), 0.0)

    def test_session_ignores_non_interactive_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet)

            card = session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=False)

            self.assertIsNone(card)
            self.assertEqual(session.settle(), 0.0)

    def test_remote_session_records_rendered_and_qualified_events(self):
        class FakePlacementClient:
            def __init__(self):
                self.events = []

            def next_placement(self, tool, config, terminal_is_interactive, surface=None):
                return SponsorCard(
                    id="plc_test",
                    sponsor="Remote Sponsor",
                    message="Remote creative",
                    url="https://sponsor.example",
                    credit_amount=0.01,
                    placement_id="plc_test",
                    campaign_id="cmp_test",
                )

            def record_event(self, placement_id, payload):
                self.events.append((placement_id, payload))
                if payload["event"] == "qualified_5s":
                    return {"accepted": True, "billable": True}
                return {"accepted": True, "billable": False}

        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            client = FakePlacementClient()
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet, placement_client=client)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNotNone(session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True))

            self.assertEqual(client.events[0][1]["event"], "rendered")
            self.assertEqual(session.settle(now=16.0), 0.01)
            self.assertEqual(client.events[1][1]["event"], "qualified_5s")
            self.assertEqual(client.events[1][1]["visible_seconds"], 6.0)
            self.assertEqual(wallet.balance(), 0.01)

    def test_remote_session_reports_reward_progress_for_active_real_card(self):
        class FakePlacementClient:
            def next_placement(self, tool, config, terminal_is_interactive, surface=None):
                return SponsorCard(
                    id="plc_test",
                    sponsor="Remote Sponsor",
                    message="Remote creative",
                    url="https://sponsor.example",
                    credit_amount=0.01,
                    placement_id="plc_test",
                    campaign_id="cmp_test",
                )

            def record_event(self, placement_id, payload):
                return {"accepted": True, "billable": False}

        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(
                tool="codex",
                config=config,
                wallet=wallet,
                placement_client=FakePlacementClient(),
            )

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNotNone(session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True))

            progress = session.reward_progress(now=12.0)
            self.assertIsNotNone(progress)
            self.assertEqual(progress["visible_seconds"], 2.0)
            self.assertEqual(progress["remaining_seconds"], 3.0)
            self.assertEqual(progress["progress"], 0.4)
            self.assertFalse(progress["eligible"])
            self.assertTrue(session.reward_progress(now=15.0)["eligible"])
            session.mark_cards_hidden(now=16.0)
            self.assertIsNone(session.reward_progress(now=17.0))

    def test_example_cards_do_not_report_reward_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet, placement_client=None)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNotNone(session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True))

            self.assertIsNone(session.reward_progress(now=12.0))

    def test_remote_session_uses_backend_confirmed_earned_amount(self):
        class FakePlacementClient:
            def next_placement(self, tool, config, terminal_is_interactive, surface=None):
                return SponsorCard(
                    id="plc_test",
                    sponsor="Remote Sponsor",
                    message="Remote creative",
                    url="https://sponsor.example",
                    credit_amount=0.01,
                    placement_id="plc_test",
                    campaign_id="cmp_test",
                )

            def record_event(self, placement_id, payload):
                if payload["event"] == "qualified_5s":
                    return {"accepted": True, "billable": True, "earned": 0.004}
                return {"accepted": True, "billable": False}

        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(
                tool="codex",
                config=config,
                wallet=wallet,
                placement_client=FakePlacementClient(),
            )

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNotNone(session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True))

            self.assertEqual(session.settle(now=16.0), 0.004)
            self.assertEqual(wallet.balance(), 0.004)

    def test_remote_session_backs_off_when_backend_has_no_placement(self):
        class EmptyThenFilledPlacementClient:
            def __init__(self):
                self.calls = 0

            def next_placement(self, tool, config, terminal_is_interactive, surface=None):
                self.calls += 1
                if self.calls == 1:
                    return None
                return SponsorCard(
                    id="plc_test",
                    sponsor="Remote Sponsor",
                    message="Remote creative",
                    url="https://sponsor.example",
                    credit_amount=0.01,
                    placement_id="plc_test",
                    campaign_id="cmp_test",
                )

            def record_event(self, placement_id, payload):
                return {"accepted": True, "billable": False}

        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            client = EmptyThenFilledPlacementClient()
            config = {"frequency": "high", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet, placement_client=client)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNone(session.maybe_card(now=10.0, idle_for=6.0, terminal_is_interactive=True))
                self.assertIsNone(session.maybe_card(now=10.2, idle_for=6.2, terminal_is_interactive=True))
                card = session.maybe_card(now=20.1, idle_for=16.1, terminal_is_interactive=True)

            self.assertEqual(client.calls, 2)
            self.assertIsNotNone(card)


    def test_carousel_rotates_to_next_card_after_rotate_seconds(self):
        class CountingPlacementClient:
            def __init__(self):
                self.calls = 0

            def next_placement(self, tool, config, terminal_is_interactive, surface=None):
                self.calls += 1
                return SponsorCard(
                    id=f"plc_{self.calls}",
                    sponsor=f"Sponsor {self.calls}",
                    message="Creative",
                    url="https://sponsor.example",
                    credit_amount=0.01,
                    placement_id=f"plc_{self.calls}",
                    campaign_id="cmp_test",
                )

            def record_event(self, placement_id, payload):
                return {"accepted": True, "billable": False}

        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            client = CountingPlacementClient()
            # "normal" rotates every 45s; idle threshold is 10s.
            config = {"frequency": "normal", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet, placement_client=client)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                first = session.maybe_card(now=10.0, idle_for=10.0, terminal_is_interactive=True)
                # Still idle but inside the 45s display window: no rotation yet.
                held = session.maybe_card(now=40.0, idle_for=40.0, terminal_is_interactive=True)
                # Past 45s and still idle: advance to the next placement.
                rotated = session.maybe_card(now=56.0, idle_for=56.0, terminal_is_interactive=True)

            self.assertIsNotNone(first)
            self.assertIsNone(held)
            self.assertIsNotNone(rotated)
            self.assertEqual(first.placement_id, "plc_1")
            self.assertEqual(rotated.placement_id, "plc_2")
            self.assertEqual(client.calls, 2)
            # The first card's billing window closed when the second was shown.
            self.assertEqual(session.cards[0].visible_until, 56.0)


    def test_carousel_pauses_when_user_is_afk(self):
        class CountingPlacementClient:
            def __init__(self):
                self.calls = 0

            def next_placement(self, tool, config, terminal_is_interactive, surface=None):
                self.calls += 1
                return SponsorCard(
                    id=f"plc_{self.calls}",
                    sponsor=f"Sponsor {self.calls}",
                    message="Creative",
                    url="https://sponsor.example",
                    credit_amount=0.01,
                    placement_id=f"plc_{self.calls}",
                    campaign_id="cmp_test",
                )

            def record_event(self, placement_id, payload):
                return {"accepted": True, "billable": False}

        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            client = CountingPlacementClient()
            config = {"frequency": "normal", "ads_enabled": True}
            session = SponsorSession(tool="codex", config=config, wallet=wallet, placement_client=client)

            env = clear_ci_env()
            env["SAI_HOME"] = tmp
            with patch.dict(os.environ, env, clear=False):
                now = 10.0
                shown = 0
                # The carousel rotates up to the AFK limit with no keypress.
                for _ in range(AFK_ROTATION_LIMIT):
                    if session.maybe_card(now=now, idle_for=now, terminal_is_interactive=True):
                        shown += 1
                    now += 45.0
                self.assertEqual(shown, AFK_ROTATION_LIMIT)
                # Past the cap and still no keypress: the carousel pauses.
                self.assertIsNone(session.maybe_card(now=now, idle_for=now, terminal_is_interactive=True))
                now += 45.0
                # A keypress marks the user present and the carousel resumes.
                session.note_user_input()
                self.assertIsNotNone(session.maybe_card(now=now, idle_for=now, terminal_is_interactive=True))

            self.assertEqual(client.calls, AFK_ROTATION_LIMIT + 1)


    def test_example_cards_cycle_round_robin_without_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = Wallet(Path(tmp) / "wallet.json")
            # No placement_client -> example-card fallback.
            session = SponsorSession(
                tool="codex",
                config={"frequency": "high", "ads_enabled": True},
                wallet=wallet,
            )
            self.assertIsNone(session.placement_client)
            picks = [session._next_card(True) for _ in range(len(LOCAL_SPONSORS) + 1)]
            # Consecutive example cards differ, and the cursor wraps around.
            self.assertEqual([c.id for c in picks[: len(LOCAL_SPONSORS)]], [c.id for c in LOCAL_SPONSORS])
            self.assertEqual(picks[len(LOCAL_SPONSORS)].id, LOCAL_SPONSORS[0].id)


if __name__ == "__main__":
    unittest.main()
