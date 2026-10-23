"""Discover public learning projects from GitHub's official REST API.

Popularity is an explicit proxy using observed stars, forks and activity. It is
never described as weekly star growth without a comparable saved observation.
README instructions are read as untrusted text, never executed or installed.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPException
import json
import math
import re
import ssl
from threading import Lock
import time
from urllib import error, request
from urllib.parse import urlencode, urlsplit


MAX_PROJECTS = 20
MAX_CALLS = 3 + MAX_PROJECTS
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_README_BYTES = 128 * 1024
REQUEST_TIMEOUT = 15
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
_SPDX = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "ISC",
         "MPL-2.0", "GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0",
         "Unlicense", "CC0-1.0", "BSL-1.0"}
_TOPIC = re.compile(r"\b(ai|llm|agent|rag|mcp|automation|workflow|toolkit|cli|assistant|"
                    r"productivity|utility|utilities|self-hosted|ocr|notebook)\b|"
                    r"人工智能|自动化|实用工具|大模型|智能体|知识管理", re.I)
_PRACTICAL = re.compile(r"\b(app(?:lication)?s?|tools?|automation|workflows?|cli|assistant|"
                        r"productivity|utilities|utility|browser|desktop|dashboard|"
                        r"chatbot|copilot|note[- ]?taking|document[- ]?processing)\b|"
                        r"应用|工具|自动化|工作流|桌面|浏览器|知识管理", re.I)
_CORE_INFRA = re.compile(r"\b(?:training|inference|serving)[ -]+(?:engine|framework|runtime|library)|"
                         r"\b(?:gpu|cuda)[ -]+(?:kernel|runtime)|"
                         r"\b(?:distributed|model|llm)[ -]+(?:training|inference)|"
                         r"\bpre[- ]?trained models?\b|训练框架|推理引擎|推理框架", re.I)
_QUICKSTART = re.compile(r"(?im)^(?:#{1,6}[ \t]*)?[^\w\n<>]{0,8}(?:"
                         r"(?:quick[ \t]*start|getting started|installation|install|usage|setup)\b|"
                         r"快速开始|安装|使用|上手)")
_COMMAND = re.compile(r"(?im)```(?:bash|sh|shell|console|powershell|zsh|python|javascript|typescript)?\s*$|"
                      r"\b(pip install|uv sync|npm (?:install|run)|pnpm |yarn |docker (?:run|compose)|"
                      r"cargo (?:install|run)|go (?:install|run)|make (?:install|run))\b")
_SIGNALS = {
    "quickstart": _QUICKSTART,
    "examples": re.compile(r"(?im)^(?:#{1,6}[ \t]*)?[^\w\n<>]{0,8}(examples?|tutorials?|demos?|示例|教程)|(?:^|[/\s(])examples?/", re.I),
    "tests": re.compile(r"(?im)^(?:#{1,6}[ \t]*)?[^\w\n<>]{0,8}(testing|tests?|测试)\b|\b(pytest|npm test|cargo test|go test|make test)\b|\]\([^)]*tests?/", re.I),
    "docs": re.compile(r"(?im)^(?:#{1,6}[ \t]*)?[^\w\n<>]{0,8}(documentation|docs|文档)\b|\]\([^)]*(?:/docs[/.)]|docs\.)", re.I),
}


@dataclass
class ProjectFetchResult:
    projects: list[dict]
    diagnostics: dict[str, int]


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A renamed repository can be rediscovered on the next search. Never
        # forward an optional bearer token to any redirected destination.
        return None


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("GitHub observation time must include a timezone")
    return value.astimezone(timezone.utc)


def _date(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid GitHub timestamp")
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def canonical_repository_url(url: str) -> str:
    if not isinstance(url, str) or any(char.isspace() for char in url):
        raise ValueError("Invalid GitHub repository URL")
    parsed = urlsplit(url)
    name = parsed.path.strip("/")
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.query or parsed.fragment or not _NAME.fullmatch(name)
            or name.split("/")[1] in {".", ".."}):
        raise ValueError("Invalid GitHub repository URL")
    return "https://github.com/" + name


def _read(response, deadline: float) -> bytes:
    parts, total = [], 0
    reader = getattr(response, "read1", response.read)
    while total <= MAX_RESPONSE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        part = reader(min(65536, MAX_RESPONSE_BYTES + 1 - total))
        if not part:
            break
        parts.append(part)
        total += len(part)
    if total > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
        raise ValueError("GitHub response exceeded limits")
    return b"".join(parts)


class _Client:
    def __init__(self, token):
        self.token, self.lock, self.unavailable = token, Lock(), False
        self.diagnostics: dict[str, int] = {"api_requests": 0, "searches_succeeded": 0,
                                          "repositories_received": 0, "readmes_requested": 0,
                                          "projects_verified": 0}

    def count(self, label: str, amount: int = 1):
        with self.lock:
            self.diagnostics[label] = self.diagnostics.get(label, 0) + amount

    def get(self, path: str, query: dict | None = None):
        # Paths are constructed only from validated repository names, never
        # from README links, download_url values or model output.
        if (path != "/search/repositories"
                and not (path.startswith("/repos/") and path.endswith("/readme")
                         and _NAME.fullmatch(path[len('/repos/'):-len('/readme')]))):
            self.count("invalid_endpoint")
            return None
        with self.lock:
            if self.unavailable:
                return None
            if self.diagnostics["api_requests"] >= MAX_CALLS:
                self.diagnostics["request_budget_exhausted"] = 1
                return None
            self.diagnostics["api_requests"] += 1
        url = "https://api.github.com" + path
        if query:
            url += "?" + urlencode(query)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Daily-Reading-Projects/0.2",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            opener = request.build_opener(_NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
            deadline = time.monotonic() + REQUEST_TIMEOUT
            with opener.open(request.Request(url, headers=headers), timeout=REQUEST_TIMEOUT) as response:
                raw = _read(response, deadline)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                self.count("invalid_json")
                return None
            return value
        except error.HTTPError as exc:
            label = ("credential_rejected" if exc.code == 401 else
                     "rate_limited_or_forbidden" if exc.code in {403, 429} else
                     "not_found" if exc.code == 404 else
                     "redirect_refused" if exc.code in {301, 302, 303, 307, 308} else "http_error")
            self.count(label)
            if exc.code in {401, 403, 429}:
                with self.lock:
                    self.unavailable = True
            exc.close()
        except (error.URLError, OSError, HTTPException):
            self.count("network_error")
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self.count("invalid_or_oversized_response")
        return None


def _metadata(item: object, *, now: datetime, client: _Client) -> dict | None:
    try:
        if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
            raise ValueError
        name = item["full_name"]
        if not _NAME.fullmatch(name) or name.split("/")[1] in {".", ".."}:
            raise ValueError
        url = canonical_repository_url(item.get("html_url"))
        if url.casefold() != ("https://github.com/" + name).casefold():
            raise ValueError
        owner = item.get("owner")
        if not isinstance(owner, dict) or str(owner.get("login", "")).casefold() != name.split("/")[0].casefold():
            raise ValueError
        if any(type(item.get(flag)) is not bool for flag in ("private", "archived", "fork")):
            raise ValueError
        if item["private"] or item["archived"] or item["fork"] or item.get("disabled") is True:
            client.count("excluded_private_archived_or_fork")
            return None
        stars, forks = item.get("stargazers_count"), item.get("forks_count")
        if any(type(number) is not int or not 0 <= number <= 10**10 for number in (stars, forks)):
            raise ValueError
        created, pushed = _date(item.get("created_at")), _date(item.get("pushed_at"))
        if created > now + timedelta(minutes=5) or pushed > now + timedelta(minutes=5):
            raise ValueError
        if stars < 50 or pushed < now - timedelta(days=7):
            client.count("excluded_low_popularity_or_inactive")
            return None
        license_data = item.get("license")
        spdx = license_data.get("spdx_id") if isinstance(license_data, dict) else None
        if spdx not in _SPDX:
            client.count("excluded_license_unverified")
            return None
        description = item.get("description") or ""
        topics = item.get("topics") or []
        if not isinstance(description, str) or not isinstance(topics, list):
            raise ValueError
        topics = [topic[:80] for topic in topics if isinstance(topic, str)][:30]
        subject = " ".join((name, description, *topics))
        if not _TOPIC.search(subject):
            client.count("excluded_off_topic")
            return None
        if (name.split("/")[1].casefold().startswith("awesome-")
                or re.search(r"curated list of|list of resources|资源汇总|资源清单", description, re.I)):
            client.count("excluded_resource_list")
            return None
        age_days = max((now - created).total_seconds() / 86400, 1)
        activity_days = max((now - pushed).total_seconds() / 86400, 0)
        velocity = round(stars / age_days, 2)
        score = (min(60, math.log10(stars + 1) * 12) + min(10, math.log10(forks + 1) * 4)
                 + 10 * max(0, 1 - activity_days / 7)
                 + (min(20, math.log1p(velocity) * 4) if age_days <= 90 else 0))
        language = item.get("language")
        if language is not None and not isinstance(language, str):
            raise ValueError
        # These are metadata hints for allocating the limited README budget,
        # not assertions about capabilities or whether a project can be run.
        focus_fields = {"name": name, "description": description, "topics": " ".join(topics)}
        practical = [field + ": " + match.group(0).casefold()
                     for field, value in focus_fields.items() for match in _PRACTICAL.finditer(value)][:12]
        infrastructure = [field + ": " + match.group(0).casefold()
                          for field, value in focus_fields.items() for match in _CORE_INFRA.finditer(value)][:12]
        practical_priority = int(bool(practical)) - int(bool(infrastructure))
        return {"id": "github:" + name.casefold(), "url": url, "source": "GitHub", "title": name,
                "name": name, "author": name.split("/")[0], "description": description[:1000],
                "language": language, "stars": stars, "forks": forks, "license": spdx,
                "license_name": str(license_data.get("name") or spdx)[:120],
                "archived": False, "fork": False, "topics": topics,
                "practical_project_priority": practical_priority,
                "metadata_focus": {"basis": "GitHub名称、描述和主题中的关键词；尚需README核对",
                                   "practical_signals": practical, "infrastructure_signals": infrastructure},
                "created_at": created.isoformat(), "pushed_at": pushed.isoformat(),
                "verified_at": now.isoformat(), "weekly_star_growth": None,
                "popularity_proxy": {"label": "热门候选指标（非本周涨星）", "score": round(score, 2),
                                     "active_in_last_7_days": True, "new_in_last_90_days": age_days <= 90,
                                     "stars_per_day_since_creation": velocity}}
    except (ValueError, TypeError, OverflowError):
        client.count("invalid_repository_metadata")
        return None


def _readme(project: dict, *, client: _Client) -> dict | None:
    client.count("readmes_requested")
    value = client.get("/repos/" + project["name"] + "/readme")
    if value is None:
        return None
    try:
        content, size, sha = value.get("content"), value.get("size"), value.get("sha")
        if (value.get("encoding") != "base64" or not isinstance(content, str)
                or type(size) is not int or not 1 <= size <= MAX_README_BYTES
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or len(content) > MAX_README_BYTES * 2):
            raise ValueError
        raw = base64.b64decode("".join(content.split()), validate=True)
        if (len(raw) != size or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != sha):
            raise ValueError
        text = raw.decode("utf-8")
        if len(text.strip()) < 300:
            client.count("excluded_readme_too_short")
            return None
        html_url = value.get("html_url")
        prefix = project["url"].casefold() + "/blob/"
        if (not isinstance(html_url, str) or not html_url.casefold().startswith(prefix)
                or urlsplit(html_url).netloc.lower() != "github.com"):
            raise ValueError
        signals = {name: bool(pattern.search(text)) for name, pattern in _SIGNALS.items()}
        signals["quickstart"] = signals["quickstart"] and bool(_COMMAND.search(text))
        if not signals["quickstart"]:
            client.count("excluded_no_documented_quickstart")
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        link_lines = sum(bool(re.match(r"^[-*]\s+\[.*\]\(https?://", line)) for line in lines)
        if link_lines >= 12 and link_lines / max(len(lines), 1) > .45 and "```" not in text:
            client.count("excluded_resource_list")
            return None
        evidence = {name: [line[:200] for line in lines if pattern.search(line)][:2]
                    for name, pattern in _SIGNALS.items()}
        return {**project, "readme_text": text[:12000], "readme_sha": sha, "readme_url": html_url,
                "has_quickstart": signals["quickstart"], "has_examples": signals["examples"],
                "has_tests": signals["tests"], "has_docs": signals["docs"],
                "readme_evidence": evidence,
                "readiness_basis": "README中的启动步骤、示例和测试说明；未实际安装或运行",
                "learning_readiness_score": 25 + 20 + min(len(text) // 500, 10)
                                            + 20 * signals["examples"] + 15 * signals["docs"] + 10 * signals["tests"],
                "mentions_api_key": bool(re.search(r"API[_ -]?KEY|access token", text, re.I)),
                "mentions_gpu": bool(re.search(r"\bGPU\b|\bCUDA\b|显卡", text, re.I)),
                "mentions_docker": bool(re.search(r"\bdocker\b", text, re.I))}
    except (ValueError, TypeError, UnicodeError):
        client.count("invalid_readme")
        return None


def build_snapshot(projects: list[dict], *, observed_at: datetime) -> dict:
    """Store observations for an honest future star-change comparison."""
    now = _aware(observed_at)
    values = {}
    for project in projects:
        name, stars = project.get("id"), project.get("stars")
        if (not isinstance(name, str) or not name.startswith("github:")
                or not _NAME.fullmatch(name[7:]) or type(stars) is not int or stars < 0):
            raise ValueError("Invalid project snapshot")
        values[name.casefold()] = stars
    return {"observed_at": now.isoformat(), "stars": values}


def compute_growth(projects: list[dict], previous_snapshot: dict | None, *, now: datetime) -> dict[str, dict]:
    current = _aware(now)
    result = {project["id"]: {"star_change_since_snapshot": None, "snapshot_interval_days": None,
                              "weekly_star_growth": None} for project in projects}
    if previous_snapshot is None:
        return result
    if not isinstance(previous_snapshot, dict) or not isinstance(previous_snapshot.get("stars"), dict):
        raise ValueError("Invalid project snapshot")
    observed = _date(previous_snapshot.get("observed_at"))
    days = (current - observed).total_seconds() / 86400
    if days <= 0:
        raise ValueError("Invalid project snapshot interval")
    for name, stars in previous_snapshot["stars"].items():
        if (not isinstance(name, str) or not name.startswith("github:") or not _NAME.fullmatch(name[7:])
                or type(stars) is not int or stars < 0):
            raise ValueError("Invalid project snapshot")
    baseline = {name.casefold(): stars for name, stars in previous_snapshot["stars"].items()}
    for project in projects:
        if project["id"].casefold() in baseline:
            change = project["stars"] - baseline[project["id"].casefold()]
            result[project["id"]] = {"star_change_since_snapshot": change,
                                      "snapshot_interval_days": round(days, 3),
                                      "weekly_star_growth": change if 6.5 <= days <= 7.5 else None}
    return result


def fetch_projects(*, now: datetime, limit: int = MAX_PROJECTS, token: str | None = None,
                   previous_snapshot: dict | None = None,
                   seen_ids: set[str] | None = None) -> ProjectFetchResult:
    """Read at most three searches and twenty READMEs, at most two concurrently.

    Public metadata and a recognized license are required. README evidence is a
    learning aid, not a claim that the project was installed or successfully run.
    Previously sent GitHub IDs are compared without case distinctions and
    excluded before the README budget is allocated to the remaining candidates.
    """
    current = _aware(now)
    if type(limit) is not int or not 1 <= limit <= MAX_PROJECTS:
        raise ValueError("GitHub project limit must be between 1 and 20")
    if token is not None and (not isinstance(token, str) or not token.strip()
                              or any(char.isspace() for char in token)):
        raise ValueError("GitHub token configuration is invalid")
    if seen_ids is not None and not isinstance(seen_ids, set):
        raise ValueError("GitHub history must be a set of repository IDs")
    seen = set()
    for project_id in seen_ids or set():
        if (not isinstance(project_id, str) or project_id[:7].casefold() != "github:"
                or not _NAME.fullmatch(project_id[7:])):
            raise ValueError("Invalid GitHub history repository ID")
        canonical_repository_url("https://github.com/" + project_id[7:])
        seen.add(project_id.casefold())
    # Validate an optional baseline before any external requests.
    compute_growth([], previous_snapshot, now=current)
    client = _Client(token)
    active = (current - timedelta(days=7)).date().isoformat()
    recent = (current - timedelta(days=90)).date().isoformat()
    common = f"is:public archived:false fork:false pushed:>={active}"
    searches = [f"topic:ai-agents stars:>=100 {common}",
                f"topic:automation stars:>=50 {common}",
                f"created:>={recent} stars:>=50 {common}"]
    candidates: dict[str, dict] = {}
    for query in searches:
        response = client.get("/search/repositories", {"q": query, "sort": "stars", "order": "desc", "per_page": 30})
        if response is None:
            continue
        items = response.get("items")
        if not isinstance(items, list):
            client.count("invalid_search_results")
            continue
        client.count("searches_succeeded")
        if response.get("incomplete_results") is True:
            client.count("search_incomplete_results")
        client.count("repositories_received", min(len(items), 30))
        for item in items[:30]:
            project = _metadata(item, now=current, client=client)
            if project is not None:
                candidates.setdefault(project["id"], project)
    unseen = [project for project in candidates.values() if project["id"] not in seen]
    client.count("excluded_previously_seen", len(candidates) - len(unseen))
    ranked = sorted(unseen, key=lambda item: (-item["practical_project_priority"],
                                             -item["popularity_proxy"]["score"], item["id"]))
    with ThreadPoolExecutor(max_workers=2) as executor:
        projects = [item for item in executor.map(lambda project: _readme(project, client=client), ranked[:limit])
                    if item is not None]
    growth = compute_growth(projects, previous_snapshot, now=current)
    for project in projects:
        project.update(growth[project["id"]])
    projects.sort(key=lambda item: (-item["practical_project_priority"], -item["learning_readiness_score"],
                                    -item["popularity_proxy"]["score"], item["id"]))
    client.count("projects_verified", len(projects))
    return ProjectFetchResult(projects, dict(client.diagnostics))
