"""One run of the digest; fetching and sending are explicit operations."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import smtplib
import sys
from zoneinfo import ZoneInfo

from .mail import build_message, send_message
from .selection import select_posts
from .state import already_sent, load_state, record_delivery, seen_post_ids


class ConfigurationError(ValueError):
    pass


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
    from .provider import search

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
        result = search(base_url=os.environ["DIGEST_API_BASE"], api_key=os.environ["DIGEST_API_KEY"],
                        model=os.environ["DIGEST_MODEL"], settings=settings, seen_ids=seen, now=now)
        posts = select_posts(result.posts, categories=settings["categories"], target_count=settings["target_count"],
                             max_per_category=settings["max_per_category"], max_per_author=settings["max_per_author"],
                             blocked_accounts=settings["blocked_accounts"], seen_ids=seen, cited_urls=result.cited_urls,
                             now=now, max_age_days=settings.get("max_age_days", 7),
                             max_offer_age_hours=settings.get("max_offer_age_hours", 72))
        if not posts:
            raise ConfigurationError("没有通过来源、时效和去重检查的帖子；本次停止发信")
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
        return {"status": "smtp_accepted" if send else "preview", "date": date, "count": len(posts)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X 帖子每日精选：检查配置、生成预览或发送邮件")
    parser.add_argument("--settings", type=Path, default=Path("settings.json"))
    parser.add_argument("--work-dir", type=Path, default=Path(".local"))
    parser.add_argument("--check", action="store_true", help="只检查配置，不联网、不发信")
    parser.add_argument("--fetch", action="store_true", help="执行一次搜索API请求（可能计费）并生成预览")
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
