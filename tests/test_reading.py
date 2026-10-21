"""Reading-edition policies and delivery effects, using synthetic local sources."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import smtplib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from x_digest.app import ConfigurationError, load_settings
from x_digest.provider import ProviderError
from x_digest import reading, reading_state


NOW = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
ENV = {"DIGEST_API_BASE": "https://api.example.test/v1", "DIGEST_API_KEY": "test-placeholder-not-live",
       "DIGEST_MODEL": "test-model", "SMTP_USERNAME": "test@qq.com", "SMTP_AUTH_CODE": "test-only"}


def article(source="雪球", number=1, *, age=1, category=None, score=90):
    published = None if age is None else NOW - timedelta(days=age)
    if source == "X":
        post_id = str(((int(published.timestamp() * 1000) - 1288834974657) << 22) + number)
        identifier, author = "x:" + post_id, "reader" + str(number)
        url = f"https://x.com/{author}/status/{post_id}"
    else:
        identifier, author = "xueqiu:" + str(number), "雪球作者" + str(number)
        url = f"https://xueqiu.com/{1000 + number}/{number}"
    return {"id": identifier, "url": url, "author": author, "source": source,
            "author_id": str(1000 + number), "text": "具有具体论点和依据的合成测试正文。" * 60,
            "published_at": published.isoformat() if published else None,
            "verified_at": NOW.isoformat(), "category": category or ("投资" if source == "X" else "价值投资"),
            "title": "合成的阅读推荐", "summary": "仅用于测试的中文阅读介绍。", "reason": "测试具体学习收获。",
            "learning_points": ["理解测试中的方法", "检查测试中的假设"], "score_total": score,
            "is_long_read": True,
            "reading_kind": "undated" if published is None else ("classic" if age > 30 else "recent")}


def project(identifier="github:example/tool"):
    return {"id": identifier, "source": "GitHub", "author": identifier.split(":")[1].split("/")[0],
            "url": "https://github.com/" + identifier.split(":", 1)[1], "score_total": 90}


def edition_result(items):
    return items, {"targets": {}, "source_status": {}}, {"synthetic_fixture": 1}


def message_fixture(*args, **kwargs):
    message = EmailMessage()
    message.set_content("合成的纯文本邮件")
    message.add_alternative("<p>合成的阅读邮件</p>", subtype="html")
    return message


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(Path(__file__).resolve().parents[1] / "settings.json")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        for patcher in [patch.dict("os.environ", ENV, clear=True),
                        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network in tests"))]:
            patcher.start()
            self.addCleanup(patcher.stop)
        build_patch = patch("x_digest.reading.build_reading_message", side_effect=message_fixture)
        self.build = build_patch.start()
        self.addCleanup(build_patch.stop)
        send_patch = patch("x_digest.reading.send_message")
        self.send = send_patch.start()
        self.addCleanup(send_patch.stop)

    def run_edition(self, *, send=False, edition="daily", now=NOW):
        return reading.run_reading(self.settings, work_dir=self.work, send=send, edition=edition, now=now)

    def test_daily_verifies_both_sources_and_shares_classic_slots_including_unknown_dates(self):
        xs = [article("X", 1, age=1, category="AI"), article("X", 2, age=60, category="投资", score=82),
              article("X", 3, age=61, category="投资", score=81),
              article("X", 4, age=62, category="投资", score=80)]
        snow = [article(number=10, age=1), article(number=11, age=None, score=99),
                article(number=12, age=None, score=98), article(number=13, age=60, score=97),
                article(number=14, age=61, score=96)]
        def edit(**kwargs):
            # The editor can only choose from each supplied verified source pool.
            return [{key: value for key, value in item.items() if key != "text"} for item in kwargs["items"]]
        with patch("x_digest.reading.search_by_category", return_value=SimpleNamespace(cited_urls=[p["url"] for p in xs])), \
             patch("x_digest.reading.fetch_verified_posts", return_value=xs), \
             patch("x_digest.reading.discover_xueqiu", return_value=SimpleNamespace(
                 urls=[p["url"] for p in snow], diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.fetch_articles", return_value=SimpleNamespace(articles=snow, diagnostics={})), \
             patch("x_digest.reading.editorial.curate_articles", side_effect=edit):
            result = self.run_edition()
        items = json.loads((self.work / "preview/digest.json").read_text())["items"]
        self.assertEqual(result["source_counts"].keys(), {"X", "雪球"})
        self.assertEqual(sum(p["reading_kind"] != "recent" for p in items), 3)
        self.assertEqual(sum(p["reading_kind"] == "undated" for p in items), 1)
        self.assertTrue({xs[0]["id"], snow[0]["id"]} <= {p["id"] for p in items})
        self.assertTrue({p["id"] for p in items} <= {p["id"] for p in xs + snow})
        self.send.assert_not_called()

    def test_already_sent_returns_before_credentials_collectors_or_mail(self):
        with patch("x_digest.reading._daily", return_value=edition_result([article()])) as daily:
            self.assertEqual(self.run_edition(send=True)["status"], "smtp_accepted")
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(self.run_edition(send=True)["status"], "already_sent")
        daily.assert_called_once()
        self.send.assert_called_once()
        self.build.assert_called_once()

    def test_invalid_mail_configuration_stops_before_collectors_and_editors(self):
        for update in [{"MAIL_TO": "not-a-mailbox"}, {"SMTP_USERNAME": "reader@qq.com\r\nBcc: other@test.com"},
                       {"SMTP_AUTH_CODE": ""}]:
            with self.subTest(update=update), patch.dict("os.environ", update), \
                 patch("x_digest.reading._daily") as daily, patch("x_digest.reading._weekly") as weekly, \
                 patch("x_digest.reading.editorial.curate_articles") as edit:
                with self.assertRaises((ConfigurationError, ValueError)):
                    self.run_edition(send=True)
                daily.assert_not_called()
                weekly.assert_not_called()
                edit.assert_not_called()
        self.send.assert_not_called()

    def test_smtp_failure_preserves_existing_state_and_does_not_consume_new_items(self):
        original = reading_state.record_delivery(self.work / "reading-state.json", {"version": 2, "deliveries": []},
            edition="daily", period="2026-10-19", date="2026-10-19", item_ids=["x:101"],
            sent_at="2026-10-19T08:00:00+08:00")
        before = (self.work / "reading-state.json").read_bytes()
        self.send.side_effect = smtplib.SMTPException("synthetic SMTP refusal")
        with patch("x_digest.reading._daily", return_value=edition_result([article()])):
            with self.assertRaises(smtplib.SMTPException):
                self.run_edition(send=True)
        self.assertEqual((self.work / "reading-state.json").read_bytes(), before)
        self.assertEqual(reading_state.load_state(self.work / "reading-state.json"), original)
        self.assertNotIn("xueqiu:1", reading_state.seen_ids(original, today=NOW.date()))

    def test_preview_is_reviewable_without_sending_or_delivery_history(self):
        with patch("x_digest.reading._daily", return_value=edition_result([article()])):
            result = self.run_edition()
        self.assertEqual(result["status"], "preview")
        self.assertTrue((self.work / "preview/digest.html").exists())
        self.assertTrue((self.work / "reading-audit.json").exists())
        self.assertFalse((self.work / "reading-state.json").exists())
        self.send.assert_not_called()

    def test_sunday_daily_and_weekly_send_independently_but_weekly_only_once_per_iso_week(self):
        with patch("x_digest.reading._daily", return_value=edition_result([article()])) as daily, \
             patch("x_digest.reading._weekly", return_value=edition_result([project()])) as weekly:
            first = self.run_edition(send=True, now=SUNDAY)
            second = self.run_edition(send=True, edition="weekly", now=SUNDAY)
            repeat = self.run_edition(send=True, edition="weekly", now=SUNDAY - timedelta(days=2))
        self.assertEqual([first["status"], second["status"], repeat["status"]],
                         ["smtp_accepted", "smtp_accepted", "already_sent"])
        self.assertEqual(repeat["period"], "2026-W42")
        self.assertEqual(self.send.call_count, 2)
        daily.assert_called_once()
        weekly.assert_called_once()
        saved = reading_state.load_state(self.work / "reading-state.json")
        self.assertTrue(reading_state.already_sent(saved, edition="daily", period="2026-10-18"))
        self.assertTrue(reading_state.already_sent(saved, edition="weekly", period="2026-W42"))

    def test_legacy_x_history_reaches_new_daily_dedup_without_marking_current_edition_sent(self):
        legacy = {"version": 1, "deliveries": [{"date": "2026-10-20", "post_ids": ["1812344441312813407"],
                                                "sent_at": "2026-10-20T08:00:00+08:00"}]}
        (self.work / "state.json").write_text(json.dumps(legacy))
        def daily(settings, seen, now):
            self.assertIn("x:1812344441312813407", seen)
            return edition_result([article()])
        with patch("x_digest.reading._daily", side_effect=daily) as collect:
            self.assertEqual(self.run_edition()["status"], "preview")
        collect.assert_called_once()
        self.assertEqual(json.loads((self.work / "state.json").read_text()), legacy)
        self.assertFalse((self.work / "reading-state.json").exists())

    def test_weekly_excludes_previously_sent_github_identity_despite_case_changes(self):
        history = reading_state.record_delivery(self.work / "reading-state.json", {"version": 2, "deliveries": []},
            edition="weekly", period="2026-W42", date="2026-10-18", item_ids=["github:OWNER/Existing"],
            sent_at="2026-10-18T08:00:00+08:00")
        existing, fresh = project("github:owner/existing"), project("github:newowner/Fresh")
        def curate(**kwargs):
            self.assertEqual([p["id"] for p in kwargs["items"]], [fresh["id"]])
            return kwargs["items"]
        with patch("x_digest.reading.fetch_projects", return_value=SimpleNamespace(
                 projects=[existing, fresh], diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.editorial.curate_projects", side_effect=curate) as edit:
            result = self.run_edition(edition="weekly")
        self.assertEqual(result["count"], 1)
        edit.assert_called_once()
        items = json.loads((self.work / "preview/digest.json").read_text())["items"]
        self.assertEqual(items[0]["id"], "github:newowner/fresh")
        self.assertEqual(reading_state.load_state(self.work / "reading-state.json"), history)

    def test_daily_x_search_failure_can_send_verified_snow_without_inventing_x_items(self):
        snow = article()
        with patch("x_digest.reading.search_by_category", side_effect=ProviderError("synthetic search failure")), \
             patch("x_digest.reading.fetch_verified_posts") as xfetch, \
             patch("x_digest.reading.discover_xueqiu", return_value=SimpleNamespace(
                 urls=[snow["url"]], diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.fetch_articles", return_value=SimpleNamespace(articles=[snow], diagnostics={})), \
             patch("x_digest.reading.editorial.curate_articles", return_value=[snow]) as edit:
            result = self.run_edition(send=True)
        self.assertEqual(result["source_counts"], {"雪球": 1})
        self.assertEqual(edit.call_args.kwargs["source"], "雪球")
        xfetch.assert_not_called()
        self.assertEqual(self.build.call_args.kwargs["coverage"]["source_status"]["X"], "unavailable")
        self.assertEqual(reading_state.seen_ids(reading_state.load_state(self.work / "reading-state.json"),
                                             today=NOW.date()), {snow["id"]})

    def test_daily_empty_editor_result_never_uses_discovery_summary_as_fallback(self):
        snow, x = article(), article("X", category="AI")
        generated = {"summary": "未经核实、绝不应出现在邮件里的搜索摘要"}
        with patch("x_digest.reading.search_by_category", return_value=SimpleNamespace(
                 cited_urls=[x["url"]], posts=[generated])), \
             patch("x_digest.reading.fetch_verified_posts", return_value=[x]), \
             patch("x_digest.reading.discover_xueqiu", return_value=SimpleNamespace(
                 urls=[snow["url"]], diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.fetch_articles", return_value=SimpleNamespace(articles=[snow], diagnostics={})), \
             patch("x_digest.reading.editorial.curate_articles", return_value=[]) as edit:
            with self.assertRaisesRegex(ConfigurationError, "没有通过"):
                self.run_edition(send=True)
        self.assertEqual(edit.call_count, 2)
        self.build.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse((self.work / "reading-state.json").exists())
        self.assertFalse((self.work / "preview/digest.json").exists())


if __name__ == "__main__":
    unittest.main()
