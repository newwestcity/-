from datetime import datetime, timezone
from http.client import IncompleteRead
import io
import json
from threading import Barrier, Lock
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
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 360)
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

    def test_request_accepts_bounded_output_and_timeout_overrides(self):
        settings = dict(SETTINGS, max_output_tokens=3000)
        _, opener = self.call(settings=settings, timeout=120)
        self.assertEqual(json.loads(opener.open.call_args.args[0].data)["max_output_tokens"], 3000)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)

    def test_invalid_output_or_timeout_fails_before_network(self):
        cases = [{"settings": dict(SETTINGS, max_output_tokens=value)}
                 for value in (0, True, 3000.0, 10001, "3000")]
        cases += [{"timeout": value} for value in (0, True, -1, 361, float("nan"))]
        for override in cases:
            with self.subTest(override=override), patch.object(provider.request, "build_opener") as build:
                with self.assertRaises(provider.ProviderError):
                    provider.search(base_url="https://relay.example/v1", api_key=SECRET, model="configured",
                                    settings=override.get("settings", SETTINGS), seen_ids=set(), now=NOW,
                                    timeout=override.get("timeout", 360))
                build.assert_not_called()


class CategorySearchTests(unittest.TestCase):
    def call(self, settings=None):
        return provider.search_by_category(base_url="https://relay.example/v1", api_key=SECRET,
                                           model="configured-search-model", settings=settings or SETTINGS,
                                           seen_ids={"789"}, now=NOW)

    def test_each_category_gets_one_strict_bounded_request_with_two_workers(self):
        categories = ["科技", "AI", "股市", "投资", "读书", "学生羊毛"]
        settings = dict(SETTINGS, categories=categories, max_tool_calls=6)
        original = json.loads(json.dumps(settings))
        lock = Lock()
        pair = Barrier(2, timeout=5)
        active = 0
        peak = 0
        calls = []

        def respond(req, *, timeout):
            nonlocal active, peak
            payload = json.loads(req.data)
            enum = payload["text"]["format"]["schema"]["properties"]["posts"]["items"]["properties"]["category"]["enum"]
            self.assertEqual(len(enum), 1)
            category = enum[0]
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append((category, payload, timeout))
            try:
                pair.wait()
                index = categories.index(category)
                url = f"https://x.com/author{index}/status/{12345 + index}"
                data = response_data()
                data["output"][0]["action"]["sources"] = [{"type": "url", "url": url}]
                data["output"][1]["content"][0]["text"] = json.dumps({"posts": [{
                    "url": url, "category": category, "summary": "已引用的摘要", "reason": "测试理由",
                }]})
                data["usage"] = {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
                                 "provider_detail": SECRET}
                return io.BytesIO(json.dumps(data).encode())
            finally:
                with lock:
                    active -= 1

        opener = Mock()
        opener.open.side_effect = respond
        with patch.object(provider.request, "build_opener", return_value=opener):
            result = self.call(settings)
        self.assertEqual(peak, 2)
        self.assertEqual(opener.open.call_count, len(categories))
        self.assertCountEqual([category for category, _, _ in calls], categories)
        self.assertEqual([post["category"] for post in result.posts], categories)
        self.assertEqual(len(result.cited_urls), len(categories))
        self.assertEqual(result.usage, {"input_tokens": 6, "output_tokens": 12, "total_tokens": 18})
        self.assertEqual(settings, original)
        for _, payload, timeout in calls:
            self.assertEqual(timeout, 120)
            self.assertEqual(payload["max_tool_calls"], 1)
            self.assertEqual(payload["max_output_tokens"], 3000)
            self.assertIn("目标最终 2 条", payload["input"][1]["content"])
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertEqual(payload["include"], ["web_search_call.action.sources"])

    def test_partial_failure_is_skipped_without_retry_or_error_text(self):
        def search(**kwargs):
            if kwargs["settings"]["categories"] == ["读书"]:
                raise provider.ProviderError(SECRET)
            return provider.SearchResult([{"category": "AI"}], [REAL_URL], {"total_tokens": 5})

        with patch.object(provider, "search", side_effect=search) as run, patch("builtins.print") as output:
            result = self.call()
        self.assertEqual(run.call_count, len(SETTINGS["categories"]))
        self.assertEqual(result.posts, [{"category": "AI"}])
        self.assertEqual(result.cited_urls, [REAL_URL])
        self.assertEqual(result.usage, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 5})
        self.assertNotIn(SECRET, repr(result))
        output.assert_not_called()

    def test_all_category_failures_raise_only_fixed_error_without_retry(self):
        with patch.object(provider, "search", side_effect=provider.ProviderError(SECRET)) as run:
            with self.assertRaises(provider.ProviderError) as raised:
                self.call()
        self.assertEqual(run.call_count, len(SETTINGS["categories"]))
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("所有分类", str(raised.exception))

    def test_budget_and_local_configuration_are_checked_before_any_request(self):
        invalid = [dict(SETTINGS, max_tool_calls=1), dict(SETTINGS, max_tool_calls=True),
                   dict(SETTINGS, categories=["AI", "AI"]), dict(SETTINGS, target_count=0),
                   dict(SETTINGS, categories=[]), dict(SETTINGS, max_output_tokens=0)]
        for settings in invalid:
            with self.subTest(settings=settings), patch.object(provider, "search") as run:
                with self.assertRaises(provider.ProviderError):
                    self.call(settings)
                run.assert_not_called()

    def test_target_is_capped_at_original_target_and_usage_is_fixed_integer_totals(self):
        def search(**kwargs):
            self.assertEqual(kwargs["settings"]["target_count"], 1)
            if kwargs["settings"]["categories"] == ["AI"]:
                return provider.SearchResult([], [REAL_URL], {"input_tokens": True, "output_tokens": -1,
                                                               "total_tokens": 5, "extra": SECRET})
            return provider.SearchResult([], [REAL_URL, INVENTED_URL],
                                         {"input_tokens": 3.5, "output_tokens": 9, "total_tokens": 10})

        with patch.object(provider, "search", side_effect=search) as run:
            result = self.call(dict(SETTINGS, target_count=1))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.cited_urls, [REAL_URL, INVENTED_URL])
        self.assertEqual(result.usage, {"input_tokens": 0, "output_tokens": 9, "total_tokens": 15})


if __name__ == "__main__":
    unittest.main()
