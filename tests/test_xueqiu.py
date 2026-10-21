"""Synthetic parser/transport fixtures; these do not claim live-site access."""

from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
import io
import json
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

from x_digest import xueqiu


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
AUTHOR_ID = "1234567890"
ARTICLE_ID = "2468101214"
URL = f"https://xueqiu.com/{AUTHOR_ID}/{ARTICLE_ID}"
BODY = "从企业经营和自由现金流出发，理解估值假设、竞争优势与长期风险。" * 30


def status(**changes):
    data = {"id": int(ARTICLE_ID), "user_id": int(AUTHOR_ID),
            "user": {"id": int(AUTHOR_ID), "screen_name": "测试作者"},
            "title": "如何理解企业的长期价值", "text": f"<p>{BODY}</p>",
            "created_at": int((NOW - timedelta(days=3)).timestamp() * 1000)}
    data.update(changes)
    return data


def response(data=None, *, raw=None, headers=None):
    body = io.BytesIO(json.dumps(status() if data is None else data).encode() if raw is None else raw)
    body.headers = headers or {}
    return body


def page(*, body=BODY, canonical=URL, extra="", body_class="article__bd__detail"):
    return (f'<html><head><link rel="canonical" href="{canonical}">'
            '<meta name="author" content="测试作者"></head><body>'
            '<nav>outside navigation</nav><h1>估值的原则</h1>'
            f'<div class="{body_class}"><p>{body}</p></div>{extra}</body></html>').encode()


class XueqiuTests(unittest.TestCase):
    def call(self, urls=None, *, data=None, raw=None, opener=None, now=NOW, limit=12):
        if opener is None:
            opener = Mock()
            opener.open.side_effect = lambda *args, **kwargs: response(data, raw=raw)
        with patch.object(xueqiu.request, "build_opener", return_value=opener):
            result = xueqiu.fetch_articles([URL] if urls is None else urls, now=now, limit=limit)
        return result, opener

    def test_official_https_urls_are_canonicalized_and_tracking_is_dropped(self):
        self.assertEqual(xueqiu.canonical_article_url(URL + "?utm_source=test#comments"),
                         (URL, AUTHOR_ID, ARTICLE_ID))
        self.assertEqual(xueqiu.canonical_article_url(URL.replace("xueqiu.com", "www.xueqiu.com") + "/"),
                         (URL, AUTHOR_ID, ARTICLE_ID))

    def test_invalid_links_never_open_network(self):
        urls = [None, "http://xueqiu.com/1/2", "https://xueqiu.com.evil.example/1/2",
                "https://xueqiu.com@evil.example/1/2", "https://u:p@xueqiu.com/1/2",
                "https://xueqiu.com:443/1/2", "https://xueqiu.com/1", "https://xueqiu.com/01/2",
                "https://xueqiu.com/1/2/extra", "https://xueqiu.com/1/2\n", "https://[bad/1/2"]
        with patch.object(xueqiu.request, "build_opener") as build:
            result = xueqiu.fetch_articles(urls, now=NOW)
        self.assertEqual(result.articles, [])
        self.assertEqual(result.diagnostics["invalid_url"], len(urls))
        build.assert_not_called()

    def test_status_record_requires_matching_article_and_user_ids(self):
        result, opener = self.call(now=NOW.astimezone(timezone(timedelta(hours=8))))
        self.assertEqual(result.diagnostics["verified"], 1)
        article = result.articles[0]
        self.assertEqual(article["id"], "xueqiu:" + ARTICLE_ID)
        self.assertEqual(article["url"], URL)
        self.assertEqual(article["source"], "雪球")
        self.assertEqual(article["author"], "测试作者")
        self.assertEqual(article["title"], "如何理解企业的长期价值")
        self.assertEqual(article["text"], BODY)
        self.assertEqual(article["content_basis"], "original_text")
        self.assertEqual(article["published_at"], (NOW - timedelta(days=3)).isoformat())
        self.assertEqual(article["verified_at"], NOW.isoformat())
        self.assertFalse(article["text_truncated"])
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, URL)
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertIsNone(req.get_header("Authorization"))
        self.assertIsNone(req.get_header("Cookie"))
        self.assertGreater(opener.open.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], xueqiu.REQUEST_TIMEOUT)

    def test_mismatched_or_missing_identity_is_not_accepted(self):
        samples = [status(id=999), status(user_id=999), status(user={"id": 999}),
                   status(user=None, user_id=None)]
        for data in samples:
            with self.subTest(data=data):
                self.assertEqual(self.call(data=data)[0].articles, [])

    def test_matching_status_is_found_inside_public_json_envelope(self):
        result, _ = self.call(data={"statuses": [{"id": 999, "text": BODY}, status()]})
        self.assertEqual(result.articles[0]["text"], BODY)
        result, _ = self.call(data={"list": [status(user_id=None)]})
        self.assertEqual(result.articles[0]["author_id"], AUTHOR_ID)

    def test_json_script_and_plain_json_bootstrap_are_parsed_without_evaluation(self):
        for script in ('<script type="application/json">' + json.dumps({"status": status()}) + '</script>',
                       '<script>window.SNOWMAN_STATUS = ' + json.dumps(status()) + '; alert("ignored");</script>'):
            with self.subTest(script=script[:60]):
                result, _ = self.call(raw=('<html><body>' + script + '</body></html>').encode())
                self.assertEqual(result.articles[0]["text"], BODY)
        # JavaScript object literals and functions are deliberately not evaluated.
        result, _ = self.call(raw=b'<script>SNOWMAN_STATUS = function(){return "untrusted"}();</script>')
        self.assertEqual(result.articles, [])

    def test_json_ld_must_bind_article_body_to_exact_official_article_url(self):
        data = {"@context": "https://schema.org", "@type": "Article", "url": URL,
                "headline": "长期投资", "articleBody": BODY,
                "author": {"name": "作者", "url": f"https://xueqiu.com/{AUTHOR_ID}"},
                "datePublished": "2026-10-18T08:00:00+08:00"}

        def html(value):
            return ('<script type="application/ld+json">' + json.dumps(value) + '</script>').encode()

        result, _ = self.call(raw=html(data))
        self.assertEqual(result.articles[0]["published_at"], "2026-10-18T00:00:00+00:00")
        for changes in ({"url": None}, {"url": "https://evil.example/1/2"},
                        {"url": "https://xueqiu.com/1/2"}, {"articleBody": None},
                        {"author": {"name": "wrong", "url": "https://xueqiu.com/999"}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.call(raw=html(dict(data, **changes)))[0].articles, [])

    def test_css_body_requires_matching_canonical_and_exact_known_body_container(self):
        result, _ = self.call(raw=page())
        self.assertEqual(result.articles[0]["text"], BODY)
        self.assertEqual(result.articles[0]["published_at"], "")
        self.assertEqual(result.diagnostics["unknown_date"], 1)
        for raw in (page(canonical=""), page(canonical="https://xueqiu.com/1/2"),
                    page(body_class="content"), page(extra=f'<div class="article__bd__detail">{BODY}</div>'),
                    page().replace(b'</div>', b'', 1)):
            with self.subTest(raw=raw[:80]):
                self.assertEqual(self.call(raw=raw)[0].articles, [])

    def test_navigation_script_and_hidden_markup_are_never_used_as_body(self):
        html_body = (BODY + '<br/><script>hidden()</script><style>secret style</style>'
                     '<p>&lt;literal&gt; &amp; cash flow</p>')
        result, _ = self.call(raw=page(body=html_body, extra='<p>outside footer</p>'))
        text = result.articles[0]["text"]
        self.assertIn("<literal> & cash flow", text)
        for excluded in ("outside navigation", "outside footer", "hidden()", "secret style"):
            self.assertNotIn(excluded, text)
        result, _ = self.call(raw=page(body="登录后阅读", extra=BODY))
        self.assertEqual(result.articles, [])
        self.assertEqual(result.diagnostics["body_too_short"], 1)

    def test_conflicting_page_identity_overrides_otherwise_valid_embedded_record(self):
        script = '<script type="application/json">' + json.dumps(status()) + '</script>'
        result, _ = self.call(raw=page(canonical="https://xueqiu.com/1/2", extra=script))
        self.assertEqual(result.articles, [])
        self.assertEqual(result.diagnostics["identity_mismatch"], 1)

    def test_challenge_login_navigation_and_invalid_responses_are_not_articles(self):
        for raw in (b'<html>captcha login risk control</html>', b'null', b'not JSON', b'\xff',
                    ('<html><main>' + BODY + '</main></html>').encode()):
            with self.subTest(raw=raw[:50]):
                result, _ = self.call(raw=raw)
                self.assertEqual(result.articles, [])
                self.assertEqual(result.diagnostics["unverified_page"], 1)

    def test_ambiguous_publication_is_unknown_and_future_date_is_rejected(self):
        for value in (None, "", True, 1234, "yesterday", "2026-10-18", "2026-10-18T08:00:00"):
            with self.subTest(date=value):
                article = self.call(data=status(created_at=value))[0].articles[0]
                self.assertEqual(article["published_at"], "")
        result, _ = self.call(data=status(created_at=int((NOW + timedelta(days=1)).timestamp() * 1000)))
        self.assertEqual(result.articles, [])
        self.assertEqual(result.diagnostics["future_date"], 1)

    def test_long_text_is_explicitly_truncated_not_passed_as_whole_article(self):
        result, _ = self.call(data=status(text="长" * (xueqiu.MAX_TEXT_CHARACTERS + 1)))
        article = result.articles[0]
        self.assertTrue(article["text_truncated"])
        self.assertEqual(len(article["text"]), xueqiu.MAX_TEXT_CHARACTERS)
        self.assertEqual(article["text_characters"], xueqiu.MAX_TEXT_CHARACTERS + 1)

    def test_duplicates_are_only_requested_once_and_limit_is_bounded(self):
        result, opener = self.call([URL, URL + "?from=test", URL + "/"])
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.diagnostics["duplicate_url"], 2)
        self.assertEqual(opener.open.call_count, 1)
        urls = [URL, "https://xueqiu.com/1/2"]
        _, opener = self.call(urls, limit=1)
        self.assertEqual(opener.open.call_count, 1)

    def test_bad_publication_clock_or_limit_is_rejected_before_network(self):
        with patch.object(xueqiu.request, "build_opener") as build:
            for kwargs in ({"now": datetime(2026, 10, 21)}, {"now": NOW, "limit": 0},
                           {"now": NOW, "limit": 13}, {"now": NOW, "limit": True}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    xueqiu.fetch_articles([URL], **kwargs)
            with self.assertRaises(ValueError):
                xueqiu.fetch_articles(URL, now=NOW)
        build.assert_not_called()

    def test_public_official_redirect_to_same_article_is_bounded_and_keeps_no_credentials(self):
        opener = Mock()
        opener.open.side_effect = [error.HTTPError(URL, 302, "moved", {
            "Location": URL.replace("xueqiu.com", "www.xueqiu.com")}, io.BytesIO()), response()]
        result, _ = self.call(opener=opener)
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            req = call.args[0]
            self.assertIsNone(req.get_header("Authorization"))
            self.assertIsNone(req.get_header("Cookie"))
        req = request.Request(URL, headers={"Authorization": "not forwarded"})
        self.assertIsNone(xueqiu._NoRedirects().redirect_request(req, None, 302, "", {}, URL))

    def test_offsite_login_different_article_http_and_unbounded_redirects_are_rejected(self):
        for target in ("http://xueqiu.com/1/2", "https://evil.example/1/2", "https://xueqiu.com/login",
                       "https://xueqiu.com/1/2", "https://u:p@xueqiu.com/1/2"):
            opener = Mock()
            opener.open.side_effect = error.HTTPError(URL, 302, "", {"Location": target}, io.BytesIO())
            with self.subTest(target=target):
                result, _ = self.call(opener=opener)
                self.assertEqual(result.articles, [])
                self.assertEqual(result.diagnostics["redirect_rejected"], 1)
                self.assertEqual(opener.open.call_count, 1)
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: (_ for _ in ()).throw(
            error.HTTPError(URL, 302, "", {"Location": URL}, io.BytesIO()))
        result, _ = self.call(opener=opener)
        self.assertEqual(result.articles, [])
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(result.diagnostics["redirect_rejected"], 1)

    def test_http_and_network_errors_have_fixed_non_sensitive_diagnostics(self):
        for failure, label in ((error.HTTPError(URL, 403, "secret", {}, io.BytesIO()), "access_denied"),
                               (error.HTTPError(URL, 401, "secret", {}, io.BytesIO()), "access_denied"),
                               (error.HTTPError(URL, 429, "secret", {}, io.BytesIO()), "rate_limited"),
                               (error.HTTPError(URL, 404, "secret", {}, io.BytesIO()), "not_found"),
                               (error.HTTPError(URL, 500, "secret", {}, io.BytesIO()), "http_error"),
                               (error.URLError("secret"), "connection_failed"),
                               (TimeoutError("secret"), "connection_failed"),
                               (IncompleteRead(b"secret", 999), "connection_failed")):
            opener = Mock()
            opener.open.side_effect = failure
            with self.subTest(label=label, failure=type(failure).__name__):
                result, _ = self.call(opener=opener)
                self.assertEqual(result.articles, [])
                self.assertEqual(result.diagnostics[label], 1)
                self.assertNotIn("secret", json.dumps(result.diagnostics))
                self.assertEqual(opener.open.call_count, 1)

    def test_tls_verification_and_response_size_cap(self):
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: response()
        with patch.object(xueqiu.request, "build_opener", return_value=opener) as build:
            xueqiu.fetch_articles([URL], now=NOW)
        https = next(item for item in build.call_args.args if isinstance(item, request.HTTPSHandler))
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        result, _ = self.call(raw=b'x' * (xueqiu.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(result.diagnostics["response_too_large"], 1)
        body = response(headers={"Content-Length": str(xueqiu.MAX_RESPONSE_BYTES + 1)})
        opener.open.side_effect = None
        opener.open.return_value = body
        with patch.object(body, "read1", wraps=body.read1) as read:
            result, _ = self.call(opener=opener)
        self.assertEqual(result.diagnostics["response_too_large"], 1)
        read.assert_not_called()

    def test_stream_read_has_a_total_time_limit(self):
        body = Mock()
        body.headers = {}
        body.read1.return_value = b"x"
        body.fp = None
        with patch.object(xueqiu.time, "monotonic", side_effect=[0, 16]):
            with self.assertRaises(TimeoutError):
                xueqiu._read_body(body, 15)
        body.read1.assert_called_once()


if __name__ == "__main__":
    unittest.main()
