import types
import unittest
from unittest import mock

from sai import http_client


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        http_client.reset_for_tests()

    def tearDown(self):
        http_client.reset_for_tests()

    def test_urlopen_prefers_certifi_context_when_available(self):
        request = object()
        context = object()
        fake_certifi = types.SimpleNamespace(where=lambda: "/tmp/cacert.pem")

        with (
            mock.patch.object(http_client, "certifi", fake_certifi),
            mock.patch.object(http_client.ssl, "create_default_context", return_value=context) as create_context,
            mock.patch.object(http_client.urllib.request, "urlopen", return_value="ok") as urlopen,
        ):
            self.assertEqual(http_client.urlopen(request, timeout=3.0), "ok")

        create_context.assert_called_once_with(cafile="/tmp/cacert.pem")
        urlopen.assert_called_once_with(request, timeout=3.0, context=context)

    def test_urlopen_falls_back_to_default_urllib_without_certifi(self):
        request = object()

        with (
            mock.patch.object(http_client, "certifi", None),
            mock.patch.object(http_client.ssl, "create_default_context") as create_context,
            mock.patch.object(http_client.urllib.request, "urlopen", return_value="ok") as urlopen,
        ):
            self.assertEqual(http_client.urlopen(request, timeout=2.0), "ok")

        create_context.assert_not_called()
        urlopen.assert_called_once_with(request, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
