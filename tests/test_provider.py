from datetime import datetime, timezone
from http.client import IncompleteRead
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

from x_digest import provider


REAL_URL = "https://x.com/researcher/status/12345"
INVENTED_URL = "https://x.com/invented/status/54321"
SETTINGS = {"categories": ["AI", "读书"], "editorial_preferences": ["质量优先"],
            "blocked_accounts": ["spam"], "target_count": 10}
NOW = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
SECRET = "test-secret-must-not-appear"


def response_data():
    return {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "status": "completed", "action": {
                "type": "search", "sources": [{"type": "url", "url": REAL_URL}]}},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"posts": [
                {"url": REAL_URL, "category": "AI", "summary": "测试用摘要", "reason": "测试理由"},
                {"url": INVENTED_URL, "category": "读书", "summary": "模型生成的未引用候选", "reason": "测试理由"},
            ]}), "annotations": []}]},
        ],
        "usage": {"total_tokens": 123},
    }


class ProviderTests(unittest.TestCase):
    def call(self, *, data=None, body=None, opener=None, **overrides):
        if opener is None:
            opener = Mock()
            raw = json.dumps(data if data is not None else response_data()).encode() if body is None else body
            opener.open.return_value = io.BytesIO(raw)
        args = {"base_url": "https://relay.example/v1", "api_key": SECRET,
                "model": "configured-search-model", "settings": SETTINGS,
                "seen_ids": {"789"}, "now": NOW}
        args.update(overrides)
        with patch.object(provider.request, "build_opener", return_value=opener):
            result = provider.search(**args)
        return result, opener

    def test_only_metadata_links_count_as_sources(self):
        result, _ = self.call()
        self.assertEqual(result.cited_urls, [REAL_URL])
        self.assertEqual(len(result.posts), 2)
        self.assertNotIn(INVENTED_URL, result.cited_urls)
        self.assertEqual(result.usage, {"total_tokens": 123})

    def test_annotations_provide_sources_but_still_need_completed_search(self):
        data = response_data()
        data["output"][0]["action"]["sources"] = []
        data["output"][1]["content"][0]["annotations"] = [
            {"type": "url_citation", "url": "https://twitter.com/researcher/status/12345?s=20"},
            {"type": "url_citation", "url": "https://example.com/article"},
        ]
        result, _ = self.call(data=data)
        self.assertEqual(result.cited_urls, [REAL_URL])

    def test_text_only_or_unfinished_search_is_not_treated_as_real_retrieval(self):
        for alteration in ("remove_search", "search_in_progress", "response_incomplete"):
            data = response_data()
            if alteration == "remove_search":
                data["output"].pop(0)
            elif alteration == "search_in_progress":
                data["output"][0]["status"] = "in_progress"
            else:
                data["status"] = "incomplete"
            with self.subTest(alteration=alteration), self.assertRaises(provider.ProviderError):
                self.call(data=data)

    def test_generated_urls_without_metadata_do_not_validate_themselves(self):
        data = response_data()
        data["output"][0]["action"]["sources"] = []
        with self.assertRaisesRegex(provider.ProviderError, "独立"):
            self.call(data=data)

    def test_markdown_json_is_rejected_without_fallback(self):
        data = response_data()
        data["output"][1]["content"][0]["text"] = "```json\n{\"posts\": []}\n```"
        with self.assertRaisesRegex(provider.ProviderError, "JSON"):
            self.call(data=data)

    def test_request_uses_required_search_domain_filter_and_structured_output(self):
        _, opener = self.call()
        self.assertEqual(opener.open.call_count, 1)
        req = opener.open.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "https://relay.example/v1/responses")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)
        self.assertEqual(payload["tool_choice"], "required")
        self.assertEqual(payload["tools"], [{"type": "web_search", "filters": {
            "allowed_domains": ["x.com", "twitter.com"]}}])
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_tool_calls"], 6)
        schema = payload["text"]["format"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["properties"]["posts"]["items"]["properties"]["category"]["enum"],
                         ["AI", "读书"])
        self.assertIn("789", payload["input"][1]["content"])
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_bad_endpoints_fail_before_any_request_and_do_not_echo_them(self):
        urls = ["http://relay.example/v1", "https://user:" + SECRET + "@relay.example/v1",
                "https://relay.example/v1?key=" + SECRET, "https://relay.example/v1#" + SECRET,
                "https://relay.example", "https://relay.example:bad/v1", "https://relay.example/v1\n"]
        for url in urls:
            with self.subTest(url=url), patch.object(provider.request, "build_opener") as build:
                with self.assertRaises(provider.ProviderError) as exc:
                    provider.search(base_url=url, api_key=SECRET, model="configured", settings=SETTINGS,
                                    seen_ids=set(), now=NOW)
                self.assertNotIn(SECRET, str(exc.exception))
                build.assert_not_called()

    def test_redirect_handler_stops_authorization_forwarding(self):
        req = request.Request("https://relay.example/v1/responses", headers={"Authorization": "Bearer " + SECRET})
        with self.assertRaises(provider.ProviderError) as exc:
            provider._NoRedirects().redirect_request(req, None, 302, "redirect", {},
                                                    "https://elsewhere.example/" + SECRET)
        self.assertNotIn(SECRET, str(exc.exception))
        self.assertNotIn("elsewhere", str(exc.exception))

    def test_http_and_connection_errors_are_sanitized_and_not_retried(self):
        for fault in [error.HTTPError("https://relay.example/?key=" + SECRET, 401, SECRET, {}, io.BytesIO(SECRET.encode())),
                      error.URLError("failed with Authorization Bearer " + SECRET),
                      TimeoutError(SECRET), IncompleteRead(SECRET.encode(), 999)]:
            opener = Mock()
            opener.open.side_effect = fault
            with self.subTest(fault=type(fault).__name__), self.assertRaises(provider.ProviderError) as exc:
                self.call(opener=opener)
            self.assertNotIn(SECRET, str(exc.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_oversized_or_invalid_json_response_is_not_partially_parsed(self):
        for body in [b"x" * (provider.MAX_RESPONSE_BYTES + 1), SECRET.encode(), b"\xff"]:
            with self.subTest(size=len(body)), self.assertRaises(provider.ProviderError) as exc:
                self.call(body=body)
            self.assertNotIn(SECRET, str(exc.exception))

    def test_missing_credentials_or_model_never_open_network(self):
        for override in [{"api_key": ""}, {"api_key": "bad\nkey"}, {"model": ""}]:
            with self.subTest(override=override), patch.object(provider.request, "build_opener") as build:
                with self.assertRaises(provider.ProviderError):
                    provider.search(base_url="https://relay.example/v1", api_key=override.get("api_key", SECRET),
                                    model=override.get("model", "configured"), settings=SETTINGS, seen_ids=set(), now=NOW)
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
