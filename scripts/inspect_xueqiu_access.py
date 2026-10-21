"""Inspect two anonymous official reads; no cookies, login, model or SMTP.

This diagnostic does not bypass challenges. A historical public client uses the
single-status JSON path below, with an optional Cookie header. Its existence is
evidence of the route, not evidence that anonymous access currently succeeds:
https://github.com/liuchenglong-THUIBE/xueqiu-crawler/blob/main/xq/crawler/api_client.py#L283-L296
"""

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import json
from pathlib import Path
import re
import ssl
import sys
import time
from urllib import error, request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest import xueqiu


DEFAULT_URL = "https://xueqiu.com/1784314127/367934699"
JSON_ROUTE_REFERENCE = (
    "https://github.com/liuchenglong-THUIBE/xueqiu-crawler/blob/main/xq/crawler/api_client.py#L283-L296"
)
_IGNORED = {"script", "style", "noscript", "iframe", "svg", "template"}
_KNOWN_KEYS = {"id", "user_id", "user", "title", "text", "created_at", "status", "data",
               "error_code", "error_description", "error", "message", "success"}


def _preview(value: str, limit: int = 150) -> str:
    """Short visible text only; never emit HTML, URLs or long opaque tokens."""
    value = " ".join(value.split())
    value = re.sub(r"https?://\S+", "[url]", value)
    value = re.sub(r"[A-Za-z0-9_+/-]{24,}={0,2}", "[opaque value]", value)
    value = re.sub(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", value)
    return "".join(char for char in value if char.isprintable() and char not in "<>`")[:limit]


class _Shape(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.visible = []
        self.in_title = False
        self.ignored = None
        self.script_count = 0
        self.script_types = set()
        self.script_names = set()
        self.json_ld_count = 0
        self.article_css = False
        self.password_field = False
        self.refresh_meta = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            self.script_count += 1
            kind = (values.get("type") or "text/javascript").split(";", 1)[0].strip().lower()
            if kind in {"text/javascript", "application/javascript", "application/json", "application/ld+json", "module"}:
                self.script_types.add(kind)
            else:
                self.script_types.add("other")
            self.json_ld_count += kind == "application/ld+json"
            src = values.get("src")
            if isinstance(src, str):
                try:
                    name = Path(urlsplit(src).path).name
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
                        self.script_names.add(name)
                except ValueError:
                    pass
        if not self.ignored and tag in _IGNORED:
            self.ignored = tag
        if tag == "title":
            self.in_title = True
        if "article__bd__detail" in (values.get("class") or "").split():
            self.article_css = True
        if tag == "input" and (values.get("type") or "").lower() == "password":
            self.password_field = True
        if tag == "meta" and (values.get("http-equiv") or "").lower() == "refresh":
            self.refresh_meta = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.ignored == tag:
            self.ignored = None

    def handle_data(self, value):
        if self.in_title:
            self.title.append(value)
        elif not self.ignored:
            self.visible.append(value)


def _inspect_body(raw: bytes, article_url: str, now: datetime) -> dict:
    text = raw.decode("utf-8-sig", errors="replace")
    shape = _Shape()
    shape.feed(text)
    shape.close()
    title = " ".join(shape.title)
    visible = " ".join(shape.visible)
    lower = text.lower()
    visible_lower = (title + " " + visible).lower()
    result = {
        "title": _preview(title),
        "visible_text_preview": _preview(visible),
        "script_count": shape.script_count,
        "script_types": sorted(shape.script_types),
        "script_src_names": sorted(shape.script_names)[:12],
        "known_article_css": shape.article_css,
        "json_ld_script_count": shape.json_ld_count,
        "embedded_status_markers": [name for name in ("SNOWMAN_STATUS", "SNOWMAN_DATA") if name in text],
        "challenge_indicators": {
            "visible_verification_words": any(word in visible_lower for word in (
                "验证码", "安全验证", "访问验证", "人机验证", "captcha", "verify you are human", "access denied")),
            "waf_cookie_marker": any(word in lower for word in (
                "acw_sc__v2", "__jsl_clearance", "cf-chl-", "aliyun_waf")),
            "cookie_assignment_script": "document.cookie" in lower,
            "password_input": shape.password_field,
            "refresh_meta": shape.refresh_meta,
            "login_title": any(word in title.lower() for word in ("登录", "登陆", "log in", "sign in")),
        },
        "json_document": False,
        "known_json_top_level_keys": [],
        "matching_status_identity_present": False,
        "verified_parse_count": 0,
        "parse_reason": "unverified_page",
    }
    # JSON is inspected as a record, never emitted; remote error messages and
    # arbitrary top-level keys must not become diagnostic output.
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        value = None
    if isinstance(value, (dict, list)):
        result["json_document"] = True
        result["visible_text_preview"] = ""
        if isinstance(value, dict):
            result["known_json_top_level_keys"] = sorted(_KNOWN_KEYS.intersection(value))
        _, author_id, article_id = xueqiu.canonical_article_url(article_url)
        for item in xueqiu._objects(value):
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            identities = [str(v) for v in (item.get("user_id"), user.get("id")) if v is not None]
            if str(item.get("id")) == article_id and identities and all(v == author_id for v in identities):
                result["matching_status_identity_present"] = True
                break
    try:
        xueqiu._parse_page(raw, article_url, now)
        result["verified_parse_count"] = 1
        result["parse_reason"] = "verified"
    except xueqiu._UnverifiedPage as exc:
        result["parse_reason"] = str(exc)
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    return result


def _read_once(endpoint: str, *, kind: str, article_url: str, now: datetime) -> dict:
    result = {"kind": kind, "http_status": None, "bytes": 0, "result": "connection_failed"}
    deadline = time.monotonic() + xueqiu.REQUEST_TIMEOUT
    try:
        opener = request.build_opener(xueqiu._NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
        req = request.Request(endpoint, headers={"Accept": "application/json, text/html",
                                                 "User-Agent": "DailyReadingDigest/0.2"})
        with opener.open(req, timeout=xueqiu.REQUEST_TIMEOUT) as response:
            result["http_status"] = response.status
            content_type = response.headers.get("Content-Type", "").lower()
            result["content_type"] = ("json" if "json" in content_type else
                                      "html" if "html" in content_type else "other")
            raw = xueqiu._read_body(response, deadline)
            result["bytes"] = len(raw)
        result.update(_inspect_body(raw, article_url, now))
        result["result"] = "inspected"
    except error.HTTPError as exc:
        result["http_status"] = exc.code
        result["result"] = {401: "access_denied", 403: "access_denied", 404: "not_found",
                            429: "rate_limited"}.get(exc.code, "http_error")
        if exc.code in {301, 302, 303, 307, 308}:
            result["result"] = "redirect_not_followed"
        exc.close()
    except xueqiu._UnverifiedPage as exc:
        result["result"] = str(exc)
    except (error.URLError, HTTPException, OSError):
        pass
    except (ValueError, TypeError, OverflowError, RecursionError):
        result["result"] = "unverified_page"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("article_url", nargs="?", default=DEFAULT_URL)
    args = parser.parse_args(argv)
    try:
        article_url, _, article_id = xueqiu.canonical_article_url(args.article_url)
    except (TypeError, ValueError):
        parser.error("Expected an official HTTPS numeric Xueqiu article URL")
    now = datetime.now(timezone.utc)
    report = {"article_url": article_url, "anonymous_only": True, "authorization_sent": False,
              "cookie_sent": False, "model_called": False, "mail_sent": False,
              "json_route_reference": JSON_ROUTE_REFERENCE,
              "requests": [
                  _read_once(article_url, kind="article_html", article_url=article_url, now=now),
                  _read_once(f"https://api.xueqiu.com/statuses/show.json?id={article_id}", kind="standard_status_json",
                             article_url=article_url, now=now),
              ]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    annotation = json.dumps(report, ensure_ascii=True, separators=(",", ":"))
    annotation = annotation.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::notice title=Xueqiu access shape::" + annotation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
