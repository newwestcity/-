"""Validate source links and select a diverse digest without inventing posts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit


POST_PATH = re.compile(r"/([A-Za-z0-9_]{1,15})/status/([1-9][0-9]{0,24})/?\Z")


def canonical_post_url(url: str) -> tuple[str, str, str]:
    """Return canonical URL, post ID and handle; reject non-post destinations."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Invalid X post URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
        raise ValueError("Only HTTPS X/Twitter post links are accepted")
    match = POST_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Link does not identify an X post")
    handle, post_id = match.groups()
    return f"https://x.com/{handle}/status/{post_id}", post_id, handle


def select_posts(
    candidates: list[dict],
    *,
    categories: list[str],
    target_count: int = 10,
    max_per_category: int = 3,
    max_per_author: int = 2,
    blocked_accounts: list[str] | None = None,
    seen_ids: set[str] | None = None,
    cited_urls: list[str],
    now: datetime | None = None,
    max_age_days: int = 7,
    max_offer_age_hours: int = 72,
) -> list[dict]:
    """Accept only cited, unseen posts, then reserve a slot per available topic.

    Candidate order is editorial preference order from the search/ranking
    provider. Citations must come from provider metadata, not generated prose.
    """
    if target_count < 1 or max_per_category < 1 or max_per_author < 1:
        raise ValueError("Selection limits must be positive")
    if now is not None and now.utcoffset() is None:
        raise ValueError("Current time must include a timezone")
    sources: dict[str, tuple[str, str]] = {}
    for url in cited_urls:
        try:
            source_url, source_id, source_handle = canonical_post_url(url)
            sources.setdefault(source_id, (source_url, source_handle))
        except (ValueError, TypeError):
            continue
    blocked = {a.lstrip("@").casefold() for a in (blocked_accounts or [])}
    dedup = set(seen_ids or ())
    valid: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            url, post_id, handle = canonical_post_url(item.get("url"))
        except (ValueError, TypeError):
            continue
        if post_id not in sources or post_id in dedup:
            continue
        # The source metadata supplies the author; generated text cannot rename
        # a blocked account while retaining the cited post's numeric ID.
        url, handle = sources[post_id]
        if handle.casefold() in blocked:
            continue
        if item.get("category") not in categories:
            continue
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("summary", "reason")):
            continue
        if now is not None:
            try:
                published = datetime.fromtimestamp(((int(post_id) >> 22) + 1288834974657) / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            age_limit = timedelta(hours=max_offer_age_hours) if item["category"] == "学生羊毛" else timedelta(days=max_age_days)
            if published > now + timedelta(minutes=5) or now - published > age_limit:
                continue
        dedup.add(post_id)
        valid.append({**item, "id": post_id, "url": url, "author": "@" + handle})

    chosen: list[dict] = []
    used: set[str] = set()
    topic_counts: Counter = Counter()
    author_counts: Counter = Counter()

    def take(item: dict) -> bool:
        author = item["author"].casefold()
        topic = item["category"]
        if item["id"] in used or len(chosen) >= target_count:
            return False
        if topic_counts[topic] >= max_per_category or author_counts[author] >= max_per_author:
            return False
        chosen.append(item)
        used.add(item["id"])
        topic_counts[topic] += 1
        author_counts[author] += 1
        return True

    for topic in categories:
        for item in valid:
            if item["category"] == topic and take(item):
                break
    for item in valid:
        take(item)
    return chosen
