"""Four bounded, manual X-source probes without credentials or model calls.

Mirror RSS provides candidate identities only. Any later reading recommendation
must independently verify the original post; RSS descriptions are never used
as article bodies here.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
from pathlib import Path
import ssl
import sys
import time
from urllib import error, request
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.probe_public_search import NoRedirects, SearchHTML, clean, read_bounded, unwrap
from x_digest.search_feed import _FeedError, _parse
from x_digest.selection import canonical_post_url


TIMEOUT = 25
WIDGET_ID = "2052776296422211942"
MIRROR_HOSTS = {"nitter.net", "nitter.privacydev.net"}
ORIGINAL_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
PROBES = (
    ("nitter_primary", "https://nitter.net/OpenAI/rss", "rss"),
    ("nitter_privacydev", "https://nitter.privacydev.net/OpenAI/rss", "rss"),
    ("official_tweet_widget", "https://cdn.syndication.twimg.com/tweet-result?id=" + WIDGET_ID
     + "&lang=en&token=4z4zj2bacm8", "widget"),
    ("bing_rss_english", "https://www.bing.com/search?" + urlencode({
        "format": "rss", "q": 'site:twitter.com inurl:status "value investing"',
        "mkt": "en-US", "setlang": "en-US"}), "rss"),
)


def post_identity(value, *, now, base):
    """Map only known mirror post paths, never arbitrary numbers or prose."""
    value = unwrap(value, base)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.port is not None):
            return None
        mapped = parsed.netloc.casefold() in MIRROR_HOSTS
        if mapped:
            value = parsed._replace(scheme="https", netloc="x.com", query="", fragment="").geturl()
        elif parsed.netloc.casefold() in ORIGINAL_HOSTS and parsed.scheme == "http":
            value = parsed._replace(scheme="https").geturl()
        url, post_id, _ = canonical_post_url(value)
        published = datetime.fromtimestamp(((int(post_id) >> 22) + 1288834974657) / 1000, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return {"url": url, "id": post_id, "published_at": published.isoformat(),
            "recent_7d": now - timedelta(days=7) <= published <= now, "mirror_candidate": mapped}


def widget_summary(value, *, now):
    if not isinstance(value, dict):
        return {"json_root": type(value).__name__}, []
    summary = {"json_root": "dict", "root_keys": [clean(str(key), 60) for key in list(value)[:30]],
               "has_note_tweet": "note_tweet" in value,
               "has_note_tweet_results": "note_tweet_results" in value}
    post_id = value.get("id_str")
    user = value.get("user")
    author = user.get("screen_name") if isinstance(user, dict) else None
    text = value.get("text")
    if isinstance(post_id, str) and post_id.isascii() and post_id.isdigit() and len(post_id) <= 25:
        summary["id_str"] = post_id
    if isinstance(author, str) and 1 <= len(author) <= 15 and all(char.isascii() and (char.isalnum() or char == "_") for char in author):
        summary["screen_name"] = author
    else:
        author = None
    summary["text_characters"] = len(text) if isinstance(text, str) else 0
    if post_id != WIDGET_ID or author is None:
        return summary, []
    record = post_identity("https://x.com/" + author + "/status/" + post_id, now=now,
                           base="https://cdn.syndication.twimg.com/")
    return summary, [record] if record else []


def redirect_identity(location, base):
    if not isinstance(location, str):
        return {}
    from urllib.parse import urljoin
    try:
        parsed = urlsplit(urljoin(base, location))
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return {}
        return {"redirect_host": clean(parsed.hostname, 253), "redirect_path": clean(parsed.path or "/", 240)}
    except ValueError:
        return {}


def run_probe(probe):
    name, endpoint, kind = probe
    now, started = datetime.now(timezone.utc), time.monotonic()
    report = {"source": name, "http_status": 0, "challenge": False, "links_total": 0,
              "canonical_count": 0, "recent_7d_count": 0, "mirror_candidate_count": 0,
              "result_hosts": {}, "examples": [], "content_reviewed": False}
    try:
        opener = request.build_opener(NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
        req = request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json, application/rss+xml, text/xml, text/html",
                                                "Accept-Encoding": "identity"})
        with opener.open(req, timeout=TIMEOUT) as response:
            report["http_status"] = response.status
            raw = read_bounded(response, started + TIMEOUT)  # Shared helper: 1 MiB hard limit.
        parser = SearchHTML()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.finish()
        report["challenge"] = parser.challenged()
        records = []
        if not report["challenge"]:
            if kind == "widget":
                try:
                    summary, records = widget_summary(json.loads(raw.decode("utf-8")), now=now)
                    report["widget"] = summary
                except (ValueError, UnicodeError, RecursionError):
                    report["error"] = "invalid_json"
            else:
                try:
                    links = _parse(raw)  # Rejects DTD, entities, NULs and non-RSS HTML.
                    report["links_total"] = len(links)
                    hosts = {}
                    for link in links:
                        try:
                            host = urlsplit(link["url"]).hostname
                        except ValueError:
                            host = None
                        if isinstance(host, str):
                            host = clean(host.casefold(), 253)
                            hosts[host] = hosts.get(host, 0) + 1
                        record = post_identity(link["url"], now=now, base=endpoint)
                        if record:
                            records.append(record)
                    report["result_hosts"] = dict(sorted(hosts.items(), key=lambda pair: -pair[1])[:12])
                except (_FeedError, ValueError):
                    report["error"] = "invalid_or_unsafe_rss"
            unique = {record["id"]: record for record in records}
            values = sorted(unique.values(), key=lambda item: int(item["id"]), reverse=True)
            report["canonical_count"] = len(values)
            report["recent_7d_count"] = sum(record["recent_7d"] for record in values)
            report["mirror_candidate_count"] = sum(record["mirror_candidate"] for record in values)
            report["examples"] = values[:5]
    except error.HTTPError as exc:
        report["http_status"] = exc.code
        report["error"] = "http_rejected"
        if 300 <= exc.code < 400:
            report.update(redirect_identity(exc.headers.get("Location"), endpoint))
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
        reports = list(executor.map(run_probe, PROBES))
    result = {"reports": reports, "request_limit": 4, "model_called": False, "mail_sent": False}
    directory = Path(".local")
    directory.mkdir(exist_ok=True)
    (directory / "x-alternatives-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for report in reports:
        value = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        print("::notice title=X alternative identity candidates (unread)::" + value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
