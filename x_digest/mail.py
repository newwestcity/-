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
from urllib.parse import urlsplit
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


_ARTICLE_SCORE_LABELS = {
    "depth": ("深度", 30),
    "evidence": ("证据", 25),
    "learning": ("可学性", 25),
    "readability": ("可读性", 10),
    "timeliness": ("时效", 10),
}
_PROJECT_SCORE_LABELS = {
    "learnability": ("可学性", 30),
    "reproducibility": ("可复刻性", 30),
    "practicality": ("实用性", 20),
    "popularity": ("热度", 10),
    "maintenance": ("维护", 10),
}
_SOURCE_STATUS_LABELS = {
    "available": "已取得来源内容",
    "partial": "仅取得部分来源内容",
    "unavailable": "本期暂未取得可核实内容",
    "no_candidates": "本期未找到候选内容",
    "no_qualified": "本期没有通过筛选的内容",
    "not_run": "本期未检索",
    "links_only": "仅提供搜索标题与原文链接，未读取全文、未做深度评分",
}


def _reading_text(value: object, field: str, maximum: int = 1000) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\x00" in value):
        raise ValueError(f"阅读条目的 {field} 字段无效")
    return value.strip()


def _reading_list(item: dict, field: str, minimum: int, maximum: int) -> list[str]:
    values = item.get(field)
    if not isinstance(values, list) or not minimum <= len(values) <= maximum:
        raise ValueError(f"阅读条目的 {field} 列表无效")
    return [_reading_text(value, field, 600) for value in values]


def _reading_url(item: dict) -> str:
    """Require an official, canonical link matching the verified item ID."""
    from .github_projects import canonical_repository_url
    from .selection import canonical_post_url

    url = _reading_text(item.get("url"), "url", 500)
    item_id = _reading_text(item.get("id"), "id", 200)
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or any(char.isspace() for char in url):
        raise ValueError("阅读条目必须使用不含参数的 HTTPS 原文链接")
    source = item.get("source")
    if source == "X":
        canonical, post_id, _ = canonical_post_url(url)
        if url != canonical or item_id not in {post_id, "x:" + post_id}:
            raise ValueError("X 链接与已核实条目不匹配")
    elif source == "雪球":
        match = re.fullmatch(r"https://xueqiu\.com/([1-9][0-9]{0,24})/([1-9][0-9]{0,24})", url)
        if not match or item_id != "xueqiu:" + match.group(2):
            raise ValueError("雪球链接与已核实条目不匹配")
    elif source == "GitHub":
        canonical = canonical_repository_url(url)
        if (canonical != url or
                item_id.casefold() != ("github:" + canonical.removeprefix("https://github.com/")).casefold()):
            raise ValueError("GitHub 链接与已核实条目不匹配")
    else:
        raise ValueError("阅读条目的来源无效")
    return url


def _score_label(item: dict, *, project: bool) -> str:
    scores = item.get("scores")
    if scores is None:
        return "编辑评分：本条未提供评分"
    rubric = _PROJECT_SCORE_LABELS if project else _ARTICLE_SCORE_LABELS
    if not isinstance(scores, dict) or set(scores) != set(rubric):
        raise ValueError("编辑评分维度无效")
    for key, (_, maximum) in rubric.items():
        if type(scores[key]) is not int or not 0 <= scores[key] <= maximum:
            raise ValueError("编辑评分必须是维度范围内的整数")
    total = sum(scores.values())
    if "score_total" in item and (type(item["score_total"]) is not int or item["score_total"] != total):
        raise ValueError("编辑评分总分不匹配")
    details = "，".join(f"{label}{scores[key]}/{maximum}" for key, (label, maximum) in rubric.items())
    return f"编辑评分：{total}/100（{details}）"


def _reading_date(value: object, *, kind: object = "recent", project: bool = False) -> str:
    if value in (None, ""):
        if kind == "classic":
            raise ValueError("经典阅读必须提供已核实的原文日期")
        return "原文日期未确认；不计作近期内容"
    try:
        if not isinstance(value, str):
            raise ValueError
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.utcoffset() is None:
            raise ValueError
        date_label = published.astimezone(_BEIJING).strftime("%Y-%m-%d")
    except (ValueError, OverflowError) as exc:
        raise ValueError("阅读条目的时间必须包含时区") from exc
    if project:
        return f"最近更新 {date_label}（北京时间）"
    label = "经典阅读" if kind == "classic" else "近期阅读"
    return f"{label} · 原文 {date_label}（北京时间）"


def _coverage_lines(items: list[dict], edition: str, coverage: dict | None) -> list[str]:
    sources = ("GitHub",) if edition == "weekly" else ("X", "雪球")
    targets = {"GitHub": 10} if edition == "weekly" else {"X": 10, "雪球": 4}
    statuses = {}
    if coverage is not None:
        if not isinstance(coverage, dict) or set(coverage) - {"targets", "source_status"}:
            raise ValueError("阅读覆盖说明仅接受目标数量和固定来源状态")
        supplied_targets = coverage.get("targets", {})
        statuses = coverage.get("source_status", {})
        if (not isinstance(supplied_targets, dict) or set(supplied_targets) - set(sources)
                or any(type(value) is not int or not 1 <= value <= 30 for value in supplied_targets.values())
                or not isinstance(statuses, dict) or set(statuses) - set(sources)
                or any(not isinstance(value, str) or value not in _SOURCE_STATUS_LABELS for value in statuses.values())):
            raise ValueError("阅读覆盖说明无效")
        targets.update(supplied_targets)
    lines = []
    for source in sources:
        count = sum(item["source"] == source for item in items)
        candidate_count = sum(item["source"] == source and item.get("reading_kind") == "link_candidate" for item in items)
        if candidate_count:
            count -= candidate_count
            line = f"{source}：本期 {count} 篇，另附 {candidate_count} 条阅读候选；目标 {targets[source]} 篇"
        else:
            line = f"{source}：本期 {count} {'个项目' if edition == 'weekly' else '篇'}，目标 {targets[source]} {'个' if edition == 'weekly' else '篇'}"
        if source in statuses:
            line += "；" + _SOURCE_STATUS_LABELS[statuses[source]]
        reserve_count = sum(item["source"] == source and item.get("from_reserve") is True for item in items)
        if reserve_count:
            line += f"；其中 {reserve_count} 条为此前核验的备用阅读，仍按原帖日期检查时效"
        if count < targets[source]:
            line += "；符合要求的内容不足，宁缺毋滥"
        lines.append(line + "。")
    return lines


def build_reading_message(
    items: list[dict],
    *,
    edition: str,
    date: str,
    sender: str,
    recipient: str,
    coverage: dict | None = None,
) -> EmailMessage:
    """Render verified reading recommendations; never copy the source body.

    ``coverage`` accepts only ``targets`` and ``source_status`` maps keyed by
    source. Statuses use the fixed codes in ``_SOURCE_STATUS_LABELS``; raw
    provider errors cannot accidentally become reader-facing coverage notes.
    """
    sender = _mailbox(sender, "sender")
    recipient = _mailbox(recipient, "recipient")
    if edition not in ("daily", "weekly"):
        raise ValueError("阅读版本必须是 daily 或 weekly")
    try:
        if not isinstance(date, str) or datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("阅读日期必须是 YYYY-MM-DD") from exc
    if not isinstance(items, list) or len(items) > 30 or any(not isinstance(item, dict) for item in items):
        raise ValueError("阅读条目列表无效")

    sources = ("GitHub",) if edition == "weekly" else ("X", "雪球")
    prepared = []
    identities = set()
    for item in items:
        if item.get("source") not in sources:
            raise ValueError("阅读条目来源与邮件版本不匹配")
        url = _reading_url(item)
        identity = url.casefold() if edition == "weekly" else url
        if identity in identities:
            raise ValueError("阅读条目重复")
        identities.add(identity)
        title = _reading_text(item.get("title"), "title", 240)
        if item.get("reading_kind") == "link_candidate" or item.get("content_basis") == "search_metadata":
            if (item["source"] != "雪球" or item.get("reading_kind") != "link_candidate"
                    or item.get("content_basis") != "search_metadata"):
                raise ValueError("搜索链接候选必须明确标记为雪球搜索元数据")
            # No model summary, guessed author/date, score, or learning claim
            # can be promoted from a search title into an article assessment.
            prepared.append((
                item["source"], title, url,
                ["雪球阅读候选｜未读取全文，未做深度评分", "原文日期未确认，不计作近期文章"],
                "", "", [], "", [],
                "仅依据搜索工具返回的标题和原文链接；请打开阅读全文", True,
            ))
            continue
        summary = _reading_text(item.get("summary"), "summary", 1000)
        reason = _reading_text(item.get("reason"), "reason", 600)
        learning = _reading_list(item, "learning_points", 1, 5)
        score = _score_label(item, project=edition == "weekly")
        meta = []
        extra = []
        if edition == "weekly":
            meta.append("仓库：" + url.removeprefix("https://github.com/"))
            stars = item.get("stars")
            if type(stars) is not int or stars < 0:
                raise ValueError("项目星标数无效")
            license_name = _reading_text(item.get("license"), "license", 100)
            language = item.get("language") or "未标注语言"
            language = _reading_text(language, "language", 100)
            meta.append(f"{language} · {stars:,} stars · 许可证 {license_name}")
            meta.append(_reading_date(item.get("pushed_at"), project=True))
            growth = item.get("weekly_star_growth")
            if growth is not None:
                if type(growth) is not int:
                    raise ValueError("项目周星标变化无效")
                meta.append(f"可比较的两次快照之间，7 天星标变化 {growth:+,}")
            else:
                meta.append("热度参考当前星标及活跃情况；本期无可比较的周涨星数据")
            difficulty = _reading_text(item.get("difficulty"), "difficulty", 30)
            if difficulty not in {"入门", "中等", "进阶"}:
                raise ValueError("项目难度无效")
            prerequisites = _reading_list(item, "prerequisites", 1, 4)
            effort = _reading_text(item.get("effort_estimate"), "effort_estimate", 300)
            steps = _reading_list(item, "reproduction_steps", 3, 3)
            extra.extend([
                f"学习安排：{difficulty}；时间估计：{effort.removeprefix('估计：')}",
                "前置条件：" + "；".join(prerequisites),
                "复刻练习：" + " → ".join(f"{index}. {step}" for index, step in enumerate(steps, 1)),
            ])
            basis = "评审依据：README 与仓库公开元数据；未实际安装或运行，步骤和耗时仅供练习规划。"
        else:
            author = _reading_text(item.get("author"), "author", 200)
            category = _reading_text(item.get("category"), "category", 100)
            meta.append(f"{item['source']} · {category} · {author}")
            meta.append(_reading_date(item.get("published_at"), kind=item.get("reading_kind", "recent")))
            minutes = item.get("estimated_read_minutes")
            if minutes is not None:
                if type(minutes) is not int or not 1 <= minutes <= 120:
                    raise ValueError("阅读时间估计无效")
                meta.append(f"阅读时间估计 {minutes} 分钟")
            if item["source"] == "X":
                basis = "评审依据：已核实的 X 正文摘录；未核实完整长帖或讨论串，请打开原帖阅读。"
            elif (item.get("assessment_basis") == "依据已核实文章正文评审"
                  and item.get("content_basis") == "original_text"
                  and not item.get("text_truncated")):
                basis = "评审依据：已取得的雪球原文正文。"
            else:
                basis = "评审依据：已取得的雪球正文摘录；请打开原文阅读全文。"
        prepared.append((item["source"], title, url, meta, summary, reason, learning, score, extra, basis, False))

    title = "每周 GitHub 项目精选" if edition == "weekly" else "每日阅读精选"
    unit = "个" if edition == "weekly" else "篇"
    overview = _coverage_lines(items, edition, coverage)
    candidate_count = sum(row[-1] for row in prepared)
    if candidate_count:
        overview.append(f"本期另附 {candidate_count} 条雪球阅读候选，仅提供搜索标题和原文链接；未读取全文，未做深度评分。")
    rubric = (
        "筛选：优先 AI 应用、自动化与实用工具；可学性 30、可复刻性 30、实用性 20、热度 10、维护 10。"
        if edition == "weekly" else
        "筛选：优先有完整论证的长文与讨论；深度 30、证据 25、可学性 25、可读性 10、时效 10。"
    )
    score_notice = "分数为编辑评估，不代表事实真伪保证。只提供导读和原文链接，请结合原文判断。"
    if candidate_count:
        rubric += "该标准仅适用于已核实正文的文章，雪球阅读候选不参与评分。"
    if candidate_count and candidate_count == len(items):
        rubric = "本期仅提供雪球搜索链接候选，尚未核实正文内容。"
        score_notice = "标题来自搜索工具返回的来源元数据；请打开原文判断是否值得阅读全文。"
    text_parts = [f"{title} · {date}", *overview, rubric, score_notice]
    html_parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>',
        '<body style="font-family:system-ui,sans-serif;line-height:1.7;max-width:760px;margin:auto;padding:20px;color:#202124">',
        f"<h1>{escape(title)} · {escape(date)}</h1>",
        *[f"<p>{escape(line)}</p>" for line in overview],
        f"<p>{escape(rubric)}</p>",
        f'<p style="color:#666">{escape(score_notice)}</p>',
    ]
    index = 0
    for source in sources:
        group = [row for row in prepared if row[0] == source]
        if not group:
            continue
        text_parts.append(f"【{source}】")
        html_parts.append(f"<h2>{escape(source)}</h2>")
        for _, item_title, url, meta, summary, reason, learning, score, extra, basis, link_candidate in group:
            index += 1
            if link_candidate:
                text_parts.append("\n".join([f"{index}. {item_title}", *meta, basis, f"原文：{url}"]))
                html_parts.extend([
                    '<article style="border-top:1px solid #ddd;padding:12px 0">',
                    f'<h3>{index}. <a href="{escape(url, quote=True)}">{escape(item_title)}</a></h3>',
                    *[f'<p style="color:#666">{escape(line)}</p>' for line in meta],
                    f'<p style="color:#666">{escape(basis)}</p>',
                    f'<p><a href="{escape(url, quote=True)}">打开原文</a></p>',
                    "</article>",
                ])
                continue
            text_parts.append("\n".join([
                f"{index}. {item_title}", *meta,
                f"导读：{summary}", f"为什么读：{reason}",
                "能学到：" + "；".join(learning), *extra, score, basis,
                f"原文：{url}",
            ]))
            html_parts.extend([
                '<article style="border-top:1px solid #ddd;padding:12px 0">',
                f'<h3>{index}. <a href="{escape(url, quote=True)}">{escape(item_title)}</a></h3>',
                *[f'<p style="color:#666">{escape(line)}</p>' for line in meta],
                f"<p><strong>导读：</strong>{escape(summary)}</p>",
                f"<p><strong>为什么读：</strong>{escape(reason)}</p>",
                "<p><strong>能学到：</strong>" + "；".join(escape(point) for point in learning) + "</p>",
                *[f"<p>{escape(line)}</p>" for line in extra],
                f'<p style="color:#666">{escape(score)}</p>',
                f'<p style="color:#666">{escape(basis)}</p>',
                f'<p><a href="{escape(url, quote=True)}">打开原文</a></p>',
                "</article>",
            ])
    if not items:
        text_parts.append("本期没有通过筛选的内容。")
        html_parts.append("<p>本期没有通过筛选的内容。</p>")
    html_parts.append("</body></html>")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    count_label = f"{len(items)}{unit}"
    if candidate_count:
        count_label = f"{len(items) - candidate_count}篇+{candidate_count}条候选"
    message["Subject"] = f"{title}｜{date}｜{count_label}"
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
