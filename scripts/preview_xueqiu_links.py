"""Live check of the explicitly approved title/link fallback; never sends mail."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.preview_reading import notice
from x_digest.app import check_environment, load_settings
from x_digest.discovery import discover_xueqiu_sources as discover_xueqiu
from x_digest.mail import build_reading_message
from x_digest.reading import add_xueqiu_links, _credentials
from x_digest import reading_state
from x_digest.xueqiu import fetch_articles


def main():
    settings = load_settings(Path("settings.json"))
    if check_environment(send=False):
        return 2
    now = datetime.now(ZoneInfo(settings["timezone"]))
    state = reading_state.load_state(Path(".local/reading-state.json"), legacy_path=Path(".local/state.json"))
    seen = reading_state.seen_ids(state, today=now.date(), history_days=90)
    result = discover_xueqiu(**_credentials(), now=now, seen_ids=seen)
    fetched = fetch_articles(result.urls, now=now)
    items = add_xueqiu_links([], candidates=result.candidates, outcomes=fetched.outcomes,
                            seen=seen, settings=settings)
    report = {"mode": "xueqiu_title_links", "candidate_count": len(items), "mail_sent": False,
              "discovery": result.diagnostics, "fetch": fetched.diagnostics}
    notice("Xueqiu title preview", report)
    preview = Path(".local/preview")
    preview.mkdir(parents=True, exist_ok=True)
    Path(".local/reading-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not items:
        return 1
    message = build_reading_message(items, edition="daily", date=now.date().isoformat(),
        sender="preview@example.invalid", recipient="preview@example.invalid",
        coverage={"source_status": {"X": "not_run", "雪球": "links_only"}})
    (preview / "digest.html").write_text(message.get_body(preferencelist=("html",)).get_content(), encoding="utf-8")
    for item in items:
        notice("Xueqiu title candidate", {"url": item["url"], "title": item["title"],
                                          "full_text_read": False, "depth_scored": False})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
