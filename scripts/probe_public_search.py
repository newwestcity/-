"""Six bounded, anonymous public-search probes; no model or mail calls.

This is a manual diagnostic, not a production source adapter. A link accepted
here only has an official post-shaped identity; its article remains unread.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from http.client import HTTPException
import json
from pathlib import Path
import ssl
import sys
import time
import unicodedata
from urllib import error, request
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest.selection import canonical_post_url
from x_digest.xueqiu import canonical_article_url


TIMEOUT = 25
MAX_BYTES = 1024 * 1024
OFFICIAL_X = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
OFFICIAL_SNOW = {"xueqiu.com", "www.xueqiu.com"}


@dataclass(frozen=True)
class Probe:
    engine: str
    url: str
    source: str


def probes():
    return [
        Probe("google_x", "https://www.google.com/search?" + urlencode(
            {"q": "site:x.com AI tutorial thread", "num": 10, "udm": 14, "tbs": "qdr:w"}), "X"),
        Probe("google_snowball", "https://www.google.com/search?" + urlencode(
            {"q": "site:xueqiu.com 价值投资 财报", "num": 10, "udm": 14}), "雪球"),
        Probe("bing_cn_snowball", "https://cn.bing.com/search?" + urlencode(
            {"q": "site:xueqiu.com 财报分析", "count": 10, "mkt": "zh-CN"}), "雪球"),
        Probe("duckduckgo_x", "https://html.duckduckgo.com/html/?" + urlencode(
            {"q": "site:x.com AI tutorial thread", "df": "w"}), "X"),
        Probe("baidu_snowball", "https://www.baidu.com/s?" + urlencode(
            {"wd": "site:xueqiu.com 价值投资 财报", "rn": 10}), "雪球"),
        Probe("brave_x", "https://search.brave.com/search?" + urlencode(
            {"q": "site:x.com AI tutorial thread", "source": "web", "tf": "pw"}), "X"),
    ]


def clean(value, maximum=240):
    value = " ".join(value.split())
    return "".join(char for char in value if not unicodedata.category(char).startswith("C"))[:maximum]


class SearchHTML(HTMLParser):
    """Read actual anchor attributes and visible anchor text, never scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.anchor = None
        self.anchors = []
        self.visible = []
        self.title_parts = []
        self.in_title = False
        self.challenge_form = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden += 1
        if self.hidden:
            return
        values = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "form":
            action = values.get("action", "").casefold()
            if any(term in action for term in ("/sorry/", "anomaly", "captcha", "challenge")):
                self.challenge_form = True
        if tag == "a":
            self._finish_anchor()
            if isinstance(values.get("href"), str):
                self.anchor = {"href": values["href"], "parts": []}

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "title":
            self.in_title = False
        if tag == "a":
            self._finish_anchor()

    def handle_data(self, data):
        if self.hidden:
            return
        if len(self.visible) < 10000:
            self.visible.append(data)
        if self.in_title:
            self.title_parts.append(data)
        if self.anchor is not None:
            self.anchor["parts"].append(data)

    def _finish_anchor(self):
        if self.anchor is not None:
            self.anchors.append((self.anchor["href"], clean(" ".join(self.anchor["parts"]))))
            self.anchor = None

    def finish(self):
        self._finish_anchor()

    def challenged(self):
        title = clean(" ".join(self.title_parts)).casefold()
        text = clean(" ".join(self.visible), 200000).casefold()
        return self.challenge_form or title in {"just a moment...", "security verification", "robot check"} or any(
            term in text for term in (
                "our systems have detected unusual traffic", "verify you are human", "verify that you are human",
                "please complete the following challenge", "bots use duckduckgo too", "请完成安全验证", "请输入验证码",
                "访问过于频繁", "enable javascript to continue", "enable javascript and cookies to continue",
            ))


def unwrap(href, base):
    """Decode only documented query redirects on a small provider allowlist."""
    if not isinstance(href, str) or len(href) > 8192 or any(char.isspace() for char in href):
        return ""
    value = urljoin(base, href)
    for _ in range(2):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            if parsed.username is not None or parsed.password is not None or parsed.scheme not in {"https", "http"}:
                return ""
            query = parse_qs(parsed.query)
            candidate = None
            if host in {"www.google.com", "google.com"} and parsed.path == "/url":
                candidate = (query.get("q") or query.get("url") or [""])[0]
            elif host in {"duckduckgo.com", "html.duckduckgo.com", "www.duckduckgo.com"}:
                candidate = (query.get("uddg") or [""])[0] or None
            elif host in {"www.bing.com", "cn.bing.com", "bing.com"} and parsed.path == "/ck/a":
                encoded = (query.get("u") or [""])[0]
                if encoded.startswith("a1"):
                    payload = encoded[2:]
                    candidate = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True).decode("utf-8")
            if not candidate:
                break
            value = candidate
        except (ValueError, TypeError, UnicodeError):
            return ""
    try:
        parsed = urlsplit(value)
        # Some search indexes retain HTTP originals. This is identity-only:
        # the resulting article URL is not fetched by this diagnostic.
        if parsed.scheme == "http" and parsed.netloc.casefold() in OFFICIAL_X | OFFICIAL_SNOW:
            value = parsed._replace(scheme="https").geturl()
    except ValueError:
        return ""
    return value


class NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_bounded(response, deadline):
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        raise ValueError("response_too_large")
    parts, total = [], 0
    reader = getattr(response, "read1", response.read)
    while total <= MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        part = reader(min(32768, MAX_BYTES + 1 - total))
        if not part:
            break
        parts.append(part)
        total += len(part)
    if total > MAX_BYTES:
        raise ValueError("response_too_large")
    return b"".join(parts)


def run_probe(probe):
    started = time.monotonic()
    report = {"engine": probe.engine, "source": probe.source, "http_status": 0, "challenge": False,
              "anchor_count": 0, "target_domain_count": 0, "identity_count": 0,
              "accepted": [], "full_text_read": False}
    try:
        opener = request.build_opener(NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
        req = request.Request(probe.url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html",
                                                "Accept-Encoding": "identity"})
        with opener.open(req, timeout=TIMEOUT) as response:
            report["http_status"] = response.status
            raw = read_bounded(response, started + TIMEOUT)
        parser = SearchHTML()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.finish()
        report["page_title"] = clean(" ".join(parser.title_parts), 160)
        report["challenge"] = parser.challenged()
        report["anchor_count"] = len(parser.anchors)
        if not report["challenge"]:
            accepted = {}
            for href, title in parser.anchors:
                value = unwrap(href, probe.url)
                try:
                    host = urlsplit(value).netloc.casefold()
                    if host in (OFFICIAL_X if probe.source == "X" else OFFICIAL_SNOW):
                        report["target_domain_count"] += 1
                    if probe.source == "X":
                        canonical, identity, _ = canonical_post_url(value)
                    else:
                        canonical, _, identity = canonical_article_url(value)
                except (ValueError, TypeError):
                    continue
                if identity not in accepted or len(title) > len(accepted[identity]["title"]):
                    accepted[identity] = {"url": canonical, "title": title}
            report["identity_count"] = len(accepted)
            report["accepted"] = list(accepted.values())[:5]
    except error.HTTPError as exc:
        report["http_status"] = exc.code
        report["error"] = "http_rejected"
        exc.close()
    except (TimeoutError, error.URLError) as exc:
        report["error"] = "timeout" if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError) else "network"
    except (OSError, HTTPException):
        report["error"] = "network"
    except ValueError:
        report["error"] = "response_rejected"
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


def main():
    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(run_probe, probes()))
    result = {"reports": reports, "request_limit": 6, "model_called": False, "mail_sent": False}
    directory = Path(".local")
    directory.mkdir(exist_ok=True)
    (directory / "public-search-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for report in reports:
        value = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        print("::notice title=Public search identity candidates (unread)::" + value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
