"""Discover article links and titles through Bing's public RSS search feed.

This does not fetch Xueqiu article bodies or establish publication dates. Titles
remain search metadata and must be labelled as unread article suggestions.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.client import HTTPException
import re
import ssl
import time
from urllib import error, request
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

from .discovery import XueqiuDiscoveryResult, _SEEN, _title
from .xueqiu import canonical_article_url


REQUEST_TIMEOUT = 15
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CANDIDATES = 12
_OFFICIAL_HOSTS = {"xueqiu.com", "www.xueqiu.com"}
_QUERIES = (
    "site:xueqiu.com 价值投资",
    "site:xueqiu.com 财报分析",
)


class _FeedError(ValueError):
    pass


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _feed_url(value: object) -> tuple[str, bool]:
    """Upgrade an official HTTP identity without following its source URL."""
    try:
        parsed = urlsplit(value.strip()) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in {"http", "https"}:
        raise _FeedError("invalid_url_scheme")
    if parsed.netloc.lower() not in _OFFICIAL_HOSTS:
        raise _FeedError("invalid_url_host")
    upgraded = parsed.scheme == "http"
    return parsed._replace(scheme="https").geturl(), upgraded


def _rejection_reason(value: object) -> str:
    """Return one fixed URL-shape diagnostic; never decode tracking links."""
    try:
        normalised, _ = _feed_url(value)
        canonical_article_url(normalised)
    except _FeedError as exc:
        return str(exc)
    except (ValueError, TypeError):
        return "invalid_article_path"
    return ""


def _read(response, deadline: float) -> bytes:
    headers = getattr(response, "headers", {})
    declared = headers.get("Content-Length")
    if isinstance(declared, str) and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise _FeedError("response_too_large")
    parts, total = [], 0
    reader = getattr(response, "read1", response.read)
    while total <= MAX_RESPONSE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        part = reader(min(32768, MAX_RESPONSE_BYTES + 1 - total))
        if not part:
            break
        parts.append(part)
        total += len(part)
    if total > MAX_RESPONSE_BYTES:
        raise _FeedError("response_too_large")
    if time.monotonic() > deadline:
        raise TimeoutError
    return b"".join(parts)


def _parse(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
        # Do not let an XML parser reinterpret NUL-padded bytes as UTF-16
        # after the UTF-8-only declaration check below.
        if "\x00" in text:
            raise _FeedError("invalid_feed")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
            raise _FeedError("xml_declaration_rejected")
        root = ElementTree.fromstring(text)
    except (UnicodeError, ElementTree.ParseError):
        raise _FeedError("invalid_feed") from None
    if root.tag != "rss" or root.find("channel") is None:
        raise _FeedError("invalid_feed")
    records = []
    for item in root.findall("./channel/item")[:100]:
        title = item.find("title")
        records.append({"url": item.findtext("link") or "",
                        "title": "".join(title.itertext()) if title is not None else ""})
    return records


def _fetch(query: str) -> tuple[list[dict], str]:
    # The endpoint and host are fixed locally; returned links are never fetched.
    endpoint = "https://www.bing.com/search?" + urlencode(
        {"format": "rss", "q": query, "mkt": "zh-CN", "setlang": "zh-hans"})
    deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        opener = request.build_opener(_NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
        req = request.Request(endpoint, headers={"Accept": "application/rss+xml, application/xml, text/xml",
                                                 "User-Agent": "DailyReadingDigest/0.2"})
        with opener.open(req, timeout=REQUEST_TIMEOUT) as response:
            raw = _read(response, deadline)
        return _parse(raw), "completed"
    except _FeedError as exc:
        return [], str(exc)
    except error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in {301, 302, 303, 307, 308}:
            return [], "redirect_rejected"
        return [], "http_" + str(status) if status in {401, 403, 404, 429} else "http_error"
    except (error.URLError, HTTPException, OSError, ValueError, TypeError):
        return [], "connection_failed"


def discover_xueqiu_feed(*, now: datetime, seen_ids: set[str]) -> XueqiuDiscoveryResult:
    """Read at most two public RSS feeds, without API keys, cookies or retries."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("RSS discovery time must include a timezone")
    if (not isinstance(seen_ids, set)
            or any(not isinstance(value, str) or not _SEEN.fullmatch(value) for value in seen_ids)):
        raise ValueError("Invalid reading history IDs")
    diagnostics = {"feed_requests": 2, "feeds_completed": 0, "invalid_url": 0,
                   "invalid_url_scheme": 0, "invalid_url_host": 0, "invalid_article_path": 0,
                   "http_knownhost": 0,
                   "empty_title": 0, "duplicate_url": 0, "seen_url": 0,
                   "candidate_urls": 0, "metadata_titled_candidates": 0}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_fetch, _QUERIES))
    candidates, used = [], set()
    for records, code in results:
        if code != "completed":
            diagnostics[code] = diagnostics.get(code, 0) + 1
            continue
        diagnostics["feeds_completed"] += 1
        for record in records:
            try:
                normalised, upgraded = _feed_url(record["url"])
                diagnostics["http_knownhost"] += upgraded
                url, _, article_id = canonical_article_url(normalised)
            except _FeedError as exc:
                diagnostics["invalid_url"] += 1
                diagnostics[str(exc)] += 1
                continue
            except (ValueError, TypeError):
                diagnostics["invalid_url"] += 1
                diagnostics["invalid_article_path"] += 1
                continue
            identity = "xueqiu:" + article_id
            if identity in used:
                diagnostics["duplicate_url"] += 1
                continue
            title = _title(record.get("title"))[:240].strip()
            if not title:
                diagnostics["empty_title"] += 1
                continue
            used.add(identity)
            if identity in seen_ids:
                diagnostics["seen_url"] += 1
                continue
            if len(candidates) < MAX_CANDIDATES:
                candidates.append({"id": identity, "url": url, "source": "雪球", "title": title,
                                   "discovery_basis": "search_metadata", "search_provider": "Bing RSS",
                                   "discovered_at": now.isoformat()})
    diagnostics["candidate_urls"] = len(candidates)
    diagnostics["metadata_titled_candidates"] = len(candidates)
    return XueqiuDiscoveryResult([value["url"] for value in candidates], diagnostics, candidates)
