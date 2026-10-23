"""Explicitly send a vetted repair example without consuming the daily slot.

The caller verifies the downloaded preview artifact's provenance. This command
only revalidates its public candidate snapshot; it never searches or edits.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import smtplib
import sys
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from x_digest.app import ConfigurationError, delivery_lock, load_settings
from x_digest.delivery_ledger import LedgerError, open_ledger
from x_digest.mail import build_reading_message, send_message
from x_digest.reading import merge_reserve
from x_digest.reading_reserve import load_reserve
from x_digest import reading_state


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ConfigurationError("修复验收命令参数无效")


def send_example(source_dir: Path, work_dir: Path, now=None):
    """Send new validated snapshot items once using the independent example slot."""
    settings = load_settings(Path(__file__).resolve().parents[1] / "settings.json")
    if now is None:
        now = datetime.now(ZoneInfo(settings["timezone"]))
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ConfigurationError("修复验收时间必须包含时区")
    now = now.astimezone(ZoneInfo(settings["timezone"]))
    date = now.date().isoformat()
    source_dir, work_dir = Path(source_dir), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    with delivery_lock(work_dir / "run.lock"):
        state_path = work_dir / "reading-state.json"
        ledger = open_ledger(lambda: reading_state.load_state(state_path), allow_create=False)
        history = ledger.history
        if reading_state.already_sent(history, edition="example", period=date):
            return {"status": "already_sent", "count": 0, "source_counts": {}, "date": date}
        ledger.check_available(edition="example", period=date)
        seen = reading_state.seen_ids(history, today=now.date(), history_days=settings["reading"]["history_days"])
        seen |= ledger.pending_ids(today=now.date(), history_days=settings["reading"]["history_days"])
        snapshot, _ = load_reserve(source_dir / "reading-reserve.json", settings=settings, seen=seen, now=now)
        items, _ = merge_reserve([], snapshot, settings=settings, now=now)
        if not items:
            raise ConfigurationError("修复验收没有合格且未发送的候选，未发信")
        sender = os.environ.get("SMTP_USERNAME", "")
        password = os.environ.get("SMTP_AUTH_CODE", "")
        if not sender.strip() or not password.strip() or any(char in password for char in "\r\n"):
            raise ConfigurationError("修复验收缺少有效的SMTP配置，未发信")
        message = build_reading_message(items, edition="daily", date=date, sender=sender,
                                        recipient=os.environ.get("MAIL_TO") or sender)
        message.replace_header("Subject", "修复验收｜" + str(message["Subject"]))
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        preview = work_dir / "preview"
        preview.mkdir(exist_ok=True)
        (preview / "digest.html").write_text(message.get_body(preferencelist=("html",)).get_content(),
                                            encoding="utf-8")
        identities = [item["id"] for item in items]
        ledger.reserve(edition="example", period=date, date=date, item_ids=identities, sent_at=now.isoformat())
        try:
            send_message(message, username=sender, password=password)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError, smtplib.SMTPSenderRefused,
                smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
            ledger.cancel()
            raise
        # Timeouts/disconnects intentionally leave the durable reservation.
        try:
            saved = reading_state.record_delivery(state_path, history, edition="example", period=date,
                date=date, item_ids=identities, sent_at=now.isoformat(), history_days=180)
            ledger.complete(saved)
        except (ValueError, OSError) as exc:
            raise ConfigurationError("SMTP已接受修复验收邮件，但记账未确认；请核对收件箱，禁止自动重发") from exc
        return {"status": "smtp_accepted", "count": len(items),
                "source_counts": dict(Counter(item["source"] for item in items)), "date": date}


def main(argv=None):
    parser = SafeArgumentParser(description="发送已核验的新内容作为修复验收；不占正式日报名额")
    parser.add_argument("--send", action="store_true", help="明确发送真实邮件；缺少此项只显示帮助")
    parser.add_argument("--source-dir", type=Path, help="已验证预览工件中包含reading-reserve.json的目录")
    parser.add_argument("--work-dir", type=Path, default=Path(".local/repaired-example"))
    try:
        args = parser.parse_args(argv)
        if not args.send:
            parser.print_help()
            return 0
        if args.source_dir is None:
            raise ConfigurationError("修复验收发送需要指定来源目录")
        result = send_example(args.source_dir, args.work_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ConfigurationError, LedgerError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except smtplib.SMTPException:
        print("修复验收SMTP投递失败；请核对服务器状态及投递记录", file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError):
        print("修复验收配置、来源或网络操作失败；未确认结果前禁止自动重发", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
