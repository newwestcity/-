"""One bounded source-availability check; no AI or email calls."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest.github_projects import fetch_projects
from x_digest.app import load_settings
from x_digest.mail import build_reading_message
from x_digest.reading import add_xueqiu_links
from x_digest.search_feed import discover_xueqiu_feed
from x_digest.xueqiu import fetch_articles


def main():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    github = fetch_projects(now=now, limit=3, token=os.environ.get("GH_TOKEN") or None)
    feed = discover_xueqiu_feed(now=now, seen_ids=set())
    snow = fetch_articles(feed.urls or ["https://xueqiu.com/1784314127/367934699"], now=now)
    links = add_xueqiu_links([], candidates=feed.candidates, outcomes=snow.outcomes,
                            seen=set(), settings=load_settings(Path("settings.json")))
    preview = Path(".local/preview")
    preview.mkdir(parents=True, exist_ok=True)
    if links:
        message = build_reading_message(links, edition="daily", date=now.date().isoformat(),
            sender="preview@example.invalid", recipient="preview@example.invalid",
            coverage={"source_status": {"X": "not_run", "雪球": "links_only"}})
        (preview / "snowball-source.html").write_text(
            message.get_body(preferencelist=("html",)).get_content(), encoding="utf-8")
    report = {"github": {"verified_projects": len(github.projects), "diagnostics": github.diagnostics},
              "xueqiu": {"verified_articles": len(snow.articles), "link_candidates": len(links),
                         "diagnostics": snow.diagnostics, "public_search": feed.diagnostics},
              "model_called": False, "mail_sent": False}
    (preview / "source-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('::notice title=Source access report::' + json.dumps(report, separators=(',', ':')))
    for item in links:
        value = json.dumps({"title": item["title"], "url": item["url"], "full_text_read": False}, ensure_ascii=False)
        print('::notice title=Snowball search title::' + value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'))
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write("```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")
    return 0 if github.projects and (snow.articles or links) else 1


if __name__ == "__main__":
    raise SystemExit(main())
