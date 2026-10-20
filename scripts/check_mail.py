"""Opt-in QQ SMTP connectivity test; no model API or digest is involved."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys


# Support `python scripts/check_mail.py` from the checkout without installation.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from x_digest.mail import build_message, send_message


SUBJECT = "每日阅读精选｜邮箱连接测试"
BODY = (
    "这是一封邮箱连接测试邮件，用于确认每日阅读精选可以通过已配置的 QQ 邮箱 SMTP 投递。\n\n"
    "本邮件仅用于连接测试，正式日报尚未启用。\n"
)


class CheckError(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckError("invalid_arguments")


def _safe_error(exc: Exception) -> str:
    # SMTP error bodies can echo addresses, authorization codes or remote text.
    # Emit only one of these fixed labels and a bounded numeric SMTP status.
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        label = "smtp_authentication_error"
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        label = "smtp_recipients_refused"
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        label = "smtp_sender_refused"
    elif isinstance(exc, smtplib.SMTPDataError):
        label = "smtp_data_error"
    elif isinstance(exc, smtplib.SMTPConnectError):
        label = "smtp_connect_error"
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        label = "smtp_disconnected"
    elif isinstance(exc, smtplib.SMTPException):
        label = "smtp_error"
    elif isinstance(exc, ssl.SSLError):
        label = "tls_error"
    elif isinstance(exc, TimeoutError):
        label = "network_timeout"
    elif isinstance(exc, OSError):
        label = "network_error"
    elif isinstance(exc, ValueError):
        label = "invalid_configuration"
    else:
        label = "unexpected_error"
    status = getattr(exc, "smtp_code", None)
    if type(status) is int and 100 <= status <= 999:
        return f"{label};smtp_code={status}"
    return label


def send_test() -> int:
    sender = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_AUTH_CODE", "")
    recipient_setting = os.environ.get("MAIL_TO", "")
    configured = {
        "SMTP_USERNAME_configured": bool(sender.strip()),
        "SMTP_AUTH_CODE_configured": bool(password.strip()),
        "MAIL_TO_configured": bool(recipient_setting.strip()),
    }
    print(json.dumps(configured), flush=True)
    missing = [name for name in ("SMTP_USERNAME", "SMTP_AUTH_CODE") if not configured[name + "_configured"]]
    if missing:
        print("::error::missing_required=" + ",".join(missing), file=sys.stderr)
        return 2
    recipient = recipient_setting if recipient_setting.strip() else sender
    try:
        now = datetime.now(timezone.utc)
        # Reuse strict address/header validation, then discard the empty digest
        # completely so this mail cannot be mistaken for a zero-item newsletter.
        message = build_message([], recipient=recipient, sender=sender, digest_date=now.date().isoformat())
        message.clear_content()
        message.replace_header("Subject", SUBJECT)
        message.set_content(BODY)
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        send_message(message, host="smtp.qq.com", port=465, username=sender, password=password)
    except Exception as exc:
        print("::error::mail_error=" + _safe_error(exc), file=sys.stderr)
        return 1
    print("::notice::smtp_accepted=true")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="发送一封 QQ SMTP 邮箱连接测试；默认仅显示帮助")
    parser.add_argument("--send-test", action="store_true", help="明确发送一次测试邮件，不调用模型API、不启用日报")
    try:
        args = parser.parse_args(argv)
    except CheckError:
        print("::error::mail_error=invalid_arguments", file=sys.stderr)
        return 2
    if not args.send_test:
        parser.print_help()
        return 0
    return send_test()


if __name__ == "__main__":
    raise SystemExit(main())
