import urllib.error
import unittest
from unittest.mock import patch

from twbacktest.http import HttpClientError, JsonHttpClient, RetryPolicy


class FakeResponse:
    def __init__(self, body=b'{"ok": true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class HttpClientTests(unittest.TestCase):
    def test_transient_error_is_retried(self):
        client = JsonHttpClient(RetryPolicy(attempts=2, backoff_seconds=0))
        effects = [urllib.error.URLError("temporary"), FakeResponse()]
        with patch("urllib.request.urlopen", side_effect=effects) as request:
            self.assertTrue(client.get("https://example.test")["ok"])
            self.assertEqual(request.call_count, 2)

    def test_client_error_is_not_retried(self):
        client = JsonHttpClient(RetryPolicy(attempts=3, backoff_seconds=0))
        error = urllib.error.HTTPError("https://example.test", 404, "not found", {}, None)
        with patch("urllib.request.urlopen", side_effect=error) as request:
            with self.assertRaises(HttpClientError):
                client.get("https://example.test")
            self.assertEqual(request.call_count, 1)

    def test_redirect_loop_is_retried(self):
        client = JsonHttpClient(RetryPolicy(attempts=2, backoff_seconds=0))
        redirect = urllib.error.HTTPError("https://example.test", 308, "redirect loop", {}, None)
        with patch("urllib.request.urlopen", side_effect=[redirect, FakeResponse()]) as request:
            self.assertTrue(client.get("https://example.test")["ok"])
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
