"""Repair examples use real snapshot validation and independent durable slots."""

from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import smtplib
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import send_repaired_example as example
from x_digest.app import ConfigurationError
from x_digest import reading_state
from x_digest.reading_reserve import save_reserve


NOW = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
ENV = {"SMTP_USERNAME": "fixture@qq.com", "SMTP_AUTH_CODE": "fixture-not-a-live-auth-code"}


def record(edition="daily", ids=None):
    return {"edition": edition, "period": "2026-10-23", "date": "2026-10-23",
            "item_ids": ids or ["xueqiu:1"], "sent_at": "2026-10-23T08:00:00+08:00"}


def link(number):
    return {"source": "雪球", "id": f"xueqiu:{number}", "url": f"https://xueqiu.com/{1000+number}/{number}",
            "author_id": str(1000+number), "title": "企业财报与长期投资的分析方法", "reading_kind": "link_candidate",
            "content_basis": "search_metadata", "published_at": "", "discovered_at": NOW.isoformat()}


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source, self.work = Path(self.temporary.name) / "source", Path(self.temporary.name) / "work"
        self.source.mkdir()
        save_reserve(self.source / "reading-reserve.json", [link(n) for n in (10, 11, 12)], now=NOW)
        self.ledger = Mock()
        self.ledger.history = {"version": 2, "deliveries": [record()]}
        self.ledger.pending_ids.return_value = set()
        for patcher in (patch.dict("os.environ", ENV, clear=True),
                        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("scripts.send_repaired_example.open_ledger", return_value=self.ledger)
        self.open = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("scripts.send_repaired_example.send_message")
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self):
        return example.send_example(self.source, self.work, now=NOW)

    def test_success_reserves_before_smtp_and_confirms_after_local_history(self):
        events = []
        self.ledger.reserve.side_effect = lambda **kwargs: events.append("reserve")
        self.send.side_effect = lambda *args, **kwargs: events.append("smtp")
        original_record = reading_state.record_delivery
        def save(*args, **kwargs):
            events.append("record")
            return original_record(*args, **kwargs)
        def complete(history):
            events.append("complete")
            self.ledger.history = history
        self.ledger.complete.side_effect = complete
        with patch("scripts.send_repaired_example.reading_state.record_delivery", side_effect=save):
            result = self.call()
        self.assertEqual(events, ["reserve", "smtp", "record", "complete"])
        self.assertEqual(result, {"status": "smtp_accepted", "count": 3,
                                 "source_counts": {"雪球": 3}, "date": "2026-10-23"})
        self.assertFalse(self.open.call_args.kwargs["allow_create"])
        self.assertEqual(self.ledger.reserve.call_args.kwargs["edition"], "example")
        message = self.send.call_args.args[0]
        self.assertTrue(str(message["Subject"]).startswith("修复验收｜"))
        self.assertTrue(message["Date"])
        self.assertTrue(message["Message-ID"])
        self.assertEqual(reading_state.seen_ids(self.ledger.history, today=NOW.date()),
                         {"xueqiu:1", "xueqiu:10", "xueqiu:11", "xueqiu:12"})
        self.assertTrue(reading_state.already_sent(self.ledger.history, edition="daily", period="2026-10-23"))
        self.assertFalse(reading_state.already_sent(self.ledger.history, edition="daily", period="2026-10-24"))
        html = (self.work / "preview/digest.html").read_text()
        self.assertNotIn(ENV["SMTP_USERNAME"], html)
        self.assertNotIn(ENV["SMTP_AUTH_CODE"], html)
        self.assertFalse(list(self.work.rglob("*.eml")))

    def test_existing_example_skips_before_source_read_or_smtp_configuration(self):
        self.ledger.history["deliveries"].append(record("example", ["xueqiu:10"]))
        with patch.dict("os.environ", {}, clear=True), \
             patch("scripts.send_repaired_example.load_reserve", side_effect=AssertionError("Must skip")):
            self.assertEqual(self.call()["status"], "already_sent")
        self.ledger.reserve.assert_not_called()
        self.send.assert_not_called()

    def test_seen_and_pending_materials_cannot_be_sent_as_new_example(self):
        self.ledger.history["deliveries"][0]["item_ids"] = ["xueqiu:10", "xueqiu:11"]
        self.ledger.pending_ids.return_value = {"xueqiu:12"}
        with self.assertRaises(ConfigurationError):
            self.call()
        self.ledger.reserve.assert_not_called()
        self.send.assert_not_called()

    def test_missing_snapshot_never_reserves_or_sends(self):
        self.source.joinpath("reading-reserve.json").unlink()
        with self.assertRaises(ConfigurationError):
            self.call()
        self.ledger.reserve.assert_not_called()
        self.send.assert_not_called()

    def test_ambiguous_smtp_failure_keeps_reservation_without_confirmation(self):
        self.send.side_effect = smtplib.SMTPServerDisconnected("private remote body")
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.call()
        self.ledger.reserve.assert_called_once()
        self.ledger.cancel.assert_not_called()
        self.ledger.complete.assert_not_called()
        self.assertFalse(self.work.joinpath("reading-state.json").exists())

    def test_explicit_smtp_rejection_releases_reservation_without_confirmation(self):
        self.send.side_effect = smtplib.SMTPAuthenticationError(535, b"private body")
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            self.call()
        self.ledger.cancel.assert_called_once()
        self.ledger.complete.assert_not_called()

    def test_default_cli_never_sends_and_errors_do_not_print_private_smtp_body(self):
        with patch("scripts.send_repaired_example.send_example") as send, patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(example.main([]), 0)
        send.assert_not_called()
        with patch("scripts.send_repaired_example.send_example", side_effect=smtplib.SMTPDataError(450, b"SECRET_ADDRESS")), \
             patch("sys.stderr", new_callable=StringIO) as output:
            self.assertEqual(example.main(["--send", "--source-dir", str(self.source)]), 1)
        self.assertNotIn("SECRET_ADDRESS", output.getvalue())


if __name__ == "__main__":
    unittest.main()
