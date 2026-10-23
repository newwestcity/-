"""Inspect two official anonymous timeline widgets without model or mail calls.

Only actual anchor permalinks or explicitly labelled tweet objects can provide
post identities. JSON shape diagnostics are retained before assuming any SSR
schema. The returned posts are unread candidates, not reading recommendations.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
from pathlib import Path
import ssl
import sys
import time
from urllib import error, request
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.probe_public_search import NoRedirects, SearchHTML, clean
from x_digest.selection import canonical_post_url


MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 25
HANDLES = ("OpenAI", "naval")
_EPOCH_MILLISECONDS = 1288834974657


class TimelineHTML(SearchHTML):
    def __init__(self):
        super().__init__()
        self.capture_next = False
        self.next_parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.capture_next = True
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture_next = False
        super().handle_endtag(tag)

    def handle_data(self, data):
        if self.capture_next:
            self.next_parts.append(data)
        super().handle_data(data)


def read_bounded(response, deadline):
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        raise ValueError
    reader = getattr(response, "read1", response.read)
    parts, total = [], 0
    while total <= MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        part = reader(min(32768, MAX_BYTES + 1 - total))
        if not part:
            break
        parts.append(part)
        total += len(part)
    if total > MAX_BYTES:
        raise ValueError
    return b"".join(parts)


def identity(value, *, now, basis, headline="", expected_id=None):
    try:
        canonical, post_id, author = canonical_post_url(value)
        if expected_id is not None and expected_id != post_id:
            return None
        published = datetime.fromtimestamp(((int(post_id) >> 22) + _EPOCH_MILLISECONDS) / 1000, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return {"url": canonical, "id": post_id, "author": author, "published_at": published.isoformat(),
            "recent_7d": now - timedelta(days=7) <= published <= now,
            "future": published > now + timedelta(minutes=5), "basis": basis, "headline": clean(headline, 100)}


def inspect_ssr(value, *, now):
    """Inspect observed keys; only explicit tweet records supply permalinks."""
    shapes, accepted, count = [], [], 0

    def visit(node, path, *, tweet=False, depth=0):
        nonlocal count
        count += 1
        if count > 10000 or depth > 18:
            return
        if isinstance(node, dict):
            explicit = tweet or node.get("type") == "tweet"
            if explicit:
                if len(shapes) < 8:
                    shape = {"path": path, "keys": [clean(str(key), 60) for key in list(node)[:30]],
                             "has_id_str": isinstance(node.get("id_str"), str),
                             "has_permalink": isinstance(node.get("permalink"), str)}
                    user = node.get("user")
                    if isinstance(user, dict):
                        shape["user_keys"] = [clean(str(key), 60) for key in list(user)[:20]]
                    shapes.append(shape)
                permalink, post_id = node.get("permalink"), node.get("id_str")
                if isinstance(permalink, str) and (post_id is None or isinstance(post_id, str)):
                    headline = node.get("full_text") or node.get("text") or ""
                    if not isinstance(headline, str):
                        headline = ""
                    record = identity(permalink, now=now, basis="explicit_ssr_tweet", headline=headline,
                                      expected_id=post_id)
                    if record:
                        accepted.append(record)
            for key, child in list(node.items())[:100]:
                if isinstance(child, (dict, list)):
                    visit(child, path + "." + clean(str(key), 60), tweet=key == "tweet", depth=depth + 1)
        elif isinstance(node, list):
            for index, child in enumerate(node[:300]):
                if isinstance(child, (dict, list)):
                    visit(child, path + "[" + str(index) + "]", depth=depth + 1)

    visit(value, "$", depth=0)
    keys = [clean(str(key), 60) for key in list(value)[:30]] if isinstance(value, dict) else []
    return accepted, {"root_type": type(value).__name__, "root_keys": keys,
                      "nodes_examined": min(count, 10001), "tweet_shapes": shapes}


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def run_probe(handle):
    endpoint = "https://syndication.twitter.com/srv/timeline-profile/screen-name/" + handle
    now = datetime.now(timezone.utc)
    started = time.monotonic()
    report = {"handle": handle, "http_status": 0, "challenge": False, "anchor_count": 0,
              "next_data_present": False, "candidate_count": 0, "recent_7d_count": 0,
              "future_count": 0, "examples": [], "full_text_read": False}
    try:
        opener = request.build_opener(NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
        req = request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html",
                                                "Accept-Encoding": "identity"})
        with opener.open(req, timeout=TIMEOUT) as response:
            report["http_status"] = response.status
            raw = read_bounded(response, started + TIMEOUT)
        parser = TimelineHTML()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.finish()
        report["page_title"] = clean(" ".join(parser.title_parts), 120)
        report["challenge"] = parser.challenged()
        report["anchor_count"] = len(parser.anchors)
        report["next_data_present"] = bool(parser.next_parts)
        if not report["challenge"]:
            candidates = {}
            for href, title in parser.anchors:
                record = identity(urljoin(endpoint, href), now=now, basis="official_html_anchor", headline=title)
                if record:
                    candidates[record["id"]] = record
            if parser.next_parts:
                try:
                    value = json.loads("".join(parser.next_parts), object_pairs_hook=_pairs)
                    records, shape = inspect_ssr(value, now=now)
                    report["next_data_shape"] = shape
                    for record in records:
                        candidates[record["id"]] = record
                except (ValueError, TypeError, RecursionError):
                    report["next_data_error"] = "invalid_json"
            values = sorted(candidates.values(), key=lambda item: int(item["id"]), reverse=True)
            report["candidate_count"] = len(values)
            report["recent_7d_count"] = sum(item["recent_7d"] for item in values)
            report["future_count"] = sum(item["future"] for item in values)
            report["examples"] = values[:10]
    except error.HTTPError as exc:
        report["http_status"] = exc.code
        report["error"] = "http_rejected"
        exc.close()
    except (TimeoutError, error.URLError) as exc:
        report["error"] = "timeout" if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError) else "network"
    except (OSError, HTTPException):
        report["error"] = "network"
    except ValueError:
        report["error"] = "response_rejected"
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


def main():
    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(run_probe, HANDLES))
    result = {"reports": reports, "request_limit": 2, "model_called": False, "mail_sent": False}
    directory = Path(".local")
    directory.mkdir(exist_ok=True)
    (directory / "official-timeline-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for report in reports:
        value = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        print("::notice title=Official timeline identity candidates (unread)::" + value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
