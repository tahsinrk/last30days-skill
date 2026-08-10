"""Tests for xai_x.py — error surfacing in parse_x_response().

xAI is the active X backend whenever XAI_API_KEY is set and Bird has no
cookies. Before these changes a malformed or empty payload returned `[]`, so a
broken backend rendered as a clean "no posts found" and there was no way to
tell a dead source from a quiet week. parse_x_response now raises, and the
callers in last30days.py convert that into a per-source error on the report.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lib import http, xai_x


VALID_PAYLOAD = {
    "output": [
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": '{"items": [{"url": "https://x.com/a/status/1", "text": "hi"}]}',
                }
            ],
        }
    ]
}


class TestParseXResponseRaisesOnBadPayload(unittest.TestCase):
    def test_empty_response_raises(self):
        with self.assertRaises(http.HTTPError):
            xai_x.parse_x_response({})

    def test_output_without_text_raises(self):
        with self.assertRaises(http.HTTPError):
            xai_x.parse_x_response({"output": []})

    def test_output_text_without_items_json_raises(self):
        payload = {"output": "the model replied in prose with no JSON at all"}
        with self.assertRaises(http.HTTPError):
            xai_x.parse_x_response(payload)

    def test_malformed_items_json_raises(self):
        payload = {"output": '{"items": [ this is not valid json '}
        with self.assertRaises(http.HTTPError):
            xai_x.parse_x_response(payload)

    def test_error_field_still_returns_empty_not_raise(self):
        # An explicit API error object is already logged and surfaced upstream
        # by the caller's own error handling; keep returning [] so behaviour
        # there is unchanged.
        result = xai_x.parse_x_response({"error": {"message": "rate limited"}})
        self.assertEqual(result, [])


class TestParseXResponseHappyPath(unittest.TestCase):
    def test_valid_payload_still_parses(self):
        items = xai_x.parse_x_response(VALID_PAYLOAD)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://x.com/a/status/1")

    def test_valid_payload_with_zero_items_is_not_an_error(self):
        # A genuinely quiet week must stay distinguishable from a broken source.
        payload = {"output": '{"items": []}'}
        self.assertEqual(xai_x.parse_x_response(payload), [])


if __name__ == "__main__":
    unittest.main()
