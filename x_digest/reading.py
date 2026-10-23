"""Daily reading and weekly learning editions, sharing successful-delivery history."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, make_msgid
from html import escape
import json
import os
from pathlib import Path
import re
import smtplib
from zoneinfo import ZoneInfo

from .app import ConfigurationError, check_environment, delivery_lock, reading_search_model
from . import editorial, reading_state
from .discovery import discover_xueqiu
from .discovery import XueqiuDiscoveryResult
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


def _search_credentials():
    return {**_credentials(), "model": reading_search_model()}


def _editor_code(error):
    code = getattr(error, "code", "invalid_output")
    return code if isinstance(code, str) and code in editorial.EDITORIAL_ERROR_CODES else "invalid_output"


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
    ordered = sorted(items, key=lambda p: (p.get("reading_kind") != "recent", p.get("from_reserve", False),
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


def add_xueqiu_links(items, *, candidates, outcomes, seen, settings, candidate_limit=None):
    """User-approved title/link fallback; never imply a full-article assessment."""
    if not settings["reading"].get("xueqiu_link_fallback", False):
        return items
    used = {item["id"] for item in items} | seen
    authors = {item.get("author_id") for item in items if item["source"] == "雪球"}
    # Metadata-only links are a separately labelled discovery list, not
    # reviewed classic articles. They must not consume the three-classic cap.
    slots = (candidate_limit or settings["reading"]["xueqiu_target"]) - sum(p["source"] == "雪球" for p in items)
    result = list(items)
    finance = re.compile(r"投资|价值|估值|财报|财务|经营|企业|公司|行业|现金流|盈利|资产|风险|金融|银行|保险|股息|"
                         r"invest|valuation|financial|business|cash.flow", re.I)
    excluded = re.compile(r"辱华|支那|K线|短线|涨停|喊单|带单|稳赚|保证收益|加群|荐股", re.I)
    unavailable = {"access_challenge", "unverified_page", "access_denied", "redirect_rejected",
                   "connection_failed", "rate_limited", "http_error"}
    for candidate in candidates:
        if slots <= 0:
            break
        try:
            url, author_id, article_id = canonical_article_url(candidate["url"])
            item_id, title = "xueqiu:" + article_id, candidate["title"]
            if (candidate.get("id") != item_id or candidate.get("discovery_basis") != "search_metadata"
                    or not isinstance(title, str) or not 6 <= len(title.strip()) <= 300
                    or not finance.search(title) or excluded.search(title)
                    or item_id in used or author_id in authors or outcomes.get(url) not in unavailable):
                continue
            result.append({"id": item_id, "url": url, "source": "雪球", "title": title.strip()[:240],
                           "author_id": author_id, "content_basis": "search_metadata",
                           "reading_kind": "link_candidate", "published_at": "",
                           "discovered_at": candidate.get("discovered_at", "")})
            used.add(item_id)
            authors.add(author_id)
            slots -= 1
        except (KeyError, ValueError, TypeError):
            continue
    return result


def _daily(settings, seen, now):
    credentials = _credentials()
    search_credentials = _search_credentials()
    diagnostics, candidates = {}, []
    source_status = {"X": "no_candidates", "雪球": "no_candidates"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        x_future = executor.submit(search_by_category, **search_credentials, settings=settings,
                                  seen_ids={v[2:] for v in seen if v.startswith("x:")}, now=now)
        snow_future = executor.submit(discover_xueqiu, **search_credentials, now=now, seen_ids=seen)
        try:
            search_result = x_future.result()
            urls = search_result.cited_urls
            diagnostics["x_search"] = getattr(search_result, "diagnostics", {})
            diagnostics["x_discovered"] = len(urls)
            # Date/blocked/seen filters also run before downloading excerpt bodies.
            from .app import _source_inventory
            _, inventory, audit = _source_inventory(urls, seen_ids={v[2:] for v in seen if v.startswith("x:")},
                                                   settings=settings, now=now)
            diagnostics.update(inventory)
            diagnostics["x_source_inventory"] = audit[:100]
            eligible = [row["url"] for row in audit if row["status"] in
                        ({"recent", "old"} if settings["max_classic_count"] else {"recent"})]
            # Recent sources get the bounded reader's slots first.
            eligible.sort(key=lambda url: next(row["status"] != "recent" for row in audit if row["url"] == url))
            verified_x = fetch_verified_posts(eligible, now=now, limit=20) if eligible else []
            diagnostics["x_verified"] = len(verified_x)
            diagnostics["x_verified_candidates"] = [{"id": item["id"], "published_at": item.get("published_at"),
                "text_characters": len(" ".join(item.get("text", "").split()))} for item in verified_x]
        except ProviderError as error:
            verified_x = []
            source_status["X"] = "unavailable"
            diagnostics["x_search_failed"] = 1
            diagnostics["x_search"] = getattr(error, "diagnostics", {})
            # search_by_category emits only fixed aggregate labels here. Keep
            # those counts for diagnosis without copying an exception message.
            try:
                failures = json.loads(str(error).partition("诊断=")[2])
                allowed = {"no_sources", "not_json", "incomplete", "no_tool", "invalid_posts", "network", "timeout", "other",
                           "HTTP400", "HTTP401", "HTTP403", "HTTP404", "HTTP408", "HTTP422", "HTTP429",
                           "HTTP500", "HTTP502", "HTTP503", "HTTP504"}
                if (isinstance(failures, dict) and set(failures) <= allowed
                        and all(type(n) is int and 0 <= n <= len(settings["categories"]) for n in failures.values())):
                    diagnostics["x_search_failures"] = failures
            except (ValueError, TypeError):
                pass
        try:
            snow = snow_future.result()
        except ProviderError:
            snow = XueqiuDiscoveryResult([], {"searches_completed": 0, "source_failed": 1})
        diagnostics["xueqiu_discovery"] = snow.diagnostics
        fetched = fetch_articles(snow.urls, now=now) if snow.urls else None
        verified_snow = fetched.articles if fetched else []
        diagnostics["xueqiu_fetch"] = fetched.diagnostics if fetched else {}
        if (snow.urls and not verified_snow) or (not snow.urls and not (
                snow.diagnostics.get("searches_completed", 0) or snow.diagnostics.get("feed_feeds_completed", 0))):
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
                review = {}
                candidates.extend(editorial.curate_articles(**credentials, items=pool, now=now, source=source,
                    allowed_categories=(settings["classic_categories"] if source == "X" and classic else categories),
                    target_count=min(3, target) if classic else target, classic=classic, diagnostics=review))
                editor_succeeded = True
            except ProviderError as error:
                editor_failed = True
                diagnostics[("x" if source == "X" else "xueqiu") + "_editor_failed"] = 1
                diagnostics[("x" if source == "X" else "xueqiu") + "_editor_error"] = _editor_code(error)
            finally:
                diagnostics.setdefault("article_reviews", []).append({"source": source, "classic": classic,
                                                                      **review})
        if editor_failed and not editor_succeeded:
            source_status[source] = "unavailable"
        elif verified and source_status[source] != "unavailable":
            source_status[source] = "no_qualified"
    selected = select_reading(candidates, settings=settings, now=now)
    selected = add_xueqiu_links(selected, candidates=getattr(snow, "candidates", []),
                               outcomes=getattr(fetched, "outcomes", {}), seen=seen, settings=settings)
    # Internal candidate hand-off. run_reading removes this before persisting
    # the public diagnostic report. Surplus vetted material can survive a
    # temporary search outage on a later run.
    diagnostics["_reserve_items"] = candidates + add_xueqiu_links([], candidates=getattr(snow, "candidates", []),
        outcomes=getattr(fetched, "outcomes", {}), seen=seen, settings=settings, candidate_limit=12)
    counts = Counter(p["source"] for p in selected)
    targets = {"X": settings["target_count"], "雪球": settings["reading"]["xueqiu_target"]}
    for source, count in counts.items():
        source_status[source] = "available" if count >= targets[source] else "partial"
    if any(p.get("reading_kind") == "link_candidate" for p in selected):
        diagnostics["xueqiu_link_candidates"] = sum(p.get("reading_kind") == "link_candidate" for p in selected)
        source_status["雪球"] = "links_only" if all(p.get("reading_kind") == "link_candidate"
                                                   for p in selected if p["source"] == "雪球") else "partial"
    return selected, {"targets": targets, "source_status": source_status}, diagnostics


def merge_reserve(current, stored, *, settings, now):
    """Fill source quotas from already-vetted, freshly revalidated snapshots."""
    pool = list(current)
    used = {item["id"] for item in pool}
    pool.extend({**item, "from_reserve": True} for item in stored if item["id"] not in used)
    selected = select_reading([item for item in pool if item.get("reading_kind") != "link_candidate"],
                              settings=settings, now=now)
    used = {item["id"] for item in selected}
    authors = {item.get("author_id") for item in selected if item["source"] == "雪球"}
    for item in pool:
        if (item.get("reading_kind") != "link_candidate" or item["id"] in used
                or item.get("author_id") in authors
                or sum(value["source"] == "雪球" for value in selected) >= settings["reading"]["xueqiu_target"]):
            continue
        selected.append(item)
        used.add(item["id"])
        authors.add(item.get("author_id"))
    return selected, pool


def _weekly(settings, seen, now):
    fetched = fetch_projects(now=now, limit=20, token=os.environ.get("GH_TOKEN") or None,
                             seen_ids={v for v in seen if v.startswith("github:")})
    projects = [p for p in fetched.projects if p["id"].casefold() not in {i.casefold() for i in seen}]
    # Independent short reviews avoid one response containing ten long plans.
    owner_candidates, eligible = Counter(), []
    for project in projects:
        owner = project["author"].casefold()
        if owner_candidates[owner] < 2:
            eligible.append(project)
            owner_candidates[owner] += 1
    pools = ([eligible] if len(eligible) <= 5 else [eligible[::2], eligible[1::2]]) if eligible else []
    selected, failures, reviews = [], 0, []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for pool in pools:
            review = {}
            reviews.append(review)
            futures.append(executor.submit(editorial.curate_projects, **_credentials(), items=pool,
                                           target_count=5, now=now, diagnostics=review))
        for future in futures:
            try:
                selected.extend(future.result())
            except ProviderError as error:
                failures += 1
                fetched.diagnostics["editor_failed"] = failures
                fetched.diagnostics["editor_error"] = _editor_code(error)
                field = getattr(error, "field", "")
                if field in editorial.EDITORIAL_ERROR_FIELDS:
                    fetched.diagnostics["editor_field"] = field
    fetched.diagnostics["editor_reviews"] = reviews
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
    if failures and not chosen:
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
        def local_history():
            return reading_state.load_state(state_path, legacy_path=work_dir / "state.json")
        # A deliberate preview may inspect today's live pipeline after a demo
        # was delivered. Sending remains strictly once per edition/period.
        ledger = None
        if send and os.environ.get("DIGEST_REQUIRE_LEDGER") == "true":
            from .delivery_ledger import open_ledger
            ledger = open_ledger(local_history, allow_create=False)
            state = ledger.history
        else:
            state = local_history()
        if send and reading_state.already_sent(state, edition=edition, period=period):
            if ledger:
                # Repair an absent/stale runner cache from the authoritative
                # ledger even when no new delivery is needed.
                reading_state.save_state(state_path, state)
            return {"status": "already_sent", "edition": edition, "period": period}
        if ledger:
            ledger.check_available(edition=edition, period=period)
        missing = check_environment(send=send, reading=True)
        if missing:
            raise ConfigurationError("缺少安全配置：" + ", ".join(missing))
        seen = reading_state.seen_ids(state, today=now.date(), history_days=settings["reading"]["history_days"])
        if ledger:
            seen |= ledger.pending_ids(today=now.date(), history_days=settings["reading"]["history_days"])
        items, coverage, diagnostics = (_daily if edition == "daily" else _weekly)(settings, seen, now)
        if edition == "daily":
            from .reading_reserve import load_reserve, save_reserve
            stored, reserve_diagnostics = load_reserve(work_dir / "reading-reserve.json", settings=settings,
                                                       seen=seen, now=now)
            fresh_pool = diagnostics.pop("_reserve_items", items)
            items, reserve_pool = merge_reserve(fresh_pool, stored, settings=settings, now=now)
            reserve_diagnostics["used"] = sum(item.get("from_reserve", False) for item in items)
            try:
                save_reserve(work_dir / "reading-reserve.json", reserve_pool, now=now)
            except (OSError, ValueError):
                reserve_diagnostics["save_failed"] = 1
            diagnostics["reserve"] = reserve_diagnostics
            for source, target in coverage["targets"].items():
                chosen = [item for item in items if item["source"] == source]
                if chosen:
                    coverage["source_status"][source] = ("links_only" if all(
                        item.get("reading_kind") == "link_candidate" for item in chosen) else
                        ("available" if len(chosen) >= target else "partial"))
        (work_dir / "reading-audit.json").write_text(json.dumps({"edition": edition, "coverage": coverage,
            "diagnostics": diagnostics}, ensure_ascii=False, indent=2), encoding="utf-8")
        if not items:
            report = {"status": "no_material", "edition": edition, "date": date, "count": 0,
                      "source_counts": {}, "coverage": coverage, "mail_sent": False}
            (work_dir / "status-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
            preview = work_dir / "preview"
            preview.mkdir(exist_ok=True)
            (preview / "digest.html").unlink(missing_ok=True)
            (preview / "digest.json").unlink(missing_ok=True)
            labels = {"unavailable": "来源暂不可用", "no_candidates": "尚未找到候选",
                      "no_qualified": "尚无通过筛选的内容"}
            sources = "".join("<li>" + escape(source) + "：" + escape(labels.get(status, status)) + "</li>"
                              for source, status in coverage["source_status"].items())
            (preview / "status.html").write_text(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>阅读任务状态</title>'
                '<h1>本期暂未取得合格材料</h1><p>尚未发送邮件，后续补跑仍可继续。</p><ul>'
                + sources + '</ul></html>', encoding="utf-8")
            return report
        (work_dir / "status-report.json").unlink(missing_ok=True)
        (work_dir / "preview/status.html").unlink(missing_ok=True)
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
            if ledger:
                ledger.reserve(edition=edition, period=period, date=date, item_ids=[item["id"] for item in items],
                               sent_at=now.isoformat())
            try:
                send_message(message, username=sender, password=os.environ["SMTP_AUTH_CODE"])
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError, smtplib.SMTPSenderRefused,
                    smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
                # These replies explicitly reject delivery. Disconnects and
                # timeouts can happen after DATA acceptance: keep their intent.
                if ledger:
                    ledger.cancel()
                raise
            try:
                saved = reading_state.record_delivery(state_path, state, edition=edition, period=period, date=date,
                    item_ids=[item["id"] for item in items], sent_at=now.isoformat(), history_days=180)
                if ledger:
                    ledger.complete(saved)
            except (ValueError, OSError) as exc:
                raise ConfigurationError("SMTP已接受邮件，但发送记录保存失败；重跑前请先核对收件箱") from exc
        return {"status": "smtp_accepted" if send else "preview", "edition": edition, "date": date,
                "count": len(items), "source_counts": dict(Counter(item["source"] for item in items)),
                "classic_count": sum(item.get("reading_kind") == "classic" for item in items),
                "link_candidate_count": sum(item.get("reading_kind") == "link_candidate" for item in items)}
