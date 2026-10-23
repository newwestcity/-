from datetime import datetime, timezone
from http.client import IncompleteRead
import io
import json
from threading import Lock
import unittest
from unittest.mock import Mock, patch
from urllib import error

from x_digest import discovery
from x_digest.provider import ProviderError


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
REAL = "https://xueqiu.com/123456/987654"
FAKE = "https://xueqiu.com/123456/777777"
SECRET = "test-secret-only-never-live"


def response(urls=None, annotations=None, text=FAKE):
    return {"status": "completed", "output": [
        {"type": "web_search_call", "status": "completed", "action": {
            "type": "search", "sources": [{"url": value} for value in (urls if urls is not None else [REAL])]}},
        {"type": "message", "content": [{"type": "output_text", "text": text,
                                           "annotations": annotations or []}]},
    ]}


class DiscoveryTests(unittest.TestCase):
    def call(self, values=None, **overrides):
        values = list(values if values is not None else [response(), response()])
        lock = Lock()
        opener = Mock()
        def open_response(req, timeout):
            with lock:
                value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        opener.open.side_effect = open_response
        kwargs = {"base_url": "https://relay.example/v1", "api_key": SECRET,
                  "model": "test-model", "now": NOW, "seen_ids": set()}
        kwargs.update(overrides)
        with patch.object(discovery.request, "build_opener", return_value=opener):
            result = discovery.discover_xueqiu(**kwargs)
        return result, opener

    def test_only_tool_metadata_urls_and_annotations_are_accepted(self):
        result, opener = self.call([
            response([REAL + "?tracking=x", "https://xueqiu.com/u/123", "https://evil.example/123/987"]),
            response([], [{"type": "url_citation", "url": "https://www.xueqiu.com/123456/654321"},
                          {"type": "text", "url": FAKE}]),
        ])
        # The mocked HTTP responses are consumed by concurrent workers; their
        # scheduling order is irrelevant to source provenance and identity.
        self.assertCountEqual(result.urls, [REAL, "https://xueqiu.com/123456/654321"])
        self.assertNotIn(FAKE, result.urls)
        self.assertEqual(result.diagnostics["search_requests"], 2)
        self.assertEqual(result.diagnostics["searches_completed"], 2)
        self.assertEqual(result.diagnostics["invalid_url"], 2)
        self.assertEqual(opener.open.call_count, 2)

    def test_titles_only_come_from_source_metadata_and_annotations(self):
        first = response([REAL], text='标题：模型编的投资介绍 ' + FAKE)
        first["output"][0]["action"]["sources"][0]["title"] = "  &lt;b&gt;财报分析&lt;/b&gt;\n现金流\x00 "
        second = response([], [{"type": "url_citation", "url": "https://www.xueqiu.com/123456/654321",
                                "title": "价值投资" * 100}])
        result, _ = self.call([first, second])
        by_url = {value["url"]: value for value in result.candidates}
        self.assertEqual(by_url[REAL]["title"], "财报分析 现金流")
        self.assertEqual(len(by_url["https://xueqiu.com/123456/654321"]["title"]), 300)
        for value in result.candidates:
            self.assertEqual(value["source"], "雪球")
            self.assertEqual(value["discovery_basis"], "search_metadata")
            self.assertEqual(value["discovered_at"], NOW.isoformat())
            self.assertNotIn("published_at", value)
            self.assertNotIn("author", value)
            self.assertNotIn("summary", value)
            self.assertNotIn("scores", value)
        self.assertEqual(by_url[REAL]["id"], "xueqiu:987654")

    def test_empty_or_generated_title_never_creates_a_metadata_candidate(self):
        result, _ = self.call([response([REAL], text="财报深度分析"),
                              response([], [{"type": "text", "url": REAL, "title": "假的元数据标题"}])])
        self.assertEqual(result.urls, [REAL])
        self.assertEqual(result.candidates, [])
        # Existing callers that construct the result with two arguments work.
        self.assertEqual(discovery.XueqiuDiscoveryResult([], {}).candidates, [])

    def test_duplicate_citation_can_supply_title_only_for_same_canonical_identity(self):
        data = response([REAL], annotations=[{"type": "url_citation", "url": REAL + "?x=1",
                                             "title": "长期投资与安全边际"}])
        result, _ = self.call([data, response([REAL])])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0]["title"], "长期投资与安全边际")
        self.assertEqual(result.candidates[0]["url"], REAL)

    def test_uncompleted_tools_cannot_supply_title_fallback(self):
        data = response([REAL], annotations=[{"type": "url_citation", "url": REAL, "title": "投资原理"}])
        data["output"][0]["status"] = "in_progress"
        result, _ = self.call([data, data])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.urls, [])

    def test_unfinished_or_missing_tool_does_not_accept_annotations(self):
        for alteration in ("missing_tool", "unfinished_tool", "unfinished_response"):
            data = response([], [{"type": "url_citation", "url": REAL}])
            if alteration == "missing_tool":
                data["output"].pop(0)
            elif alteration == "unfinished_tool":
                data["output"][0]["status"] = "in_progress"
            else:
                data["status"] = "incomplete"
            result, _ = self.call([data, data])
            with self.subTest(alteration=alteration):
                self.assertEqual(result.urls, [])
                self.assertEqual(result.diagnostics["searches_completed"], 0)

    def test_generated_text_urls_are_never_used_when_no_metadata(self):
        result, _ = self.call([response([], text=REAL), response([], text=REAL)])
        self.assertEqual(result.urls, [])
        self.assertEqual(result.diagnostics["no_sources"], 2)

    def test_seen_and_duplicate_identity_are_filtered_and_limit_is_twelve(self):
        urls = [f"https://xueqiu.com/123/{index}" for index in range(1, 30)]
        result, _ = self.call([response(urls), response(urls)], seen_ids={"xueqiu:1", "x:123", "github:Owner/repo"})
        self.assertEqual(len(result.urls), 12)
        self.assertNotIn("https://xueqiu.com/123/1", result.urls)
        self.assertEqual(result.diagnostics["seen_url"], 1)
        self.assertEqual(result.diagnostics["duplicate_url"], 29)

    def test_exact_requests_are_bounded_dated_and_retrieval_only(self):
        _, opener = self.call()
        payloads = [json.loads(call.args[0].data) for call in opener.open.call_args_list]
        for call, payload in zip(opener.open.call_args_list, payloads):
            self.assertEqual(call.kwargs["timeout"], 180)
            self.assertEqual(call.args[0].full_url, "https://relay.example/v1/responses")
            self.assertEqual(payload["tools"][0]["filters"]["allowed_domains"], ["xueqiu.com"])
            self.assertNotIn("max_tool_calls", payload)
            self.assertEqual(payload["max_output_tokens"], 1500)
            self.assertEqual(payload["tool_choice"], "required")
            self.assertEqual(payload["include"], ["web_search_call.action.sources"])
            self.assertNotIn(SECRET, payload["input"])
            self.assertIn("before:2026-10-22", payload["input"])
        self.assertTrue(any("after:2026-09-21" in value["input"] for value in payloads))
        self.assertTrue(any("经典" in value["input"] for value in payloads))

    def test_http_bodies_and_connection_messages_never_escape(self):
        failure = error.HTTPError("https://relay.example", 401, SECRET, {}, io.BytesIO(SECRET.encode()))
        result, opener = self.call([failure, error.URLError(SECRET)])
        self.assertEqual(result.urls, [])
        self.assertEqual(result.diagnostics["http_401"], 1)
        self.assertEqual(result.diagnostics["connection_failed"], 1)
        self.assertNotIn(SECRET, repr(result))
        self.assertEqual(opener.open.call_count, 2)

    def test_invalid_json_oversize_and_read_failures_are_fixed_diagnostics(self):
        for value, code in ((b"<html>" + SECRET.encode(), "invalid_json"),
                            (b"x" * (discovery.MAX_RESPONSE_BYTES + 1), "response_too_large"),
                            (IncompleteRead(SECRET.encode()), "connection_failed"),
                            (ProviderError(SECRET), "redirect_rejected")):
            with self.subTest(code=code):
                result, _ = self.call([value, value])
                self.assertEqual(result.urls, [])
                self.assertEqual(result.diagnostics[code], 2)
                self.assertNotIn(SECRET, repr(result))

    def test_invalid_config_and_naive_time_stop_before_network(self):
        for override in ({"api_key": ""}, {"base_url": "http://relay.example/v1"},
                         {"now": NOW.replace(tzinfo=None)}, {"seen_ids": {"invalid"}}):
            with self.subTest(override=override), patch.object(discovery.request, "build_opener") as build:
                with self.assertRaises(ProviderError):
                    discovery.discover_xueqiu(base_url=override.get("base_url", "https://relay.example/v1"),
                        api_key=override.get("api_key", SECRET), model="test-model",
                        now=override.get("now", NOW), seen_ids=override.get("seen_ids", set()))
                build.assert_not_called()


class DiscoverySourceRoutingTests(unittest.TestCase):
    def call(self, **overrides):
        kwargs = {"base_url": "https://relay.example/v1", "api_key": SECRET,
                  "model": "test-model", "now": NOW, "seen_ids": set()}
        kwargs.update(overrides)
        return discovery.discover_xueqiu_sources(**kwargs)

    def candidates(self, count, *, label):
        return [{"id": f"xueqiu:{number}", "url": f"https://xueqiu.com/123/{number}",
                 "source": "雪球", "title": f"{label}：价值投资{number}",
                 "discovery_basis": "search_metadata", "discovered_at": NOW.isoformat()}
                for number in range(1, count + 1)]




if __name__ == "__main__":
    unittest.main()
