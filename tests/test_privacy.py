import unittest

from sai.privacy import public_event_schema, sanitize_event


class PrivacyTests(unittest.TestCase):
    def test_sanitize_event_rejects_forbidden_fields(self):
        with self.assertRaises(ValueError):
            sanitize_event({"surface": "cli_agent_wait", "prompt": "secret"})

        with self.assertRaises(ValueError):
            sanitize_event({"surface": "cli_agent_wait", "command": "pytest"})

        with self.assertRaises(ValueError):
            sanitize_event({"surface": "cli_agent_wait", "api_key": "secret"})

    def test_sanitize_event_sets_upload_flags_false(self):
        event = sanitize_event({"surface": "cli_agent_wait", "tool": "codex"})
        self.assertFalse(event["code_uploaded"])
        self.assertFalse(event["prompt_uploaded"])
        self.assertFalse(event["logs_uploaded"])
        self.assertNotIn("terminal_output", event)

    def test_public_event_schema_separates_event_and_transport_keys(self):
        schema = public_event_schema()

        self.assertIn("event_allowed_keys", schema)
        self.assertIn("transport_keys", schema)
        self.assertNotIn("allowed_keys", schema)
        self.assertIn("duration_bucket", schema["event_allowed_keys"])
        self.assertIn("install_id_hash", schema["transport_keys"])
        self.assertIn("session_id", schema["transport_keys"])
        self.assertIn("command", schema["forbidden_keys"])
        self.assertIn("api_key", schema["forbidden_keys"])


if __name__ == "__main__":
    unittest.main()
