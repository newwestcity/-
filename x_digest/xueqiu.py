"""Read publicly available Xueqiu articles without login or session cookies.

The live site may require authentication or reject automated access. Such pages
are skipped, never treated as article text. Historical public clients document
``id/user_id/title/text/created_at`` and ``article__bd__detail``; those sources
are parser evidence, not proof that a current live request will succeed.

https://github.com/py-bin/xueqiu_spider/blob/master/spider/cssw_craw.py
https://github.com/wangshichen50/xueqiu_articleget/blob/master/articleget.py
https://github.com/chenkongming/xueqiu-article-crawler/blob/prod/report.py
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import ssl
import time
from urllib import error, request
from urllib.parse import urljoin, urlsplit


MAX_ARTICLES = 12
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARACTERS = 24000
MIN_BODY_CHARACTERS = 300
REQUEST_TIMEOUT = 15
_HOSTS = {"xueqiu.com", "www.xueqiu.com"}
_ARTICLE_PATH = re.compile(r"/([1-9][0-9]{0,19})/([1-9][0-9]{0,19})/?\Z")
_USER_PATH = re.compile(r"/([1-9][0-9]{0,19})/?\Z")
_BOOTSTRAP = re.compile(r"(?:window\.)?(?:SNOWMAN_STATUS|SNOWMAN_DATA)\s*=\s*")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "blockquote", "br"}


@dataclass
class ArticleFetchResult:
    articles: list[dict]
    diagnostics: dict[str, int]
    outcomes: dict[str, str] = field(default_factory=dict)


class _UnverifiedPage(ValueError):
    """A fixed diagnostic label; never includes remote text or URL values."""


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def canonical_article_url(url: str) -> tuple[str, str, str]:
    """Return HTTPS URL, numeric author ID and article ID, dropping tracking."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Invalid Xueqiu article URL")
    parsed = urlsplit(url)
    match = _ARTICLE_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc.lower() not in _HOSTS or not match:
        raise ValueError("Only official HTTPS Xueqiu article links are accepted")
    author_id, article_id = match.groups()
    return f"https://xueqiu.com/{author_id}/{article_id}", author_id, article_id


def _identity_url(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    # An old page's metadata may use HTTP. This is identity comparison only;
    # network requests and redirects always require HTTPS.
    if value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    try:
        return canonical_article_url(value)
    except (TypeError, ValueError):
        return None


class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: str | None = None

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            return
        if tag in {"script", "style", "noscript", "iframe", "svg", "template"}:
            self.ignored = tag
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored:
                self.ignored = None
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextHTML()
    parser.feed(value)
    parser.close()
    return _normalise_text("".join(parser.parts))


def _normalise_text(value: str) -> str:
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


class _ArticlePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical: list[str] = []
        self.scripts: list[tuple[str, str]] = []
        self.script_type: str | None = None
        self.script_parts: list[str] = []
        self.body_depth = 0
        self.body_tags: list[str] = []
        self.body_invalid = False
        self.body_parts: list[str] = []
        self.body_blocks = 0
        self.ignored: str | None = None
        self.h1_depth = 0
        self.title_parts: list[str] = []
        self.author = ""
        self.published = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.script_type is not None:
            return
        if tag == "script":
            self.script_type = (values.get("type") or "text/javascript").split(";", 1)[0].strip().lower()
            self.script_parts = []
            return
        if self.ignored:
            return
        if tag in {"style", "noscript", "iframe", "svg", "template"}:
            self.ignored = tag
            return
        if tag == "link" and "canonical" in (values.get("rel") or "").lower().split():
            self.canonical.append(values.get("href") or "")
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content") or ""
            if name == "og:url":
                self.canonical.append(content)
            elif name == "author":
                self.author = content
            elif name == "article:published_time":
                self.published = content
        if tag == "h1":
            self.h1_depth += 1
        elif self.h1_depth and tag not in _VOID_TAGS:
            self.h1_depth += 1
        if self.body_depth:
            if tag not in _VOID_TAGS:
                self.body_tags.append(tag)
                self.body_depth = len(self.body_tags)
            if tag in _BLOCK_TAGS:
                self.body_parts.append("\n")
        elif "article__bd__detail" in (values.get("class") or "").split():
            self.body_blocks += 1
            self.body_tags = [tag]
            self.body_depth = 1

    def handle_endtag(self, tag):
        if self.script_type is not None:
            if tag == "script":
                self.scripts.append((self.script_type, "".join(self.script_parts)))
                self.script_type = None
            return
        if self.ignored:
            if tag == self.ignored:
                self.ignored = None
            return
        if self.h1_depth:
            self.h1_depth -= 1
        if self.body_depth:
            if tag in _BLOCK_TAGS:
                self.body_parts.append("\n")
            if tag not in _VOID_TAGS:
                if self.body_tags[-1] != tag:
                    self.body_invalid = True
                    self.body_tags = []
                else:
                    self.body_tags.pop()
                self.body_depth = len(self.body_tags)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.script_type is not None:
            self.script_parts.append(data)
        elif not self.ignored:
            if self.body_depth:
                self.body_parts.append(data)
            if self.h1_depth:
                self.title_parts.append(data)


def _publication(value: object, now: datetime) -> str:
    published = None
    if type(value) is int or (isinstance(value, str) and value.isdigit()):
        number = int(value)
        # Xueqiu's documented created_at convention is Unix milliseconds.
        if number >= 10 ** 12:
            try:
                published = datetime.fromtimestamp(number / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                return ""
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                published = parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    if published is None:
        return ""
    if published > now + timedelta(minutes=5):
        raise _UnverifiedPage("future_date")
    return published.isoformat()


def _article(*, url: str, author: object, title: object, text: object, date: object, now: datetime,
             text_is_plain: bool = False) -> dict:
    canonical, author_id, article_id = canonical_article_url(url)
    plain = _normalise_text(text) if text_is_plain and isinstance(text, str) else _plain_text(text)
    if len(plain) < MIN_BODY_CHARACTERS:
        raise _UnverifiedPage("body_too_short")
    return {"id": "xueqiu:" + article_id, "url": canonical, "source": "雪球",
            "author": _plain_text(author).strip()[:200] or author_id, "author_id": author_id,
            "title": _plain_text(title).strip()[:300], "text": plain[:MAX_TEXT_CHARACTERS].rstrip(),
            "text_characters": len(plain), "text_truncated": len(plain) > MAX_TEXT_CHARACTERS,
            "published_at": _publication(date, now), "verified_at": now.isoformat(),
            "content_basis": "original_text"}


def _from_status(value: object, url: str, now: datetime) -> dict | None:
    if not isinstance(value, dict):
        return None
    _, author_id, article_id = canonical_article_url(url)
    if str(value.get("id")) != article_id:
        return None
    user = value.get("user") if isinstance(value.get("user"), dict) else {}
    identifiers = [str(item) for item in (value.get("user_id"), user.get("id")) if item is not None]
    if not identifiers:
        return None
    if any(item != author_id for item in identifiers):
        raise _UnverifiedPage("identity_mismatch")
    return _article(url=url, author=user.get("screen_name") or value.get("screen_name") or "",
                    title=value.get("title"), text=value.get("text"), date=value.get("created_at"), now=now)


def _objects(value: object, *, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for child in list(value.values())[:100]:
            if isinstance(child, (dict, list)):
                yield from _objects(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:100]:
            yield from _objects(child, depth=depth + 1)


def _from_ld(value: dict, url: str, now: datetime) -> dict | None:
    kind = value.get("@type")
    kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
    if not any(item in {"Article", "BlogPosting", "NewsArticle", "SocialMediaPosting"} for item in kinds if isinstance(item, str)):
        return None
    references = [value.get("url"), value.get("@id"), value.get("mainEntityOfPage")]
    references = [item.get("@id") if isinstance(item, dict) else item for item in references]
    identities = [parsed for item in references if (parsed := _identity_url(item)) is not None]
    if not identities or any(item[0] != url for item in identities):
        return None
    author = value.get("author")
    if isinstance(author, list):
        author = author[0] if len(author) == 1 else {}
    if isinstance(author, dict):
        author_url = author.get("url") or author.get("@id")
        if isinstance(author_url, str):
            parsed = urlsplit(author_url)
            match = _USER_PATH.fullmatch(parsed.path)
            if parsed.hostname in _HOSTS and match and match.group(1) != canonical_article_url(url)[1]:
                raise _UnverifiedPage("identity_mismatch")
        author_name = author.get("name")
    else:
        author_name = author
    return _article(url=url, author=author_name, title=value.get("headline") or value.get("name"),
                    text=value.get("articleBody"), date=value.get("datePublished"), now=now)


def _parse_page(raw: bytes, url: str, now: datetime) -> dict:
    text = raw.decode("utf-8-sig")
    if text.lstrip().startswith(("{", "[")):
        for value in _objects(json.loads(text)):
            article = _from_status(value, url, now)
            if article is not None:
                return article
        raise _UnverifiedPage("unverified_page")
    page = _ArticlePage()
    page.feed(text)
    page.close()
    identities = [parsed for item in page.canonical if (parsed := _identity_url(item)) is not None]
    if any(identity[0] != url for identity in identities):
        raise _UnverifiedPage("identity_mismatch")
    for kind, script in page.scripts[:64]:
        values = []
        if kind in {"application/ld+json", "application/json"}:
            try:
                values.append(json.loads(script))
            except (ValueError, RecursionError):
                continue
        elif kind in {"text/javascript", "application/javascript"}:
            for match in list(_BOOTSTRAP.finditer(script))[:8]:
                try:
                    values.append(json.JSONDecoder().raw_decode(script, match.end())[0])
                except (ValueError, RecursionError):
                    continue
        for value in values:
            for candidate in _objects(value):
                article = (_from_ld(candidate, url, now) if kind == "application/ld+json"
                           else _from_status(candidate, url, now))
                if article is not None:
                    return article
    if identities and page.body_blocks == 1 and not page.body_depth and not page.body_invalid:
        return _article(url=url, author=page.author, title="".join(page.title_parts),
                        text="\n".join(page.body_parts), date=page.published, now=now, text_is_plain=True)
    if any(marker in text.lower() for marker in ("acw_sc__v2", "_waf_", "__jsl_clearance")):
        raise _UnverifiedPage("access_challenge")
    raise _UnverifiedPage("unverified_page")


def _read_body(response, deadline: float) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            declared_length = int(length)
        except (ValueError, TypeError):
            declared_length = 0
        if declared_length > MAX_RESPONSE_BYTES:
            raise _UnverifiedPage("response_too_large")
    chunks, total = [], 0
    reader = getattr(response, "read1", response.read)
    while total <= MAX_RESPONSE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = reader(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_RESPONSE_BYTES:
        raise _UnverifiedPage("response_too_large")
    if time.monotonic() > deadline:
        raise TimeoutError
    return b"".join(chunks)


def _fetch_one(url: str, now: datetime) -> tuple[dict | None, str]:
    endpoint = url
    deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        opener = request.build_opener(_NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "connection_failed"
            req = request.Request(endpoint, headers={"Accept": "text/html, application/json",
                                                     "User-Agent": "DailyReadingDigest/0.2"})
            try:
                with opener.open(req, timeout=remaining) as response:
                    raw = _read_body(response, deadline)
                return _parse_page(raw, url, now), "verified"
            except error.HTTPError as exc:
                try:
                    if exc.code in {301, 302, 303, 307, 308}:
                        if attempt >= 2:
                            return None, "redirect_rejected"
                        location = exc.headers.get("Location") if exc.headers is not None else None
                        target = urljoin(endpoint, location) if isinstance(location, str) else ""
                        try:
                            canonical, _, _ = canonical_article_url(target)
                        except (TypeError, ValueError):
                            return None, "redirect_rejected"
                        if canonical != url:
                            return None, "redirect_rejected"
                        endpoint = target
                        continue
                    return None, ({401: "access_denied", 403: "access_denied", 404: "not_found", 429: "rate_limited"}
                                  .get(exc.code, "http_error"))
                finally:
                    exc.close()
    except _UnverifiedPage as exc:
        return None, str(exc)
    except (error.URLError, HTTPException, OSError):
        return None, "connection_failed"
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None, "unverified_page"
    return None, "redirect_rejected"


def fetch_articles(urls: list[str], *, now: datetime, limit: int = MAX_ARTICLES) -> ArticleFetchResult:
    """Read bounded public candidates, without cookies, login, retries or AI calls.

    ``published_at`` is empty when the official record has no unambiguous date;
    callers must not classify that article as recent. Text may be an explicitly
    marked excerpt for articles longer than 24,000 characters.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("Reading time must include a timezone")
    if not isinstance(urls, list) or type(limit) is not int or not 1 <= limit <= MAX_ARTICLES:
        raise ValueError("Expected article URLs and a limit from 1 to 12")
    utc_now = now.astimezone(timezone.utc)
    labels = ("invalid_url", "duplicate_url", "verified", "unknown_date", "access_denied", "access_challenge", "not_found",
              "rate_limited", "http_error", "redirect_rejected", "connection_failed", "unverified_page",
              "body_too_short", "identity_mismatch", "future_date", "response_too_large")
    diagnostics = dict.fromkeys(labels, 0)
    candidates, seen = [], set()
    for value in urls:
        try:
            url = canonical_article_url(value)[0]
        except (TypeError, ValueError):
            diagnostics["invalid_url"] += 1
            continue
        if url in seen:
            diagnostics["duplicate_url"] += 1
            continue
        seen.add(url)
        candidates.append(url)
        if len(candidates) >= limit:
            break
    articles, ids, outcomes = [], set(), {}
    if candidates:
        with ThreadPoolExecutor(max_workers=2) as executor:
            for url, (article, reason) in zip(candidates, executor.map(lambda value: _fetch_one(value, utc_now), candidates)):
                reason = reason if reason in diagnostics else "unverified_page"
                outcomes[url] = reason
                diagnostics[reason] += 1
                if article is not None and article["id"] not in ids:
                    articles.append(article)
                    ids.add(article["id"])
                    diagnostics["unknown_date"] += not bool(article["published_at"])
    return ArticleFetchResult(articles, diagnostics, outcomes)
