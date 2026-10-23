"""Reading-edition policies and delivery effects, using synthetic local sources."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from itertools import count
import json
from pathlib import Path
import smtplib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from x_digest.app import ConfigurationError, load_settings
from x_digest.editorial import EditorialError
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


def link_candidate(number=1, *, author=None, title="价值投资中的现金流分析方法"):
    return {"id": f"xueqiu:{number}", "url": f"https://xueqiu.com/{author or 1000 + number}/{number}",
            "title": title, "discovery_basis": "search_metadata", "discovered_at": NOW.isoformat()}


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

    def ledger(self, *, history=None):
        ledger = Mock()
        ledger.history = history or {"version": 2, "deliveries": []}
        ledger.pending_ids.return_value = set()
        return ledger

    def test_durable_history_recovers_corrupt_cache_and_skips_without_model_or_smtp(self):
        history = reading_state.record_delivery(self.work / "reading-state.json", {"version": 2, "deliveries": []},
            edition="daily", period="2026-10-20", date="2026-10-20", item_ids=["xueqiu:1"],
            sent_at=NOW.isoformat())
        (self.work / "reading-state.json").write_text("corrupt runner cache")
        ledger = self.ledger(history=history)
        with patch.dict("os.environ", {"DIGEST_REQUIRE_LEDGER": "true"}), \
             patch("x_digest.delivery_ledger.open_ledger", return_value=ledger) as open_remote, \
             patch("x_digest.reading._daily") as daily:
            result = self.run_edition(send=True)
        self.assertEqual(result["status"], "already_sent")
        self.assertFalse(open_remote.call_args.kwargs["allow_create"])
        self.assertEqual(reading_state.load_state(self.work / "reading-state.json"), history)
        daily.assert_not_called()
        self.send.assert_not_called()

    def test_durable_unconfirmed_send_blocks_before_paid_search(self):
        from x_digest.delivery_ledger import LedgerError
        ledger = self.ledger()
        ledger.check_available.side_effect = LedgerError("本期已有未确认的SMTP发送记录")
        with patch.dict("os.environ", {"DIGEST_REQUIRE_LEDGER": "true"}), \
             patch("x_digest.delivery_ledger.open_ledger", return_value=ledger), \
             patch("x_digest.reading._daily") as daily:
            with self.assertRaises(LedgerError):
                self.run_edition(send=True)
        daily.assert_not_called()
        self.send.assert_not_called()

    def test_durable_reservation_precedes_smtp_and_completion_follows_acceptance(self):
        ledger, effects = self.ledger(), []
        ledger.reserve.side_effect = lambda **kw: effects.append("reserve")
        self.send.side_effect = lambda *a, **kw: effects.append("smtp")
        ledger.complete.side_effect = lambda history: effects.append("complete")
        with patch.dict("os.environ", {"DIGEST_REQUIRE_LEDGER": "true"}), \
             patch("x_digest.delivery_ledger.open_ledger", return_value=ledger), \
             patch("x_digest.reading._daily", return_value=edition_result([article()])):
            self.assertEqual(self.run_edition(send=True)["status"], "smtp_accepted")
        self.assertEqual(effects, ["reserve", "smtp", "complete"])
        self.assertEqual(ledger.complete.call_args.args[0], reading_state.load_state(self.work / "reading-state.json"))

    def test_durable_explicit_smtp_rejection_releases_but_disconnect_keeps_reservation(self):
        for error, release in ((smtplib.SMTPAuthenticationError(535, b"refused"), True),
                               (smtplib.SMTPDataError(451, b"try later"), True),
                               (smtplib.SMTPServerDisconnected("ambiguous"), False),
                               (TimeoutError("ambiguous"), False)):
            with self.subTest(error=type(error).__name__):
                ledger = self.ledger()
                self.send.side_effect = error
                with patch.dict("os.environ", {"DIGEST_REQUIRE_LEDGER": "true"}), \
                     patch("x_digest.delivery_ledger.open_ledger", return_value=ledger), \
                     patch("x_digest.reading._daily", return_value=edition_result([article()])):
                    with self.assertRaises(type(error)):
                        self.run_edition(send=True)
                self.assertEqual(ledger.cancel.called, release)
                ledger.complete.assert_not_called()

    def test_preview_after_same_day_delivery_still_exercises_full_reading_pipeline(self):
        with patch("x_digest.reading._daily", return_value=edition_result([article()])) as daily:
            self.run_edition(send=True)
            self.assertEqual(self.run_edition()["status"], "preview")
        self.assertEqual(daily.call_count, 2)
        self.assertEqual(self.send.call_count, 1)

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

    def test_small_weekly_pool_needs_only_one_bounded_editor_call(self):
        for size in (1, 5):
            candidates = [project(f"github:owner{i}/tool") for i in range(size)]
            with self.subTest(size=size), \
                 patch("x_digest.reading.fetch_projects", return_value=SimpleNamespace(
                     projects=candidates, diagnostics={"searches_succeeded": 1})), \
                 patch("x_digest.reading.editorial.curate_projects", side_effect=lambda **kw: kw["items"][:5]) as edit:
                result = self.run_edition(edition="weekly")
                self.assertEqual(result["count"], size)
                edit.assert_called_once()
                self.assertEqual(edit.call_args.kwargs["target_count"], 5)
                self.assertEqual({p["id"] for p in edit.call_args.kwargs["items"]},
                                 {p["id"] for p in candidates})
        self.send.assert_not_called()
        self.assertFalse((self.work / "reading-state.json").exists())

    def test_large_weekly_pool_edits_each_candidate_once_in_two_small_batches(self):
        for size in (6, 17, 20):
            candidates = [project(f"github:owner{i // 2}/tool{i}") for i in range(size)]
            with self.subTest(size=size), \
                 patch("x_digest.reading.fetch_projects", return_value=SimpleNamespace(
                     projects=candidates, diagnostics={"searches_succeeded": 1})), \
                 patch("x_digest.reading.editorial.curate_projects", side_effect=lambda **kw: kw["items"][:5]) as edit:
                result = self.run_edition(edition="weekly")
                self.assertEqual(edit.call_count, 2)
                supplied = []
                for call in edit.call_args_list:
                    self.assertEqual(call.kwargs["target_count"], 5)
                    self.assertLessEqual(len(call.kwargs["items"]), 10)
                    supplied.extend(p["id"] for p in call.kwargs["items"])
                self.assertCountEqual(supplied, [p["id"] for p in candidates])
                self.assertEqual(len(supplied), len(set(supplied)))
                self.assertEqual(result["count"], min(10, size))
        self.send.assert_not_called()
        self.assertFalse((self.work / "reading-state.json").exists())

    def test_weekly_owner_limit_applies_before_paid_editing(self):
        candidates = [project(f"github:oneowner/tool{i}") for i in range(20)]
        with patch("x_digest.reading.fetch_projects", return_value=SimpleNamespace(
                 projects=candidates, diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.editorial.curate_projects", side_effect=lambda **kw: kw["items"][:5]) as edit:
            result = self.run_edition(edition="weekly")
        edit.assert_called_once()
        self.assertEqual(len(edit.call_args.kwargs["items"]), 2)
        self.assertEqual(result["count"], 2)

    def test_weekly_failed_batch_preserves_successful_batch_without_retries(self):
        candidates = [project(f"github:owner{i}/tool") for i in range(12)]
        attempts, survivors = count(), []
        def curate(**kwargs):
            if next(attempts) == 0:
                raise EditorialError("synthetic timeout", code="timeout")
            survivors.extend(kwargs["items"][:5])
            return kwargs["items"][:5]
        with patch("x_digest.reading.fetch_projects", return_value=SimpleNamespace(
                 projects=candidates, diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.editorial.curate_projects", side_effect=curate) as edit:
            result = self.run_edition(edition="weekly")
        self.assertEqual(edit.call_count, 2)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["count"], 5)
        content = json.loads((self.work / "preview/digest.json").read_text())
        self.assertEqual({p["id"] for p in content["items"]}, {p["id"].casefold() for p in survivors})
        self.assertEqual(content["coverage"]["source_status"]["GitHub"], "partial")
        audit = json.loads((self.work / "reading-audit.json").read_text())
        self.assertEqual(audit["diagnostics"]["editor_error"], "timeout")
        self.send.assert_not_called()
        self.assertFalse((self.work / "reading-state.json").exists())

    def test_weekly_all_batches_failed_leave_sanitized_diagnostics_without_mail_or_history(self):
        candidates = [project(f"github:owner{i}/tool") for i in range(12)]
        with patch("x_digest.reading.fetch_projects", return_value=SimpleNamespace(
                 projects=candidates, diagnostics={"searches_succeeded": 1})), \
             patch("x_digest.reading.editorial.curate_projects", side_effect=EditorialError(
                 "synthetic private transport details", code="timeout")) as edit:
            with self.assertRaises(ConfigurationError):
                self.run_edition(edition="weekly")
        self.assertEqual(edit.call_count, 2)
        audit_text = (self.work / "reading-audit.json").read_text()
        audit = json.loads(audit_text)
        self.assertEqual(audit["diagnostics"]["editor_error"], "timeout")
        self.assertEqual(audit["coverage"]["source_status"]["GitHub"], "unavailable")
        self.assertNotIn("private transport", audit_text)
        self.build.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse((self.work / "reading-state.json").exists())
        self.assertFalse((self.work / "preview/digest.json").exists())

    def test_snow_link_fallback_only_accepts_matching_search_metadata_and_access_failures(self):
        rejected = [
            (link_candidate(11), "not_found"),
            (link_candidate(12), "identity_mismatch"),
            (link_candidate(13), "body_too_short"),
            (link_candidate(14), "access_challenge"),  # Already seen.
            ({**link_candidate(15), "id": "xueqiu:999"}, "access_challenge"),
            ({key: value for key, value in link_candidate(16).items() if key != "discovery_basis"}, "access_challenge"),
            ({**link_candidate(17), "discovery_basis": "generated_text"}, "access_challenge"),
            (link_candidate(18, title="今天真是美好的一天"), "access_challenge"),
            (link_candidate(19, title="价值投资稳赚保证收益加群"), "access_challenge"),
        ]
        valid = [link_candidate(1), link_candidate(2, author=1001),  # Same publisher as #1.
                 link_candidate(3), link_candidate(4), link_candidate(5)]
        # Untrusted fields must never become a generated assessment or author claim.
        valid[0].update(summary="编造正文摘要", reason="编造推荐理由", author="未经核实作者", score_total=100,
                        text="未经核实的文章正文")
        candidates = [p for p, _ in rejected] + valid
        outcomes = {p["url"]: status for p, status in rejected}
        outcomes.update({p["url"]: "access_challenge" for p in valid})
        chosen = reading.add_xueqiu_links([], candidates=candidates, outcomes=outcomes,
                                         seen={"xueqiu:14"}, settings=self.settings)
        self.assertEqual([p["id"] for p in chosen], ["xueqiu:1", "xueqiu:3", "xueqiu:4", "xueqiu:5"])
        self.assertEqual(len({p["author_id"] for p in chosen}), 4)
        self.assertTrue(all(p["reading_kind"] == "link_candidate" for p in chosen))
        self.assertTrue(all(not {"summary", "reason", "author", "score_total", "scores", "text"} & p.keys()
                            for p in chosen))
        self.assertEqual(chosen[0]["title"], valid[0]["title"])
        self.assertEqual(chosen[0]["content_basis"], "search_metadata")

    def test_snow_link_candidates_have_independent_quota_from_reviewed_classics(self):
        existing = [article("X", age=60), article(number=20, age=None)]
        links = [link_candidate(i) for i in range(1, 5)]
        outcomes = {p["url"]: "access_denied" for p in links}
        chosen = reading.add_xueqiu_links(existing, candidates=links, outcomes=outcomes,
                                         seen=set(), settings=self.settings)
        self.assertEqual(len(chosen), 5)
        self.assertEqual(sum(p["reading_kind"] == "link_candidate" for p in chosen), 3)
        self.assertEqual(existing, chosen[:2])
        full = existing + [article("X", number=30, age=90)]
        self.assertEqual(len(reading.add_xueqiu_links(full, candidates=links, outcomes=outcomes,
                                                    seen=set(), settings=self.settings)), 6)
        self.settings["reading"]["xueqiu_link_fallback"] = False
        self.assertEqual(reading.add_xueqiu_links([], candidates=links, outcomes=outcomes,
                                                seen=set(), settings=self.settings), [])

    def test_daily_waf_fallback_previews_four_links_without_model_assessment_or_delivery_record(self):
        links = [link_candidate(i) for i in range(1, 6)]
        with patch("x_digest.reading.search_by_category", side_effect=ProviderError("synthetic X outage")), \
             patch("x_digest.reading.fetch_verified_posts") as xfetch, \
             patch("x_digest.reading.discover_xueqiu", return_value=SimpleNamespace(
                 urls=[p["url"] for p in links], candidates=links, diagnostics={"searches_completed": 2})), \
             patch("x_digest.reading.fetch_articles", return_value=SimpleNamespace(articles=[],
                 diagnostics={"access_challenge": 5}, outcomes={p["url"]: "access_challenge" for p in links})), \
             patch("x_digest.reading.editorial.curate_articles") as edit:
            result = self.run_edition()
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["source_counts"], {"雪球": 4})
        content = json.loads((self.work / "preview/digest.json").read_text())
        self.assertEqual(content["coverage"]["source_status"], {"X": "unavailable", "雪球": "links_only"})
        self.assertTrue(all(p["reading_kind"] == "link_candidate" for p in content["items"]))
        self.assertTrue(all(not {"author", "summary", "reason", "scores", "score_total", "text"} & p.keys()
                            for p in content["items"]))
        edit.assert_not_called()
        xfetch.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse((self.work / "reading-state.json").exists())


if __name__ == "__main__":
    unittest.main()
