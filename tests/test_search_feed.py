from datetime import datetime, timezone
from http.client import IncompleteRead
import io
from threading import Lock
import unittest
from unittest.mock import Mock, patch
from urllib import error
from urllib.parse import parse_qs, urlsplit

from x_digest import search_feed


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
URL = "https://xueqiu.com/123/456"


def rss(items=None):
    body = items if items is not None else f"<item><title>价值投资与安全边际</title><link>{URL}</link></item>"
    return ("<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel>" + body + "</channel></rss>").encode()


class FeedTests(unittest.TestCase):
    def call(self, values=None, **overrides):
        values = list(values if values is not None else [rss(), rss()])
        opener, lock = Mock(), Lock()
        def open_response(req, timeout):
            with lock:
                value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(value)
        opener.open.side_effect = open_response
        kwargs = {"now": NOW, "seen_ids": set()}
        kwargs.update(overrides)
        with patch.object(search_feed.request, "build_opener", return_value=opener):
            result = search_feed.discover_xueqiu_feed(**kwargs)
        return result, opener

    def test_only_original_links_and_actual_feed_titles_are_returned(self):
        feed = rss(f"<item><title>&lt;b&gt;财报分析&lt;/b&gt;\n 与估值</title><link>{URL}?track=1</link>"
                   "<description>这里的摘要和作者、发布时间不能当作已读原文依据</description><pubDate>future</pubDate></item>"
                   "<item><title>bad</title><link>https://evil.example/123/456</link></item>"
                   "<item><title>主页</title><link>https://xueqiu.com/u/123</link></item>")
        result, opener = self.call([feed, rss("")])
        self.assertEqual(result.urls, [URL])
        item = result.candidates[0]
        self.assertEqual(item, {"id": "xueqiu:456", "url": URL, "source": "雪球", "title": "财报分析 与估值",
                               "discovery_basis": "search_metadata", "search_provider": "Bing RSS",
                               "discovered_at": NOW.isoformat()})
        self.assertEqual(result.diagnostics["invalid_url"], 2)
        self.assertEqual(opener.open.call_count, 2)

    def test_requests_only_fixed_public_https_host_and_never_send_credentials(self):
        _, opener = self.call()
        queries = set()
        for call in opener.open.call_args_list:
            req = call.args[0]
            parsed = urlsplit(req.full_url)
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "www.bing.com", "/search"))
            query = parse_qs(parsed.query)
            self.assertEqual(query["format"], ["rss"])
            self.assertEqual(query["mkt"], ["zh-CN"])
            self.assertEqual(query["setlang"], ["zh-hans"])
            self.assertIn("site:xueqiu.com", query["q"][0])
            queries.add(query["q"][0])
            self.assertEqual(req.get_method(), "GET")
            self.assertIsNone(req.data)
            self.assertFalse(any(key.lower() in {"authorization", "cookie"} for key in req.headers))
            self.assertEqual(call.kwargs["timeout"], 15)
        self.assertEqual(queries, {"site:xueqiu.com 价值投资", "site:xueqiu.com 财报分析"})
        handler = search_feed._NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_http_official_identity_is_upgraded_and_invalid_links_have_shape_diagnostics(self):
        feed = rss("<item><title>企业经营分析</title><link>http://www.xueqiu.com/123/456</link></item>"
                   "<item><title>bad</title><link>ftp://xueqiu.com/123/456</link></item>"
                   "<item><title>bad</title><link>http://xueqiu.com.evil.example/123/456</link></item>"
                   "<item><title>bad</title><link>https://www.bing.com/ck/a</link></item>"
                   "<item><title>主页</title><link>http://xueqiu.com/u/123</link></item>")
        result, opener = self.call([feed, rss("")])
        self.assertEqual(result.urls, [URL])
        self.assertEqual(result.diagnostics["http_knownhost"], 2)
        self.assertEqual(result.diagnostics["invalid_url"], 4)
        self.assertEqual(result.diagnostics["invalid_url_scheme"], 1)
        self.assertEqual(result.diagnostics["invalid_url_host"], 2)
        self.assertEqual(result.diagnostics["invalid_article_path"], 1)
        self.assertEqual(search_feed._rejection_reason("https://xueqiu.com/u/123"), "invalid_article_path")
        self.assertEqual(search_feed._rejection_reason("http://xueqiu.com/123/456"), "")
        for call in opener.open.call_args_list:
            self.assertEqual(urlsplit(call.args[0].full_url).netloc, "www.bing.com")

    def test_seen_duplicates_empty_titles_and_limit_are_enforced(self):
        items = "<item><title> </title><link>https://xueqiu.com/123/999</link></item>" + "".join(
            f"<item><title>{'财报分析' * 100}</title><link>https://xueqiu.com/123/{index}</link></item>"
            for index in range(1, 30))
        result, _ = self.call([rss(items), rss(items)], seen_ids={"xueqiu:1", "x:123"})
        self.assertEqual(len(result.candidates), 12)
        self.assertEqual(len(result.candidates[0]["title"]), 240)
        self.assertNotIn("https://xueqiu.com/123/1", result.urls)
        self.assertEqual(result.diagnostics["seen_url"], 1)
        self.assertEqual(result.diagnostics["duplicate_url"], 29)

    def test_entity_doctype_utf16_html_and_invalid_xml_are_not_feed_evidence(self):
        cases = [b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>',
                 b'<!ENTITY x "boom"><rss><channel/></rss>',
                 '<rss><channel/></rss>'.encode('utf-16'),
                 '<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>'.encode('utf-16le'),
                 b'<html><body>captcha</body></html>', b'bad xml']
        for body in cases:
            with self.subTest(body=body[:30]):
                result, _ = self.call([body, body])
                self.assertEqual(result.urls, [])
                self.assertEqual(result.diagnostics["feeds_completed"], 0)

    def test_bounded_failures_have_no_response_body_or_message_leak_and_no_retry(self):
        secret = "must-not-print-remote-secret"
        cases = [
            (error.HTTPError("https://www.bing.com", 403, secret, {}, io.BytesIO(secret.encode())), "http_403"),
            (error.HTTPError("https://www.bing.com", 302, secret, {}, io.BytesIO(secret.encode())), "redirect_rejected"),
            (error.URLError(secret), "connection_failed"),
            (IncompleteRead(secret.encode()), "connection_failed"),
            (b'x' * (search_feed.MAX_RESPONSE_BYTES + 1), "response_too_large"),
        ]
        for value, code in cases:
            with self.subTest(code=code):
                result, opener = self.call([value, value])
                self.assertEqual(result.urls, [])
                self.assertEqual(result.diagnostics[code], 2)
                self.assertNotIn(secret, repr(result))
                self.assertEqual(opener.open.call_count, 2)

    def test_local_invalid_input_stops_before_requests(self):
        for kwargs in ({"now": NOW.replace(tzinfo=None), "seen_ids": set()},
                       {"now": NOW, "seen_ids": {"bad id"}}):
            with self.subTest(kwargs=kwargs), patch.object(search_feed.request, "build_opener") as build:
                with self.assertRaises(ValueError):
                    search_feed.discover_xueqiu_feed(**kwargs)
                build.assert_not_called()

    def test_body_deadline_is_enforced(self):
        with patch.object(search_feed.time, "monotonic", return_value=100):
            with self.assertRaises(TimeoutError):
                search_feed._read(io.BytesIO(rss()), 99)


if __name__ == "__main__":
    unittest.main()
