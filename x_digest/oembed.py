"""Read candidate posts from X's public, unauthenticated oEmbed endpoint.

The endpoint returns fallback markup, not a search API or a guarantee that a
post remains available. Only text returned by X is accepted here; callers are
responsible for age limits and editorial selection.

Official contract: https://docs.x.com/x-for-websites/oembed-api
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import ssl
import time
from urllib import error, request
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from .selection import canonical_post_url


MAX_RESPONSE_BYTES = 512 * 1024
REQUEST_TIMEOUT = 10
MAX_POSTS = 20
_PUBLISHERS = {"publish.x.com", "publish.twitter.com"}
_POST_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com",
               "mobile.x.com", "mobile.twitter.com"}
_HANDLE_PATH = re.compile(r"/([A-Za-z0-9_]{1,15})/?\Z")
_RESERVED_HANDLES = {"i", "home", "search", "explore", "intent", "share",
                     "notifications", "messages", "compose", "settings",
                     "login", "signup", "tos", "privacy"}
_EPOCH = datetime(2010, 11, 4, 1, 42, 54, 657000, tzinfo=timezone.utc)


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Handle at most one approved redirect ourselves, without copying headers.
        return None


class _PostHTML(HTMLParser):
    """Read one fallback blockquote; links inside its text are not permalinks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.permalinks: list[str] = []
        self.blocks = 0
        self.block_depth = 0
        self.paragraph_depth = 0
        self.ignored: str | None = None
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            return
        if tag in {"script", "style"}:
            self.ignored = tag
            return
        attributes = dict(attrs)
        if tag == "blockquote":
            if self.block_depth:
                self.block_depth += 1
                self.invalid = True
            elif "twitter-tweet" in (attributes.get("class") or "").split():
                self.blocks += 1
                self.block_depth = 1
            return
        if not self.block_depth:
            return
        if tag == "p":
            if self.paragraph_depth:
                self.invalid = True
            if self.text_parts:
                self.text_parts.append("\n")
            self.paragraph_depth += 1
        elif tag == "br" and self.paragraph_depth:
            self.text_parts.append("\n")
        elif tag == "a" and not self.paragraph_depth:
            href = attributes.get("href")
            if isinstance(href, str):
                self.permalinks.append(href)

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored:
                self.ignored = None
            return
        if not self.block_depth:
            return
        if tag == "p":
            if self.paragraph_depth:
                self.paragraph_depth -= 1
        elif tag == "blockquote":
            if self.paragraph_depth:
                self.invalid = True
            self.block_depth -= 1

    def handle_data(self, data):
        if self.block_depth and self.paragraph_depth and not self.ignored:
            self.text_parts.append(data)


def _author_handle(url: object) -> str:
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Invalid author URL")
    parsed = urlsplit(url)
    match = _HANDLE_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.netloc.lower() not in _POST_HOSTS
            or parsed.query or parsed.fragment or not match):
        raise ValueError("Invalid author URL")
    handle = match.group(1)
    if handle.casefold() in _RESERVED_HANDLES:
        raise ValueError("Invalid author URL")
    return handle


def _parse_post(data: object, post_id: str, now: datetime) -> dict:
    if not isinstance(data, dict) or data.get("type") != "rich":
        raise ValueError("Missing post markup")
    if canonical_post_url(data.get("url"))[1] != post_id:
        raise ValueError("Post ID mismatch")
    handle = _author_handle(data.get("author_url"))
    html = data.get("html")
    if not isinstance(html, str):
        raise ValueError("Missing post markup")
    parser = _PostHTML()
    parser.feed(html)
    parser.close()
    if parser.invalid or parser.blocks != 1 or parser.block_depth or parser.paragraph_depth or parser.ignored:
        raise ValueError("Invalid post markup")
    permalink_ids = []
    for permalink in parser.permalinks:
        try:
            permalink_ids.append(canonical_post_url(permalink)[1])
        except (TypeError, ValueError):
            continue
    if not permalink_ids or any(value != post_id for value in permalink_ids):
        raise ValueError("Post permalink mismatch")
    text = " ".join("".join(parser.text_parts).split())[:4000].rstrip()
    if not text:
        raise ValueError("Missing post text")
    published = _EPOCH + timedelta(milliseconds=int(post_id) >> 22)
    if published > now + timedelta(minutes=5):
        raise ValueError("Future post")
    return {"id": post_id, "url": f"https://x.com/{handle}/status/{post_id}",
            "author": handle, "text": text, "published_at": published.isoformat(),
            "verified_at": now.isoformat()}


def _redirect_url(current: str, location: object) -> str:
    if not isinstance(location, str) or any(c.isspace() for c in location):
        raise ValueError("Invalid publisher redirect")
    target = urlsplit(urljoin(current, location))
    source = urlsplit(current)
    if (target.scheme != "https" or target.netloc.lower() not in _PUBLISHERS
            or target.netloc.lower() == source.netloc.lower()
            or target.path != "/oembed" or target.fragment):
        raise ValueError("Invalid publisher redirect")
    # Only the official publisher hostname may change. Do not accept a new
    # candidate URL or new parameters supplied by a redirect response.
    return urlunsplit(("https", target.netloc.lower(), "/oembed", source.query, ""))


def _read_body(response, deadline: float) -> bytes:
    """Bound both bytes and elapsed time, including a slowly streamed body."""
    chunks: list[bytes] = []
    total = 0
    # HTTPResponse.read1 makes at most one underlying read; read(n) may wait
    # indefinitely for n bytes while a server keeps trickling smaller chunks.
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
    if total > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
        raise ValueError("Post response exceeded limits")
    return b"".join(chunks)


def _fetch_one(candidate: tuple[str, str], now: datetime) -> dict | None:
    url, post_id = candidate
    endpoint = "https://publish.x.com/oembed?" + urlencode({
        "url": url, "omit_script": "true", "hide_thread": "true",
    })
    deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        opener = request.build_opener(
            _NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()),
        )
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            req = request.Request(endpoint, method="GET", headers={
                "Accept": "application/json", "User-Agent": "X-Digest-oEmbed/1.0",
            })
            try:
                with opener.open(req, timeout=remaining) as response:
                    headers = getattr(response, "headers", None)
                    size = headers.get("Content-Length") if headers is not None else None
                    if size is not None and (int(size) < 0 or int(size) > MAX_RESPONSE_BYTES):
                        return None
                    raw = _read_body(response, deadline)
                return _parse_post(json.loads(raw), post_id, now)
            except error.HTTPError as exc:
                try:
                    if attempt or exc.code not in {301, 302, 303, 307, 308}:
                        return None
                    location = exc.headers.get("Location") if exc.headers is not None else None
                    endpoint = _redirect_url(endpoint, location)
                finally:
                    exc.close()
    except (error.URLError, HTTPException, OSError, ValueError, TypeError,
            OverflowError, RecursionError):
        # No raw response or exception details are logged or passed to callers.
        return None
    return None


def fetch_verified_posts(urls: list[str], *, now: datetime, limit: int = MAX_POSTS) -> list[dict]:
    """Fetch up to 20 unique candidates, at most two concurrently, without retries.

    Each candidate has a ten-second timeout and a 512 KiB response cap. A single
    HTTPS /oembed redirect between the two official publisher hosts is allowed.
    The function takes no model, API, mail or other credentials.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("Verification time must include a timezone")
    if not isinstance(urls, list) or type(limit) is not int or not 1 <= limit <= MAX_POSTS:
        raise ValueError("Expected a URL list and a limit between 1 and 20")
    utc_now = now.astimezone(timezone.utc)
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in urls:
        try:
            canonical, post_id, _ = canonical_post_url(url)
            published = _EPOCH + timedelta(milliseconds=int(post_id) >> 22)
        except (TypeError, ValueError, OverflowError):
            continue
        if post_id in seen or published > utc_now + timedelta(minutes=5):
            continue
        seen.add(post_id)
        candidates.append((canonical, post_id))
        if len(candidates) >= limit:
            break
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=2) as executor:
        return [post for post in executor.map(lambda item: _fetch_one(item, utc_now), candidates)
                if post is not None]
