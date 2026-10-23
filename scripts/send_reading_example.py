"""Send the two already-generated, verified-provenance title links once."""
from datetime import datetime
from email.utils import format_datetime, make_msgid
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest.app import delivery_lock, load_settings
from x_digest.mail import build_reading_message, send_message
from x_digest.reading import add_xueqiu_links
from x_digest import reading_state
from x_digest.xueqiu import canonical_article_url

EXPECTED = {"https://xueqiu.com/3236455484/410628998", "https://xueqiu.com/1965756146/410590217"}


class Titles(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading, self.link, self.parts, self.items = False, None, [], []

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.heading = True
        if self.heading and tag == "a":
            self.link, self.parts = dict(attrs).get("href"), []

    def handle_data(self, value):
        if self.heading and self.link:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if tag == "a" and self.heading and self.link:
            url, _, article_id = canonical_article_url(self.link)
            self.items.append({"id": "xueqiu:" + article_id, "url": url, "title": "".join(self.parts),
                               "discovery_basis": "search_metadata"})
            self.link = None
        if tag == "h3":
            self.heading = False


def run():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    date = now.date().isoformat()
    with delivery_lock(Path(".local/run.lock")):
        state_path = Path(".local/reading-state.json")
        state = reading_state.load_state(state_path, legacy_path=Path(".local/state.json"))
        if reading_state.already_sent(state, edition="daily", period=date):
            return {"status": "already_sent", "date": date}
        paths = list(Path(".local/sample").rglob("digest.html"))
        audits = list(Path(".local/sample").rglob("reading-audit.json"))
        if len(paths) != 1 or len(audits) != 1 or paths[0].stat().st_size > 65536:
            raise ValueError("Unexpected example artifact")
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        if audit.get("candidate_count") != 2 or audit.get("mail_sent") is not False:
            raise ValueError("Unexpected example provenance")
        parser = Titles()
        parser.feed(paths[0].read_text(encoding="utf-8"))
        if len(parser.items) != 2 or {p["url"] for p in parser.items} != EXPECTED:
            raise ValueError("Unexpected example source links")
        seen = reading_state.seen_ids(state, today=now.date(), history_days=90)
        items = add_xueqiu_links([], candidates=parser.items, outcomes={url: "access_challenge" for url in EXPECTED},
                                seen=seen, settings=load_settings(Path("settings.json")))
        if not items:
            return {"status": "already_read", "date": date}
        sender = os.environ["SMTP_USERNAME"]
        message = build_reading_message(items, edition="daily", date=date, sender=sender,
            recipient=os.environ.get("MAIL_TO") or sender,
            coverage={"source_status": {"X": "not_run", "雪球": "links_only"}})
        message.replace_header("Subject", "阅读推荐升级示范｜" + date + "｜" + str(len(items)) + "条雪球链接")
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        send_message(message, username=sender, password=os.environ["SMTP_AUTH_CODE"])
        reading_state.record_delivery(state_path, state, edition="daily", period=date, date=date,
            item_ids=[p["id"] for p in items], sent_at=now.isoformat(), history_days=180)
        return {"status": "smtp_accepted", "date": date, "count": len(items), "new_model_calls": 0}


if __name__ == "__main__":
    try:
        report = run()
        print('::notice title=Reading example delivery::' + json.dumps(report, separators=(',', ':')))
    except Exception:
        print("::error::Example delivery failed; check the mailbox before retrying if SMTP may have accepted it.")
        raise SystemExit(1)
