"""Validate source links and select a diverse digest without inventing posts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit


POST_PATH = re.compile(r"/([A-Za-z0-9_]{1,15})/status/([1-9][0-9]{0,24})/?\Z")
MEDIA_POST_PATH = re.compile(r"/([A-Za-z0-9_]{1,15})/status/([1-9][0-9]{0,24})/(?:photo|video)/[1-9][0-9]*/?\Z")
WEB_POST_PATH = re.compile(r"/i/web/status/([1-9][0-9]{0,24})/?\Z")
_CLASSIC_CATEGORIES = frozenset({"股市", "投资", "读书"})
_X_EPOCH = datetime(2010, 11, 4, 1, 42, 54, 657000, tzinfo=timezone.utc)
_POST_ID = re.compile(r"[1-9][0-9]{0,24}\Z")


def canonical_post_url(url: str) -> tuple[str, str, str]:
    """Return URL, ID and handle (empty when an X routing URL omits author)."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Invalid X post URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}:
        raise ValueError("Only HTTPS X/Twitter post links are accepted")
    web_match = WEB_POST_PATH.fullmatch(parsed.path)
    if web_match:
        post_id = web_match.group(1)
        return f"https://x.com/i/status/{post_id}", post_id, ""
    match = POST_PATH.fullmatch(parsed.path) or MEDIA_POST_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Link does not identify an X post")
    handle, post_id = match.groups()
    return f"https://x.com/{handle}/status/{post_id}", post_id, "" if handle.casefold() == "i" else handle


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
    diagnostics: dict[str, int] | None = None,
    verified_classic_ids: set[str] | None = None,
    classic_categories: list[str] | None = None,
    max_classic_count: int = 0,
) -> list[dict]:
    """Accept only cited, unseen posts, then reserve a slot per available topic.

    Candidate order is editorial preference order from the search/ranking
    provider. Citations must come from provider metadata, not generated prose.
    Classics are disabled by default. Enabling them requires an aware ``now``
    and a trusted ID set populated after independent official-text verification.
    Model-generated age, date or classic flags cannot grant that exception.
    Recent posts are selected first; verified classics only fill remaining slots.
    """
    if any(type(value) is not int or value < 1
           for value in (target_count, max_per_category, max_per_author)):
        raise ValueError("Selection limits must be positive")
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(topic, str) or not topic.strip() for topic in categories)
            or len(categories) != len(set(categories))):
        raise ValueError("Categories must be a nonempty list of unique names")
    if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
        raise ValueError("Current time must include a timezone")
    if type(max_age_days) is not int or not 1 <= max_age_days <= 7:
        raise ValueError("Recent post age limit must be between 1 and 7 days")
    if type(max_offer_age_hours) is not int or not 1 <= max_offer_age_hours <= 72:
        raise ValueError("Offer age limit must be between 1 and 72 hours")
    if type(max_classic_count) is not int or not 0 <= max_classic_count <= 3:
        raise ValueError("Classic count must be an integer between 0 and 3")
    if max_classic_count and now is None:
        raise ValueError("Classic selection requires a timezone-aware current time")
    if classic_categories is not None and (
        not isinstance(classic_categories, list)
        or any(not isinstance(topic, str) for topic in classic_categories)
        or len(classic_categories) != len(set(classic_categories))
        or not set(classic_categories) <= _CLASSIC_CATEGORIES
        or not set(classic_categories) <= set(categories)
    ):
        raise ValueError("Classic categories must be unique stock, investment or reading categories present in categories")
    classic_topics = (set(categories) & _CLASSIC_CATEGORIES
                      if classic_categories is None else set(classic_categories))
    if verified_classic_ids is not None and (
        not isinstance(verified_classic_ids, set)
        or any(not isinstance(post_id, str) or not _POST_ID.fullmatch(post_id)
               for post_id in verified_classic_ids)
    ):
        raise ValueError("Verified classic IDs must be a set of positive numeric ID strings")
    trusted_classics = verified_classic_ids or set()
    counts = dict.fromkeys((
        "candidates_received", "cited_post_ids", "named_cited_post_ids",
        "invalid_url", "uncited", "seen", "unknown_author", "blocked_author",
        "invalid_category", "invalid_summary", "too_old", "future", "eligible", "selected",
        "recent_eligible", "classic_eligible", "recent_selected", "classic_selected",
    ), 0)
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(counts)
        counts = diagnostics
    sources: dict[str, tuple[str, str]] = {}
    for url in cited_urls:
        try:
            source_url, source_id, source_handle = canonical_post_url(url)
            existing = sources.get(source_id)
            if existing is None or (not existing[1] and source_handle):
                sources[source_id] = (source_url, source_handle)
        except (ValueError, TypeError):
            continue
    counts["cited_post_ids"] = len(sources)
    counts["named_cited_post_ids"] = sum(bool(handle) for _, handle in sources.values())
    blocked = {a.lstrip("@").casefold() for a in (blocked_accounts or [])}
    dedup = set(seen_ids or ())
    valid: list[dict] = []
    for item in candidates:
        counts["candidates_received"] += 1
        if not isinstance(item, dict):
            counts["invalid_url"] += 1
            continue
        try:
            url, post_id, handle = canonical_post_url(item.get("url"))
        except (ValueError, TypeError):
            counts["invalid_url"] += 1
            continue
        if post_id not in sources:
            counts["uncited"] += 1
            continue
        if post_id in dedup:
            counts["seen"] += 1
            continue
        # The source metadata supplies the author; generated text cannot rename
        # a blocked account while retaining the cited post's numeric ID.
        url, handle = sources[post_id]
        if not handle:
            counts["unknown_author"] += 1
            continue
        if handle.casefold() in blocked:
            counts["blocked_author"] += 1
            continue
        if item.get("category") not in categories:
            counts["invalid_category"] += 1
            continue
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("summary", "reason")):
            counts["invalid_summary"] += 1
            continue
        try:
            published = _X_EPOCH + timedelta(milliseconds=int(post_id) >> 22)
        except (ValueError, OverflowError, OSError):
            counts["invalid_url"] += 1
            continue
        reading_kind = "recent"
        if now is not None:
            age_limit = timedelta(hours=max_offer_age_hours) if item["category"] == "学生羊毛" else timedelta(days=max_age_days)
            if published > now + timedelta(minutes=5):
                counts["future"] += 1
                continue
            if now - published > age_limit:
                if (now - published > timedelta(days=7)
                        and max_classic_count > 0
                        and post_id in trusted_classics
                        and item["category"] in classic_topics):
                    reading_kind = "classic"
                else:
                    counts["too_old"] += 1
                    continue
        dedup.add(post_id)
        valid.append({**item, "id": post_id, "url": url, "author": "@" + handle,
                      "published_at": published.isoformat(), "reading_kind": reading_kind})
        counts["eligible"] += 1
        counts[reading_kind + "_eligible"] += 1

    chosen: list[dict] = []
    used: set[str] = set()
    topic_counts: Counter = Counter()
    author_counts: Counter = Counter()
    reading_counts: Counter = Counter()

    def take(item: dict) -> bool:
        author = item["author"].casefold()
        topic = item["category"]
        if item["id"] in used or len(chosen) >= target_count:
            return False
        if topic_counts[topic] >= max_per_category or author_counts[author] >= max_per_author:
            return False
        if item["reading_kind"] == "classic" and reading_counts["classic"] >= max_classic_count:
            return False
        chosen.append(item)
        used.add(item["id"])
        topic_counts[topic] += 1
        author_counts[author] += 1
        reading_counts[item["reading_kind"]] += 1
        return True

    for reading_kind in ("recent", "classic"):
        pool = [item for item in valid if item["reading_kind"] == reading_kind]
        # Cover as-yet unrepresented topics before adding extra posts from the
        # same pool. Existing author/category counts also apply to classics.
        for topic in categories:
            if topic_counts[topic]:
                continue
            for item in pool:
                if item["category"] == topic and take(item):
                    break
        for item in pool:
            take(item)
    counts["selected"] = len(chosen)
    counts["recent_selected"] = reading_counts["recent"]
    counts["classic_selected"] = reading_counts["classic"]
    return chosen
