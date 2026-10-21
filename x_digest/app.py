"""One run of the digest; fetching and sending are explicit operations."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import smtplib
import sys
from zoneinfo import ZoneInfo

from .mail import build_message, send_message
from .selection import canonical_post_url, select_posts
from .state import already_sent, load_state, record_delivery, seen_post_ids


class ConfigurationError(ValueError):
    pass


def _source_inventory(cited_urls, *, seen_ids, settings, now):
    counts = dict.fromkeys(('citation_recent', 'citation_old', 'citation_future',
                            'citation_seen', 'citation_blocked', 'citation_invalid'), 0)
    blocked = {handle.lstrip('@').casefold() for handle in settings.get('blocked_accounts', [])}
    urls, audit, used = [], [], set()
    for value in cited_urls:
        try:
            url, post_id, handle = canonical_post_url(value)
            if post_id in used:
                continue
            used.add(post_id)
            published = datetime.fromtimestamp(((int(post_id) >> 22) + 1288834974657) / 1000, timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            counts['citation_invalid'] += 1
            continue
        if post_id in seen_ids:
            status = 'seen'
        elif handle and handle.casefold() in blocked:
            status = 'blocked'
        elif published > now + timedelta(minutes=5):
            status = 'future'
        elif now - published > timedelta(days=settings.get('max_age_days', 7)):
            status = 'old'
        else:
            status = 'recent'
            urls.append(url)
        counts['citation_' + status] += 1
        audit.append({'url': url, 'published_at': published.isoformat(), 'status': status})
    return urls, counts, audit


def load_settings(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ConfigurationError("settings.json 必须包含配置对象")
    categories = value.get("categories")
    if not isinstance(categories, list) or not categories or any(not isinstance(c, str) or not c.strip() for c in categories):
        raise ConfigurationError("categories 必须是非空分类列表")
    if len(set(categories)) != len(categories):
        raise ConfigurationError("分类不能重复")
    for name in ("target_count", "max_per_category", "max_per_author", "history_days"):
        number = value.get(name)
        if type(number) is not int or not 1 <= number <= 100:
            raise ConfigurationError(f"{name} 必须是 1 到 100 的整数")
    for name in ("blocked_accounts", "editorial_preferences"):
        entries = value.get(name)
        if not isinstance(entries, list) or any(not isinstance(v, str) for v in entries):
            raise ConfigurationError(f"{name} 必须是字符串列表")
    classic_categories = value.get("classic_categories", [])
    classic_limit = value.get("max_classic_count", 0)
    if (not isinstance(classic_categories, list)
            or any(not isinstance(c, str) or c not in {"股市", "投资", "读书"} or c not in categories
                   for c in classic_categories)
            or len(set(classic_categories)) != len(classic_categories)
            or type(classic_limit) is not int or not 0 <= classic_limit <= 3
            or (classic_limit and not classic_categories)):
        raise ConfigurationError("经典阅读仅允许股市、投资、读书，数量必须为0到3条")
    ZoneInfo(value["timezone"])
    return value


def check_environment(*, send: bool) -> list[str]:
    required = ["DIGEST_API_BASE", "DIGEST_MODEL", "DIGEST_API_KEY", "SMTP_USERNAME"]
    if send:
        required.append("SMTP_AUTH_CODE")
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if not missing:
        from .provider import validate_configuration
        validate_configuration(base_url=os.environ["DIGEST_API_BASE"], api_key=os.environ["DIGEST_API_KEY"],
                               model=os.environ["DIGEST_MODEL"])
        sender = os.environ["SMTP_USERNAME"]
        # Reuse the mailer's strict header validation before a billed request.
        # This temporary message is never saved or sent.
        build_message([], sender=sender, recipient=os.environ.get("MAIL_TO") or sender,
                      digest_date="2000-01-01")
    return missing


@contextmanager
def delivery_lock(path: Path):
    """Serialize local Linux/macOS deliveries as well as Actions runs."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigurationError("另一个日报任务正在运行") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def run_digest(settings: dict, *, work_dir: Path, send: bool, now: datetime | None = None) -> dict:
    from .provider import ProviderError, SearchResult, search_by_category

    now = (now or datetime.now(ZoneInfo(settings["timezone"]))).astimezone(ZoneInfo(settings["timezone"]))
    date = now.date().isoformat()
    work_dir.mkdir(parents=True, exist_ok=True)
    with delivery_lock(work_dir / "run.lock"):
        state_path = work_dir / "state.json"
        state = load_state(state_path)
        if already_sent(state, date):
            return {"status": "already_sent", "date": date}
        missing = check_environment(send=send)
        if missing:
            raise ConfigurationError("缺少安全配置：" + ", ".join(missing))
        seen = seen_post_ids(state, today=now.date(), history_days=settings["history_days"])
        result = search_by_category(base_url=os.environ["DIGEST_API_BASE"], api_key=os.environ["DIGEST_API_KEY"],
                                    model=os.environ["DIGEST_MODEL"], settings=settings, seen_ids=seen, now=now)
        fresh_urls, inventory, audit = _source_inventory(result.cited_urls, seen_ids=seen,
                                                        settings=settings, now=now)
        (work_dir / 'retrieval-audit.json').write_text(json.dumps({'sources': audit}, ensure_ascii=False),
                                                     encoding='utf-8')
        diagnostics: dict[str, int] = {}
        def select_result(value, counts, *, classic_ids=None):
            return select_posts(value.posts, categories=settings["categories"], target_count=settings["target_count"],
                                max_per_category=settings["max_per_category"], max_per_author=settings["max_per_author"],
                                blocked_accounts=settings["blocked_accounts"], seen_ids=seen, cited_urls=value.cited_urls,
                                now=now, max_age_days=settings.get("max_age_days", 7),
                                max_offer_age_hours=settings.get("max_offer_age_hours", 72), diagnostics=counts,
                                classic_categories=settings.get("classic_categories", []),
                                max_classic_count=settings.get("max_classic_count", 0),
                                verified_classic_ids=classic_ids)
        posts = select_result(result, diagnostics)
        diagnostics.update(inventory)
        classic_urls = ([item['url'] for item in audit if item['status'] == 'old']
                        if settings.get('max_classic_count', 0) and len(posts) < settings['target_count'] else [])
        fresh_fallback_urls = fresh_urls if not posts else []
        if fresh_fallback_urls or classic_urls:
            from .oembed import fetch_verified_posts
            from .provider import curate_verified_posts
            verified = fetch_verified_posts(fresh_fallback_urls + classic_urls, now=now)
            diagnostics['oembed_verified_posts'] = len(verified)
            fresh_ids = {canonical_post_url(url)[1] for url in fresh_fallback_urls}
            old_ids = {canonical_post_url(url)[1] for url in classic_urls}
            editor_error = None
            for classic, allowed_ids in ((False, fresh_ids), (True, old_ids)):
                if classic and len(posts) >= settings['target_count']:
                    break
                source_posts = [post for post in verified if post['id'] in allowed_ids]
                diagnostics['classic_verified_posts' if classic else 'recent_verified_posts'] = len(source_posts)
                if not source_posts:
                    continue
                try:
                    kwargs = {'classic': True} if classic else {}
                    curated = curate_verified_posts(base_url=os.environ['DIGEST_API_BASE'],
                                                    api_key=os.environ['DIGEST_API_KEY'],
                                                    model=os.environ['DIGEST_MODEL'], settings=settings,
                                                    posts=source_posts, **kwargs)
                except ProviderError as exc:
                    editor_error = exc
                    diagnostics['classic_editor_failed' if classic else 'recent_editor_failed'] = 1
                    continue
                curated_counts: dict[str, int] = {}
                # Only the grounded editor's old candidates may use the classic
                # exemption. Never reintroduce unverified search-written summaries.
                combined = SearchResult([*posts, *curated.posts],
                                        [*(post['url'] for post in posts), *curated.cited_urls], {})
                posts = select_result(combined, curated_counts,
                                      classic_ids={post['id'] for post in source_posts} if classic else None)
                prefix = 'classic_' if classic else 'curated_'
                diagnostics.update({prefix + key: value for key, value in curated_counts.items()})
                result.usage = {field: sum(value for value in (result.usage.get(field), curated.usage.get(field))
                                          if type(value) is int and value >= 0)
                                for field in ('input_tokens', 'output_tokens', 'total_tokens')}
            if not posts and editor_error is not None:
                raise editor_error
        if not posts:
            raise ConfigurationError("没有通过来源、时效和去重检查的帖子；本次停止发信；筛选统计："
                                     + json.dumps(diagnostics, separators=(",", ":")))
        sender = os.environ["SMTP_USERNAME"]
        recipient = os.environ.get("MAIL_TO") or sender
        message = build_message(posts, recipient=recipient, sender=sender, digest_date=date,
                                requested_count=settings["target_count"])
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        preview = work_dir / "preview"
        preview.mkdir(exist_ok=True)
        (preview / "digest.eml").write_bytes(message.as_bytes())
        (preview / "digest.html").write_text(message.get_body(preferencelist=("html",)).get_content(), encoding="utf-8")
        (preview / "digest.json").write_text(json.dumps({"date": date, "posts": posts, "usage": result.usage},
                                                       ensure_ascii=False, indent=2), encoding="utf-8")
        if send:
            send_message(message, username=sender, password=os.environ["SMTP_AUTH_CODE"])
            try:
                record_delivery(state_path, state, date=date, post_ids=[p["id"] for p in posts],
                                sent_at=now.isoformat(), history_days=settings["history_days"])
            except (ValueError, OSError) as exc:
                raise ConfigurationError("SMTP已接受邮件，但发送记录保存失败；重跑前请先核对收件箱") from exc
        classic_count = sum(post.get('reading_kind') == 'classic' for post in posts)
        return {"status": "smtp_accepted" if send else "preview", "date": date, "count": len(posts),
                "recent_count": len(posts) - classic_count, "classic_count": classic_count}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X 帖子每日精选：检查配置、生成预览或发送邮件")
    parser.add_argument("--settings", type=Path, default=Path("settings.json"))
    parser.add_argument("--work-dir", type=Path, default=Path(".local"))
    parser.add_argument("--check", action="store_true", help="只检查配置，不联网、不发信")
    parser.add_argument("--fetch", action="store_true", help="按领域检索（可能计费）并生成预览")
    parser.add_argument("--send", action="store_true", help="与 --fetch 一起使用，向配置的邮箱发送日报")
    args = parser.parse_args(argv)
    if args.send and not (args.fetch or args.check):
        parser.error("--send 需要搭配 --fetch 或 --check")
    if args.check and args.fetch:
        parser.error("--check 与 --fetch 不能同时使用")
    if not args.check and not args.fetch:
        parser.print_help()
        return 0
    try:
        settings = load_settings(args.settings)
        if args.check:
            missing = check_environment(send=args.send)
            if missing:
                print("缺少配置：" + ", ".join(missing), file=sys.stderr)
                return 2
            print("本地配置检查通过；API搜索和SMTP尚需实际验证。")
            return 0
        result = run_digest(settings, work_dir=args.work_dir, send=args.send)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except smtplib.SMTPException:
        print("SMTP投递失败；请检查QQ邮箱授权与服务状态。", file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError) as exc:
        from .provider import ProviderError
        if isinstance(exc, (ConfigurationError, ProviderError)):
            print(str(exc), file=sys.stderr)
        else:
            print("配置、网络或本地文件操作失败；请检查参数与服务可用性。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
