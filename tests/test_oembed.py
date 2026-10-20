from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
import inspect
import io
import json
import ssl
from threading import Barrier, Lock
import unittest
from unittest.mock import Mock, patch
from urllib import error, request
from urllib.parse import parse_qs, urlsplit

from x_digest import oembed


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
POST_ID = "463440424141459456"
URL = f"https://x.com/Interior/status/{POST_ID}"


def payload(post_id=POST_ID, *, author="Interior", text="Real &amp; source text"):
    return {
        "type": "rich", "url": f"https://twitter.com/old_handle/status/{post_id}",
        "author_name": "Display name", "author_url": f"https://twitter.com/{author}",
        "html": (f'<blockquote class="twitter-tweet"><p>{text}</p>'
                 f'— Display name (@{author}) <a href="https://x.com/{author}/status/{post_id}?ref_src=embed">'
                 'May 5, 2014</a></blockquote>'),
    }


def response(data=None, *, raw=None, headers=None):
    result = io.BytesIO(json.dumps(payload() if data is None else data).encode() if raw is None else raw)
    result.headers = headers or {}
    return result


class OEmbedTests(unittest.TestCase):
    def call(self, urls=None, *, data=None, raw=None, opener=None, now=NOW, limit=20):
        if opener is None:
            opener = Mock()
            opener.open.side_effect = lambda *args, **kwargs: response(data, raw=raw)
        with patch.object(oembed.request, "build_opener", return_value=opener):
            result = oembed.fetch_verified_posts([URL] if urls is None else urls, now=now, limit=limit)
        return result, opener

    def test_matching_official_markup_returns_source_text_and_utc_dates(self):
        local_now = NOW.astimezone(timezone(timedelta(hours=8)))
        posts, opener = self.call(now=local_now)
        self.assertEqual(posts, [{
            "id": POST_ID, "url": URL, "author": "Interior", "text": "Real & source text",
            "published_at": "2014-05-05T22:09:42.079000+00:00", "verified_at": NOW.isoformat(),
        }])
        self.assertEqual(opener.open.call_count, 1)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(urlsplit(req.full_url).netloc, "publish.x.com")
        self.assertEqual(parse_qs(urlsplit(req.full_url).query), {
            "url": [URL], "omit_script": ["true"], "hide_thread": ["true"],
        })
        self.assertIsNone(req.data)
        self.assertIsNone(req.get_header("Authorization"))
        self.assertGreater(opener.open.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 10)

    def test_both_response_url_and_html_permalink_must_match(self):
        for change in ("top", "html", "body_link_only"):
            data = payload()
            if change == "top":
                data["url"] = "https://x.com/Interior/status/20"
            elif change == "html":
                data["html"] = data["html"].replace(POST_ID, "20")
            else:
                data["html"] = (f'<blockquote class="twitter-tweet"><p><a href="{URL}">'
                                'linked from different post</a></p><a href="https://x.com/jack/status/20">date</a>'
                                '</blockquote>')
            with self.subTest(change=change):
                self.assertEqual(self.call(data=data)[0], [])

    def test_non_rich_and_missing_required_metadata_are_rejected(self):
        for data in (dict(payload(), type="link"), dict(payload(), url=None),
                     dict(payload(), author_url=None), dict(payload(), html=None)):
            with self.subTest(data=data):
                self.assertEqual(self.call(data=data)[0], [])

    def test_missing_empty_or_ambiguous_body_is_rejected(self):
        for html in (
            f'<blockquote class="twitter-tweet"><a href="{URL}">date</a></blockquote>',
            f'<blockquote class="twitter-tweet"><p> </p><a href="{URL}">date</a></blockquote>',
            f'<blockquote class="twitter-tweet"><p>text</p><a href="{URL}">date</a>',
            payload()["html"] * 2,
            f'<blockquote class="twitter-tweet"><p>text</p><blockquote><p>other</p></blockquote><a href="{URL}">date</a></blockquote>',
        ):
            with self.subTest(html=html):
                self.assertEqual(self.call(data=dict(payload(), html=html))[0], [])

    def test_scripts_styles_and_content_outside_target_are_never_collected(self):
        html = (f'<p>outside</p><script><blockquote class="twitter-tweet"><p>script source</p>'
                f'<a href="{URL}">fake</a></blockquote></script>'
                '<blockquote class="twitter-tweet"><p>actual <script>secret()</script>'
                '<style>hidden</style><a href="https://evil.example/">source</a><br>text</p>'
                f'<a href="{URL}">date</a></blockquote>')
        posts, _ = self.call(data=dict(payload(), html=html))
        self.assertEqual(posts[0]["text"], "actual source text")
        for fake in ("https://x.com.evil.example/Interior/status/" + POST_ID,
                     "javascript:alert(1)", "https://x.com@evil.example/Interior/status/" + POST_ID):
            data = payload()
            data["html"] = f'<blockquote class="twitter-tweet"><p>text</p><a href="{fake}">date</a></blockquote>'
            with self.subTest(fake=fake):
                self.assertEqual(self.call(data=data)[0], [])

    def test_current_author_url_controls_renamed_account_and_canonical_url(self):
        posts, _ = self.call(data=payload(author="New_Name"))
        self.assertEqual(posts[0]["author"], "New_Name")
        self.assertEqual(posts[0]["url"], f"https://x.com/New_Name/status/{POST_ID}")
        for author_url in ("http://x.com/user", "https://evil.example/user",
                           "https://x.com@evil.example/user", "https://x.com/user/extra",
                           "https://x.com/i", "https://x.com/user?api_key=secret",
                           "https://x.com/", "https://x.com/too_long_username"):
            with self.subTest(author_url=author_url):
                self.assertEqual(self.call(data=dict(payload(), author_url=author_url))[0], [])

    def test_invalid_input_links_never_open_network(self):
        urls = [None, "http://x.com/user/status/20", "https://evil.example/user/status/20",
                "https://x.com@evil.example/user/status/20", "https://x.com/search?q=test",
                "https://x.com/user/status/20\n"]
        with patch.object(oembed.request, "build_opener") as build:
            self.assertEqual(oembed.fetch_verified_posts(urls, now=NOW), [])
        build.assert_not_called()

    def test_routing_media_links_and_duplicates_share_one_fetch(self):
        for alternate in (f"https://x.com/i/web/status/{POST_ID}",
                          f"https://x.com/i/status/{POST_ID}",
                          f"https://twitter.com/old/status/{POST_ID}/photo/1"):
            with self.subTest(alternate=alternate):
                posts, opener = self.call([alternate, URL, URL + "?s=20"])
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0]["id"], POST_ID)
                self.assertEqual(opener.open.call_count, 1)

    def test_single_official_https_redirect_is_allowed_without_header_forwarding(self):
        opener = Mock()
        opener.open.side_effect = [
            error.HTTPError("https://publish.x.com/oembed", 302, "moved", {
                "Location": "https://publish.twitter.com/oembed?url=changed&api_key=untrusted",
            }, io.BytesIO()), response(),
        ]
        posts, _ = self.call(opener=opener)
        self.assertEqual(len(posts), 1)
        self.assertEqual(opener.open.call_count, 2)
        redirected = opener.open.call_args_list[1].args[0]
        self.assertEqual(urlsplit(redirected.full_url).netloc, "publish.twitter.com")
        self.assertEqual(parse_qs(urlsplit(redirected.full_url).query), {
            "url": [URL], "omit_script": ["true"], "hide_thread": ["true"],
        })
        self.assertIsNone(redirected.get_header("Authorization"))
        req = request.Request("https://publish.x.com/oembed", headers={"Authorization": "secret"})
        self.assertIsNone(oembed._NoRedirects().redirect_request(req, None, 302, "", {},
                                                               "https://publish.twitter.com/oembed"))

    def test_other_redirects_and_second_redirect_are_rejected(self):
        locations = ["http://publish.twitter.com/oembed", "https://evil.example/oembed",
                     "https://publish.twitter.com/oembed.json", "https://publish.twitter.com/other",
                     "https://user:secret@publish.twitter.com/oembed",
                     "https://publish.twitter.com:443/oembed", "https://publish.x.com/oembed",
                     "https://publish.twitter.com/oembed#fragment"]
        for location in locations:
            opener = Mock()
            opener.open.side_effect = error.HTTPError("https://publish.x.com/oembed", 302, "moved",
                                                     {"Location": location}, io.BytesIO())
            with self.subTest(location=location):
                self.assertEqual(self.call(opener=opener)[0], [])
                self.assertEqual(opener.open.call_count, 1)
        opener = Mock()
        opener.open.side_effect = error.HTTPError("https://publish.x.com/oembed", 302, "moved", None,
                                                 io.BytesIO())
        self.assertEqual(self.call(opener=opener)[0], [])
        self.assertEqual(opener.open.call_count, 1)
        opener = Mock()
        opener.open.side_effect = [
            error.HTTPError("https://publish.x.com/oembed", 302, "", {
                "Location": "https://publish.twitter.com/oembed"}, io.BytesIO()),
            error.HTTPError("https://publish.twitter.com/oembed", 302, "", {
                "Location": "https://publish.x.com/oembed"}, io.BytesIO()),
        ]
        self.assertEqual(self.call(opener=opener)[0], [])
        self.assertEqual(opener.open.call_count, 2)

    def test_size_cap_invalid_json_and_network_failures_are_skipped(self):
        for raw in (b"x" * (oembed.MAX_RESPONSE_BYTES + 1), b"not JSON", b"\xff", b"null"):
            with self.subTest(size=len(raw)):
                self.assertEqual(self.call(raw=raw)[0], [])
        body = response(headers={"Content-Length": str(oembed.MAX_RESPONSE_BYTES + 1)})
        opener = Mock()
        opener.open.return_value = body
        with patch.object(body, "read", wraps=body.read) as read:
            self.assertEqual(self.call(opener=opener)[0], [])
            read.assert_not_called()
        for exc in (error.URLError("secret"), TimeoutError("secret"),
                    IncompleteRead(b"secret", 999),
                    error.HTTPError(URL, 404, "secret", {}, io.BytesIO())):
            opener = Mock()
            opener.open.side_effect = exc
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self.call(opener=opener)[0], [])
                self.assertEqual(opener.open.call_count, 1)

    def test_streamed_body_read_stops_when_total_time_budget_expires(self):
        body = Mock()
        body.read1.return_value = b"x"
        body.fp = None
        with patch.object(oembed.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaises(TimeoutError):
                oembed._read_body(body, 10)
        body.read1.assert_called_once()

    def test_future_posts_rejected_but_older_posts_accepted_and_text_bounded(self):
        self.assertEqual(len(self.call(data=payload(text="a" * 5000))[0][0]["text"]), 4000)
        future_id = str(int(((NOW + timedelta(minutes=6)) - oembed._EPOCH).total_seconds() * 1000) << 22)
        with patch.object(oembed.request, "build_opener") as build:
            self.assertEqual(oembed.fetch_verified_posts([f"https://x.com/a/status/{future_id}"], now=NOW), [])
        build.assert_not_called()

    def test_configuration_requires_aware_time_and_has_no_credential_parameters(self):
        self.assertEqual(set(inspect.signature(oembed.fetch_verified_posts).parameters), {"urls", "now", "limit"})
        for kwargs in ({"now": NOW.replace(tzinfo=None)}, {"now": None},
                       {"now": NOW, "limit": 21}, {"now": NOW, "limit": True}):
            with self.subTest(kwargs=kwargs), patch.object(oembed.request, "build_opener") as build:
                with self.assertRaises(ValueError):
                    oembed.fetch_verified_posts([URL], **kwargs)
                build.assert_not_called()
        with self.assertRaises(TypeError):
            oembed.fetch_verified_posts([URL], now=NOW, api_key="secret")
        with patch.object(oembed.request, "build_opener", return_value=Mock(open=Mock(return_value=response()))) as build:
            oembed.fetch_verified_posts([URL], now=NOW)
        handlers = build.call_args.args
        https = next(handler for handler in handlers if isinstance(handler, request.HTTPSHandler))
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)

    def test_two_workers_preserve_input_order_and_cap_unique_candidates_at_twenty(self):
        lock = Lock()
        pair = Barrier(2, timeout=5)
        active = peak = 0
        ids = [str(int(POST_ID) + value) for value in range(24)]

        def opened(req, *, timeout):
            nonlocal active, peak
            post_id = parse_qs(urlsplit(req.full_url).query)["url"][0].rsplit("/", 1)[1]
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                pair.wait()
                return response(payload(post_id))
            finally:
                with lock:
                    active -= 1

        opener = Mock()
        opener.open.side_effect = opened
        posts, _ = self.call([f"https://x.com/a/status/{value}" for value in ids], opener=opener)
        self.assertEqual(peak, 2)
        self.assertEqual(opener.open.call_count, 20)
        self.assertEqual([post["id"] for post in posts], ids[:20])


if __name__ == "__main__":
    unittest.main()
