"""Bounded reserve of reviewed, unsent reading candidates.

This optional cache is never delivery authority. Its loss only reduces available
backup material; every loaded item is checked again before final selection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from .mail import build_reading_message
from .selection import canonical_post_url
from .xueqiu import canonical_article_url


MAX_ITEMS = 120
MAX_BYTES = 2 * 1024 * 1024
_FIELDS = frozenset({
    "source", "id", "url", "author", "author_id", "published_at", "verified_at", "discovered_at",
    "content_basis", "reading_kind", "title", "summary", "reason", "learning_points", "scores",
    "score_total", "evidence_excerpt", "content_form", "is_long_read", "category",
    "text_characters", "text_truncated", "estimated_read_minutes", "assessment_basis", "score_notice",
})
_FINANCE = {"价值投资", "企业经营", "财务与估值", "行业研究", "风险管理"}
_SCORE_LIMITS = {"depth": 30, "evidence": 25, "learning": 25, "readability": 10, "timeliness": 10}
_FINANCE_TITLE = re.compile(r"投资|价值|估值|财报|财务|经营|企业|公司|行业|现金流|盈利|资产|风险|金融|银行|保险|股息|invest|valuation|financial|business|cash.flow", re.I)
_EXCLUDED_TITLE = re.compile(r"辱华|支那|K线|短线|涨停|喊单|带单|稳赚|保证收益|加群|荐股", re.I)
_SENSITIVE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+|\bauthorization\s*:", re.I)


def _date(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError
    return parsed


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _safe_values(value):
    if isinstance(value, str):
        return len(value) <= 1000 and not any(ord(c) < 32 and c not in "\n\t" for c in value) and not _SENSITIVE.search(value)
    if isinstance(value, list):
        return len(value) <= 5 and all(_safe_values(v) for v in value)
    if isinstance(value, dict):
        return len(value) <= 5 and all(isinstance(k, str) and _safe_values(v) for k, v in value.items())
    return value is None or type(value) in {bool, int}


def _structural(item):
    """Validate stored provenance and rendering without making network calls."""
    if not isinstance(item, dict) or set(item) - _FIELDS or not all(_safe_values(v) for v in item.values()):
        raise ValueError
    source, kind = item.get("source"), item.get("reading_kind")
    if source == "X":
        url, post_id, author = canonical_post_url(item.get("url"))
        if (item.get("id") != "x:" + post_id or item.get("url") != url or not author
                or not isinstance(item.get("author"), str) or author.casefold() != item["author"].lstrip("@").casefold()):
            raise ValueError
    elif source == "雪球":
        url, author, article_id = canonical_article_url(item.get("url"))
        if item.get("id") != "xueqiu:" + article_id or item.get("url") != url or item.get("author_id") != author:
            raise ValueError
    else:
        raise ValueError
    if kind == "link_candidate":
        title = item.get("title")
        if (source != "雪球" or item.get("content_basis") != "search_metadata"
                or not isinstance(title, str) or not 6 <= len(title) <= 240
                or not _FINANCE_TITLE.search(title) or _EXCLUDED_TITLE.search(title)
                or set(item) & {"summary", "reason", "scores", "score_total", "author", "learning_points", "evidence_excerpt"}
                or item.get("published_at") not in {None, ""}):
            raise ValueError
        _date(item.get("discovered_at"))
    else:
        if kind not in {"recent", "classic", "undated"}:
            raise ValueError
        scores = item.get("scores")
        if (not isinstance(scores, dict) or set(scores) != set(_SCORE_LIMITS)
                or any(type(scores[k]) is not int or not 0 <= scores[k] <= maximum for k, maximum in _SCORE_LIMITS.items())
                or type(item.get("score_total")) is not int or item["score_total"] != sum(scores.values())
                or item["score_total"] < 75 or scores["depth"] < 18 or scores["learning"] < 15):
            raise ValueError
        if (not isinstance(item.get("evidence_excerpt"), str) or not 12 <= len(item["evidence_excerpt"]) <= 180
                or type(item.get("text_characters")) is not int
                or not (240 if source == "X" else 600) <= item["text_characters"] <= 10000
                or type(item.get("is_long_read")) is not bool
                or item.get("content_form") not in ({"long_post", "thread_excerpt"} if source == "X" else {"article", "article_excerpt"})
                or item.get("content_basis") != ("excerpt" if source == "X" else "original_text")):
            raise ValueError
        _date(item.get("verified_at"))
    build_reading_message([item], edition="daily", date="2026-01-01", sender="preview@example.invalid",
                          recipient="preview@example.invalid")
    return item


def _snapshot(item):
    return _date(item.get("discovered_at") if item.get("reading_kind") == "link_candidate" else item.get("verified_at"))


def _eligible(item, *, settings, seen, now):
    _structural(item)
    if item["id"] in seen:
        return "seen"
    snapshot = _snapshot(item)
    if snapshot > now + timedelta(minutes=5):
        return "invalid"
    source, kind = item["source"], item["reading_kind"]
    if kind == "link_candidate":
        if not settings["reading"].get("xueqiu_link_fallback", False):
            return "invalid"
        return "expired" if now - snapshot > timedelta(hours=48) else "accepted"
    category = item.get("category")
    allowed = settings["categories"] if source == "X" else _FINANCE
    if category not in allowed:
        return "invalid"
    if source == "X" and item["author"].lstrip("@").casefold() in {v.lstrip("@").casefold() for v in settings["blocked_accounts"]}:
        return "invalid"
    published = None if item.get("published_at") in (None, "") else _date(item["published_at"])
    if source == "X":
        _, post_id, _ = canonical_post_url(item["url"])
        real_date = datetime.fromtimestamp(((int(post_id) >> 22) + 1288834974657) / 1000, timezone.utc)
        if not published or abs((published - real_date).total_seconds()) > 1:
            return "invalid"
    if published and (published > now + timedelta(minutes=5) or published > snapshot + timedelta(minutes=5)):
        return "invalid"
    if kind == "recent":
        if not published:
            return "invalid"
        days = settings["max_age_days"] if source == "X" else settings["reading"]["xueqiu_max_age_days"]
        age = now - published
        # Never promote a stale news item to a classic simply because it aged.
        if age > timedelta(days=days) or now - snapshot > timedelta(days=days):
            return "expired"
        if source == "X" and category == "学生羊毛" and age > timedelta(hours=settings["max_offer_age_hours"]):
            return "expired"
    elif kind == "classic":
        if (not published or settings["max_classic_count"] == 0
                or category not in (settings["classic_categories"] if source == "X" else _FINANCE)):
            return "invalid"
        if now - snapshot > timedelta(days=30):
            return "expired"
    else:
        if published is not None or source != "雪球" or category not in _FINANCE or settings["max_classic_count"] == 0:
            return "invalid"
        if now - snapshot > timedelta(hours=48):
            return "expired"
    return "accepted"


def load_reserve(path, *, settings, seen, now):
    """Return valid backup candidates and fixed counters; bad cache is optional."""
    diagnostics = {"items_total": 0, "items_accepted": 0, "items_seen": 0, "items_expired": 0, "items_invalid": 0}
    try:
        _date(now.isoformat())
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if (not isinstance(data, dict) or set(data) != {"version", "saved_at", "items"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["items"], list) or len(data["items"]) > MAX_ITEMS):
            raise ValueError
        saved = _date(data["saved_at"])
        if saved > now + timedelta(minutes=5):
            raise ValueError
    except FileNotFoundError:
        diagnostics["missing_cache"] = 1
        return [], diagnostics
    except (ValueError, TypeError, AttributeError, OSError, UnicodeError, RecursionError):
        diagnostics["invalid_cache"] = 1
        return [], diagnostics
    accepted = {}
    for item in data["items"]:
        diagnostics["items_total"] += 1
        try:
            code = _eligible(item, settings=settings, seen=seen, now=now)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError):
            code = "invalid"
        if code != "accepted":
            diagnostics["items_" + code] += 1
            continue
        old = accepted.get(item["id"])
        if old is None or _snapshot(item) > _snapshot(old):
            accepted[item["id"]] = item
    result = sorted(accepted.values(), key=_snapshot, reverse=True)
    diagnostics["items_accepted"] = len(result)
    return result, diagnostics


def save_reserve(path, items, *, now):
    """Atomically store only bounded public review fields, never source bodies."""
    _date(now.isoformat())
    if not isinstance(items, list) or len(items) > 5000:
        raise ValueError("Invalid reserve candidates")
    accepted = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        public = {key: value for key, value in item.items() if key in _FIELDS}
        try:
            _structural(public)
            if _snapshot(public) > now + timedelta(minutes=5):
                continue
        except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError):
            continue
        old = accepted.get(public["id"])
        if old is None or _snapshot(public) > _snapshot(old):
            accepted[public["id"]] = public
    selected = sorted(accepted.values(), key=_snapshot, reverse=True)[:MAX_ITEMS]
    raw = (json.dumps({"version": 1, "saved_at": now.isoformat(), "items": selected}, ensure_ascii=False) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("Reserve candidates exceed size limit")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".reading-reserve-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
