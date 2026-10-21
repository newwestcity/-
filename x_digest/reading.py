"""Daily reading and weekly learning editions, sharing successful-delivery history."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .app import ConfigurationError, check_environment, delivery_lock
from . import editorial, reading_state
from .discovery import discover_xueqiu
from .github_projects import fetch_projects
from .mail import build_reading_message, send_message
from .oembed import fetch_verified_posts
from .provider import ProviderError, search_by_category
from .selection import canonical_post_url
from .xueqiu import canonical_article_url, fetch_articles


FINANCE_CATEGORIES = ["价值投资", "企业经营", "财务与估值", "行业研究", "风险管理"]


def _credentials():
    return {"base_url": os.environ["DIGEST_API_BASE"], "api_key": os.environ["DIGEST_API_KEY"],
            "model": os.environ["DIGEST_MODEL"]}


def _editor_code(error):
    code = getattr(error, "code", "invalid_output")
    return code if code in editorial.EDITORIAL_ERROR_CODES else "invalid_output"


def _published(item):
    if not item.get("published_at"):
        return None
    parsed = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Article date requires timezone")
    return parsed


def prepare_articles(items, *, source, seen, settings, now):
    """Apply identity, seen and age policies before any paid editing."""
    recent, timeless, used = [], [], set()
    blocked = {v.lstrip("@").casefold() for v in settings["blocked_accounts"]}
    for original in items:
        try:
            item = dict(original)
            if source == "X":
                url, post_id, author = canonical_post_url(item["url"])
                if item["id"] not in {post_id, "x:" + post_id} or not author:
                    continue
                if author.casefold() in blocked or author.casefold() != item["author"].lstrip("@").casefold():
                    continue
                item["id"], item["url"] = "x:" + post_id, url
                date = datetime.fromtimestamp(((int(post_id) >> 22) + 1288834974657) / 1000, timezone.utc)
                item["published_at"] = date.isoformat()
                item["content_basis"] = "excerpt"
                max_age = settings.get("max_age_days", 7)
            else:
                url, author_id, article_id = canonical_article_url(item["url"])
                if item["id"] != "xueqiu:" + article_id:
                    continue
                item["url"], item["author_id"] = url, author_id
                date = _published(item)
                max_age = settings["reading"]["xueqiu_max_age_days"]
            if item["id"] in seen or item["id"] in used or (date and date > now + timedelta(minutes=5)):
                continue
            item["source"] = source
            used.add(item["id"])
            if date and now - date <= timedelta(days=max_age):
                recent.append(item)
            elif settings["max_classic_count"]:
                timeless.append(item)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            continue
    return recent, timeless


def select_reading(items, *, settings, now):
    """Use transparent scores, source quotas, diversity and one shared classic cap."""
    targets = {"X": settings["target_count"], "雪球": settings["reading"]["xueqiu_target"]}
    counts, authors, categories = Counter(), Counter(), Counter()
    selected, used, exceptions, undated = [], set(), 0, 0
    # Fill recent material first; classics/unknown dates can never displace it.
    ordered = sorted(items, key=lambda p: (p.get("reading_kind") != "recent",
                     not p.get("is_long_read", False), -p.get("score_total", 0), p["id"]))
    # Round-robin categories within each source prevents the largest search from
    # occupying the whole edition. Subsequent rounds fill available capacity.
    ordered = sorted(enumerate(ordered), key=lambda pair: (
        pair[1].get("reading_kind") != "recent",
        sum(1 for earlier in ordered[:pair[0]] if earlier.get("source") == pair[1].get("source")
            and earlier.get("category") == pair[1].get("category")), pair[0]))
    for _, value in ordered:
        item = dict(value)
        source = item.get("source")
        if source not in targets or counts[source] >= targets[source] or item["id"] in used:
            continue
        if item.get("score_total", 0) < 75:
            continue
        author_key = (source, str(item.get("author_id") or item.get("author", "")).casefold())
        author_limit = settings["max_per_author"] if source == "X" else 1
        if authors[author_key] >= author_limit:
            continue
        category_key = (source, item.get("category"))
        if source == "X" and categories[category_key] >= settings["max_per_category"]:
            continue
        date = _published(item)
        age = now - date if date else None
        if source == "X" and item.get("category") == "学生羊毛":
            if not date or age > timedelta(hours=settings.get("max_offer_age_hours", 72)):
                continue
        max_age = settings.get("max_age_days", 7) if source == "X" else settings["reading"]["xueqiu_max_age_days"]
        kind = "undated" if date is None else ("classic" if age > timedelta(days=max_age) else "recent")
        if date and date > now + timedelta(minutes=5):
            continue
        if kind != "recent":
            allowed = settings["classic_categories"] if source == "X" else FINANCE_CATEGORIES
            if item.get("category") not in allowed or exceptions >= settings["max_classic_count"]:
                continue
            if kind == "undated" and undated >= 1:
                continue
            exceptions += 1
            undated += kind == "undated"
        item["reading_kind"] = kind
        selected.append(item)
        counts[source] += 1
        authors[author_key] += 1
        categories[category_key] += 1
        used.add(item["id"])
    return selected


def _daily(settings, seen, now):
    credentials = _credentials()
    diagnostics, candidates = {}, []
    source_status = {"X": "no_candidates", "雪球": "no_candidates"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        x_future = executor.submit(search_by_category, **credentials, settings=settings,
                                  seen_ids={v[2:] for v in seen if v.startswith("x:")}, now=now)
        snow_future = executor.submit(discover_xueqiu, **credentials, now=now, seen_ids=seen)
        try:
            urls = x_future.result().cited_urls
            diagnostics["x_discovered"] = len(urls)
            # Date/blocked/seen filters also run before downloading excerpt bodies.
            from .app import _source_inventory
            _, inventory, audit = _source_inventory(urls, seen_ids={v[2:] for v in seen if v.startswith("x:")},
                                                   settings=settings, now=now)
            diagnostics.update(inventory)
            eligible = [row["url"] for row in audit if row["status"] in
                        ({"recent", "old"} if settings["max_classic_count"] else {"recent"})]
            # Recent sources get the bounded reader's slots first.
            eligible.sort(key=lambda url: next(row["status"] != "recent" for row in audit if row["url"] == url))
            verified_x = fetch_verified_posts(eligible, now=now, limit=20) if eligible else []
            diagnostics["x_verified"] = len(verified_x)
        except ProviderError:
            verified_x = []
            source_status["X"] = "unavailable"
            diagnostics["x_search_failed"] = 1
        snow = snow_future.result()
        diagnostics["xueqiu_discovery"] = snow.diagnostics
        fetched = fetch_articles(snow.urls, now=now) if snow.urls else None
        verified_snow = fetched.articles if fetched else []
        diagnostics["xueqiu_fetch"] = fetched.diagnostics if fetched else {}
        if (snow.urls and not verified_snow) or (not snow.urls and not snow.diagnostics.get("searches_completed", 0)):
            source_status["雪球"] = "unavailable"
    for source, verified in (("X", verified_x), ("雪球", verified_snow)):
        recent, timeless = prepare_articles(verified, source=source, seen=seen, settings=settings, now=now)
        target = settings["target_count"] if source == "X" else settings["reading"]["xueqiu_target"]
        categories = settings["categories"] if source == "X" else FINANCE_CATEGORIES
        editor_succeeded, editor_failed = False, False
        for classic, pool in ((False, recent), (True, timeless)):
            if not pool:
                continue
            try:
                candidates.extend(editorial.curate_articles(**credentials, items=pool, now=now, source=source,
                    allowed_categories=(settings["classic_categories"] if source == "X" and classic else categories),
                    target_count=min(3, target) if classic else target, classic=classic))
                editor_succeeded = True
            except ProviderError as error:
                editor_failed = True
                diagnostics[("x" if source == "X" else "xueqiu") + "_editor_failed"] = 1
                diagnostics[("x" if source == "X" else "xueqiu") + "_editor_error"] = _editor_code(error)
        if editor_failed and not editor_succeeded:
            source_status[source] = "unavailable"
        elif verified and source_status[source] != "unavailable":
            source_status[source] = "no_qualified"
    selected = select_reading(candidates, settings=settings, now=now)
    counts = Counter(p["source"] for p in selected)
    targets = {"X": settings["target_count"], "雪球": settings["reading"]["xueqiu_target"]}
    for source, count in counts.items():
        source_status[source] = "available" if count >= targets[source] else "partial"
    return selected, {"targets": targets, "source_status": source_status}, diagnostics


def _weekly(settings, seen, now):
    fetched = fetch_projects(now=now, limit=20, token=os.environ.get("GH_TOKEN") or None,
                             seen_ids={v for v in seen if v.startswith("github:")})
    projects = [p for p in fetched.projects if p["id"].casefold() not in {i.casefold() for i in seen}]
    editor_failed = False
    try:
        selected = editorial.curate_projects(**_credentials(), items=projects, target_count=10, now=now) if projects else []
    except ProviderError as error:
        selected, editor_failed = [], True
        fetched.diagnostics["editor_failed"] = 1
        fetched.diagnostics["editor_error"] = _editor_code(error)
    # Limit a publisher to two recommendations; identity is collector-owned.
    owners, chosen = Counter(), []
    for project in sorted(selected, key=lambda p: -p["score_total"]):
        owner = project["author"].casefold()
        if owners[owner] < 2 and len(chosen) < 10:
            chosen.append({**project, "id": project["id"].casefold()})
            owners[owner] += 1
    status = "available" if len(chosen) == 10 else ("partial" if chosen else "no_qualified")
    if not fetched.projects:
        status = "unavailable" if not fetched.diagnostics.get("searches_succeeded") else "no_candidates"
    if editor_failed:
        status = "unavailable"
    return chosen, {"targets": {"GitHub": 10}, "source_status": {"GitHub": status}}, fetched.diagnostics


def run_reading(settings: dict, *, work_dir: Path, send: bool, edition="daily", now=None):
    if edition not in {"daily", "weekly"}:
        raise ConfigurationError("阅读版次必须为daily或weekly")
    now = (now or datetime.now(ZoneInfo(settings["timezone"]))).astimezone(ZoneInfo(settings["timezone"]))
    date = now.date().isoformat()
    iso = now.date().isocalendar()
    period = date if edition == "daily" else f"{iso.year}-W{iso.week:02d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    with delivery_lock(work_dir / "run.lock"):
        state_path = work_dir / "reading-state.json"
        state = reading_state.load_state(state_path, legacy_path=work_dir / "state.json")
        if reading_state.already_sent(state, edition=edition, period=period):
            return {"status": "already_sent", "edition": edition, "period": period}
        missing = check_environment(send=send)
        if missing:
            raise ConfigurationError("缺少安全配置：" + ", ".join(missing))
        seen = reading_state.seen_ids(state, today=now.date(), history_days=settings["reading"]["history_days"])
        items, coverage, diagnostics = (_daily if edition == "daily" else _weekly)(settings, seen, now)
        (work_dir / "reading-audit.json").write_text(json.dumps({"edition": edition, "coverage": coverage,
            "diagnostics": diagnostics}, ensure_ascii=False, indent=2), encoding="utf-8")
        if not items:
            raise ConfigurationError("本期没有通过原文与质量检查的阅读材料；未发空邮件。诊断="
                                     + json.dumps(diagnostics, separators=(",", ":")))
        sender = os.environ["SMTP_USERNAME"]
        message = build_reading_message(items, edition=edition, date=date, sender=sender,
                                        recipient=os.environ.get("MAIL_TO") or sender, coverage=coverage)
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        preview = work_dir / "preview"
        preview.mkdir(exist_ok=True)
        (preview / "digest.html").write_text(message.get_body(preferencelist=("html",)).get_content(), encoding="utf-8")
        # Keep a reviewable record, never a credential-bearing .eml artifact.
        (preview / "digest.json").write_text(json.dumps({"date": date, "edition": edition, "items": items,
            "coverage": coverage}, ensure_ascii=False, indent=2), encoding="utf-8")
        if send:
            send_message(message, username=sender, password=os.environ["SMTP_AUTH_CODE"])
            try:
                reading_state.record_delivery(state_path, state, edition=edition, period=period, date=date,
                    item_ids=[item["id"] for item in items], sent_at=now.isoformat(), history_days=180)
            except (ValueError, OSError) as exc:
                raise ConfigurationError("SMTP已接受邮件，但发送记录保存失败；重跑前请先核对收件箱") from exc
        return {"status": "smtp_accepted" if send else "preview", "edition": edition, "date": date,
                "count": len(items), "source_counts": dict(Counter(item["source"] for item in items)),
                "classic_count": sum(item.get("reading_kind") == "classic" for item in items)}
