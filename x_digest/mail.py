"""Render the daily digest and deliver it using verified TLS SMTP."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from html import escape
import re
import smtplib
import ssl
from zoneinfo import ZoneInfo


_POST_URL = re.compile(
    r"https://(?:x\.com|twitter\.com)/[A-Za-z0-9_]{1,15}/status/[0-9]+/?"
)
_REQUIRED_FIELDS = ("id", "url", "author", "category", "summary", "reason")
_BEIJING = ZoneInfo("Asia/Shanghai")


def _mailbox(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
        raise ValueError(f"{field} 必须是非空且不含换行的邮箱地址")
    try:
        parsed = Address(addr_spec=value)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"{field} 邮箱地址无效") from exc
    if not parsed.username or not parsed.domain:
        raise ValueError(f"{field} 邮箱地址无效")
    return str(parsed)


def _reading_label(post: dict) -> str:
    classic = post.get("reading_kind") == "classic"
    kind = "经典阅读" if classic else "近期"
    published_at = post.get("published_at")
    if published_at is None or published_at == "":
        if classic:
            raise ValueError("经典阅读必须提供包含时区的原帖发布日期")
        return f"{kind} · 原帖日期未提供"
    try:
        if not isinstance(published_at, str):
            raise ValueError
        published = datetime.fromisoformat(published_at)
        if published.tzinfo is None or published.utcoffset() is None:
            raise ValueError
        local_date = published.astimezone(_BEIJING).strftime("%Y-%m-%d")
    except (ValueError, OverflowError) as exc:
        raise ValueError("原帖发布日期必须是合法且包含时区的 ISO 日期时间") from exc
    return f"{kind} · 原帖 {local_date}（北京时间）"


def build_message(
    posts: list[dict],
    *,
    recipient: str,
    sender: str,
    digest_date: str,
    requested_count: int = 10,
) -> EmailMessage:
    """Build a multipart digest from already selected real posts.

    Selection and provenance checks belong to the caller. This renderer never
    invents replacement posts when fewer than the requested number are present.
    """
    sender = _mailbox(sender, "sender")
    recipient = _mailbox(recipient, "recipient")
    if not isinstance(digest_date, str) or not digest_date.strip() or any(
        char in digest_date for char in "\r\n"
    ):
        raise ValueError("digest_date 必须是非空且不含换行的日期")
    if not isinstance(requested_count, int) or isinstance(requested_count, bool) or requested_count < 1:
        raise ValueError("requested_count 必须是正整数")

    groups: OrderedDict[str, list[tuple[dict, str]]] = OrderedDict()
    for post in posts:
        if not isinstance(post, dict) or any(
            not isinstance(post.get(field), str) or not post[field].strip()
            for field in _REQUIRED_FIELDS
        ):
            raise ValueError("帖子缺少必要的非空字符串字段")
        if not _POST_URL.fullmatch(post["url"]):
            raise ValueError("帖子链接必须是 X 或 Twitter 的 HTTPS 原帖地址")
        groups.setdefault(post["category"], []).append((post, _reading_label(post)))

    count = len(posts)
    overview = f"本期精选 {count} 条，目标 {requested_count} 条。"
    classic_count = sum(post.get("reading_kind") == "classic" for post in posts)
    if classic_count:
        overview += f"其中近期 {count - classic_count} 条，经典阅读 {classic_count} 条。"
        if classic_count == count:
            overview += "本期近期检索未取得合格帖子，仅提供经典阅读。"
    if count < requested_count:
        overview += "符合要求的内容不足，宁缺毋滥，不补入未经核实的帖子。"
    text_parts = [f"每日 X 精选 · {digest_date}", overview]
    html_parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>',
        f"<h1>每日 X 精选 · {escape(digest_date)}</h1>",
        f"<p>{escape(overview)}</p>",
    ]
    index = 0
    for category, category_posts in groups.items():
        text_parts.append(f"\n【{category}】")
        html_parts.append(f"<h2>{escape(category)}</h2>")
        for post, reading_label in category_posts:
            index += 1
            text_parts.append(
                f"{index}. {post['author']}\n"
                f"{reading_label}\n"
                f"摘要：{post['summary']}\n"
                f"推荐理由：{post['reason']}\n"
                f"原帖：{post['url']}"
            )
            html_parts.append(
                "<article>"
                f"<h3>{index}. {escape(post['author'])}</h3>"
                f"<p>{escape(reading_label)}</p>"
                f"<p><strong>摘要：</strong>{escape(post['summary'])}</p>"
                f"<p><strong>推荐理由：</strong>{escape(post['reason'])}</p>"
                f'<p><a href="{escape(post["url"], quote=True)}">查看原帖</a></p>'
                "</article>"
            )
    if not posts:
        text_parts.append("今天没有符合筛选要求的帖子。")
        html_parts.append("<p>今天没有符合筛选要求的帖子。</p>")
    html_parts.append("</body></html>")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"每日 X 精选｜{digest_date}｜{count} 条"
    message.set_content("\n\n".join(text_parts))
    message.add_alternative("\n".join(html_parts), subtype="html")
    return message


def send_message(
    message: EmailMessage,
    *,
    host: str = "smtp.qq.com",
    port: int = 465,
    username: str,
    password: str,
) -> None:
    """Send through implicit TLS; credentials are neither persisted nor logged."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("SMTP 用户名未配置")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("SMTP 授权码未配置")
    if not message.get("From") or not message.get("To"):
        raise ValueError("邮件必须指定发件人与收件人")
    context = ssl.create_default_context()
    accepted = False
    try:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
            accepted = True
    except (smtplib.SMTPException, OSError):
        # DATA has already been accepted. A failed QUIT must not turn this into
        # a failed delivery and cause the caller to send the same mail again.
        if not accepted:
            raise
