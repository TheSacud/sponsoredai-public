import unittest
from unittest import mock

from sai.browser import is_safe_url, open_url


class IsSafeUrlTests(unittest.TestCase):
    def test_http_and_https_are_safe(self):
        self.assertTrue(is_safe_url("http://example.com"))
        self.assertTrue(is_safe_url("https://sponsoredai.dev/sponsor"))
        self.assertTrue(is_safe_url("HTTPS://EXAMPLE.COM"))  # scheme is case-insensitive

    def test_dangerous_schemes_are_unsafe(self):
        for url in (
            "file://attacker-host/share",
            "search-ms:query=foo",
            "ms-msdt:/id",
            "javascript:alert(1)",
            "data:text/html,<script>",
            "vbscript:msgbox",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_safe_url(url))

    def test_empty_and_none_are_unsafe(self):
        self.assertFalse(is_safe_url(None))
        self.assertFalse(is_safe_url(""))


class OpenUrlTests(unittest.TestCase):
    def test_opens_safe_url(self):
        with mock.patch("sai.browser.webbrowser.open") as opener:
            self.assertTrue(open_url("https://example.com"))
        opener.assert_called_once_with("https://example.com")

    def test_refuses_unsafe_url_without_opening(self):
        with mock.patch("sai.browser.webbrowser.open") as opener:
            self.assertFalse(open_url("file://attacker-host/share"))
        opener.assert_not_called()

    def test_swallows_os_error(self):
        with mock.patch("sai.browser.webbrowser.open", side_effect=OSError("no browser")) as opener:
            self.assertFalse(open_url("https://example.com"))
        opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
