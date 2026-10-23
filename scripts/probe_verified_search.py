"""Two serial, explicitly billed source probes using a catalog-verified route."""
from datetime import datetime, timedelta, timezone
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
from urllib import error, request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_connection import _request_error_details
from scripts.probe_search_routes import notice
from x_digest.discovery import _metadata_records, _title
from x_digest.provider import _NoRedirects, validate_configuration
from x_digest.selection import canonical_post_url
from x_digest.xueqiu import canonical_article_url


def main():
    base, key = os.environ["DIGEST_API_BASE"], os.environ["DIGEST_API_KEY"]
    model = os.environ.get("DIGEST_SEARCH_MODEL", "gpt-5.6-luna")
    endpoint = validate_configuration(base_url=base, api_key=key, model=model)
    opener = request.build_opener(_NoRedirects())
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"}
    with opener.open(request.Request(base.rstrip('/') + '/models', headers=headers), timeout=25) as response:
        catalog = json.loads(response.read(2 * 1024 * 1024))
    assert any(row.get('id') == model and 'openai-response' in row.get('supported_endpoint_types', [])
               for row in catalog.get('data', []) if isinstance(row, dict)), 'Search model is not listed for Responses'
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    queries = [('X', 'x.com', 'site:x.com AI agents tutorial thread after:' +
                (now - timedelta(days=7)).date().isoformat() + ' before:' + tomorrow),
               ('雪球', 'xueqiu.com', 'site:xueqiu.com 价值投资 企业分析 财报 after:' +
                (now - timedelta(days=30)).date().isoformat() + ' before:' + tomorrow)]
    if os.environ.get('PROBE_SOURCE') == 'X':
        queries = [('X', 'x.com', 'site:x.com inurl:status (value investing OR margin of safety OR book review) before:' + tomorrow)]
    reports = []
    for source, domain, query in queries:
        started = time.monotonic()
        payload = {"model": model, "store": False,
                   "input": f"现在{now.isoformat()}。只实际执行一次搜索：{query}。找3至5篇适合认真阅读的原帖，给工具引用和原文链接。"
                            "X只接受作者/status/数字ID，雪球只接受数字作者ID/数字文章ID。不要编造，不输出摘要，只列搜索得到的原文链接和标题。",
                   "tools": [{"type": "web_search", "filters": {"allowed_domains": [domain]}}],
                   "tool_choice": "required", "include": ["web_search_call.action.sources"], "max_output_tokens": 3000}
        report = {"source": source, "model": model, "http_status": 0}
        try:
            req = request.Request(endpoint, headers=headers, method='POST', data=json.dumps(payload).encode())
            with opener.open(req, timeout=180) as response:
                report['http_status'] = response.status
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            data = json.loads(raw)
            records, report['search_status'] = _metadata_records(data)
            report['metadata_records'] = len(records)
            hosts = Counter()
            samples = []
            for record in records[:30]:
                try:
                    parts = urlsplit(record.get('url', ''))
                    if parts.scheme == 'https' and parts.hostname and parts.username is None:
                        hosts[parts.hostname] += 1
                        samples.append({'host': parts.hostname, 'path': parts.path[:200],
                                        'title': _title(record.get('title'))[:100]})
                except (ValueError, TypeError):
                    pass
            report['metadata_hosts'] = dict(hosts)
            report['metadata_samples'] = samples[:15]
            accepted = {}
            for row in records:
                try:
                    if source == 'X':
                        url, identity, _ = canonical_post_url(row['url'])
                        date = datetime.fromtimestamp(((int(identity) >> 22) + 1288834974657) / 1000, timezone.utc)
                    else:
                        url, _, identity = canonical_article_url(row['url'])
                        date = None
                    title = _title(row.get('title'))[:180] or accepted.get(url, {}).get('title', '')
                    accepted[url] = {"url": url, "title": title,
                                     "published_at": date.isoformat() if date else None,
                                     "recent": (now - timedelta(days=7) <= date <= now + timedelta(minutes=5)) if date else None}
                except (KeyError, ValueError, TypeError, OverflowError, OSError):
                    continue
            report['candidate_count'] = len(accepted)
            report['candidates'] = list(accepted.values())[:10]
        except error.HTTPError as exc:
            report['http_status'] = exc.code
            report.update(_request_error_details(exc, (key, base)))
            exc.close()
        except TimeoutError:
            report['failure'] = 'timeout'
        except Exception:
            report['failure'] = 'network_or_invalid_response'
        report['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        reports.append(report)
        notice('Verified Responses source probe', report)
    Path('.local').mkdir(exist_ok=True)
    Path('.local/verified-search-probe.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
