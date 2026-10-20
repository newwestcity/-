from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import smtplib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from x_digest.app import ConfigurationError, load_settings, main, run_digest
from x_digest.provider import ProviderError
from x_digest.state import load_state


NOW = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
POST_ID = str((int(NOW.timestamp() * 1000) - 3600000 - 1288834974657) << 22)
URL = f"https://x.com/example/status/{POST_ID}"
ENV = {"DIGEST_API_BASE": "https://api.example.test/v1", "DIGEST_API_KEY": "test-placeholder-not-live",
       "DIGEST_MODEL": "test-model", "SMTP_USERNAME": "test@qq.com", "SMTP_AUTH_CODE": "test-only"}


def found():
    return SimpleNamespace(posts=[{"url": URL, "category": "AI", "summary": "仅用于测试的摘要",
                                   "reason": "仅用于测试的理由"}], cited_urls=[URL], usage={})


class AppTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(Path(__file__).resolve().parents[1] / "settings.json")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.env = patch.dict("os.environ", ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_preview_has_real_pipeline_validation_but_no_mail_or_delivery_record(self):
        with patch("x_digest.provider.search", return_value=found()), patch("x_digest.app.send_message") as send:
            result = run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        send.assert_not_called()
        self.assertEqual(result["status"], "preview")
        self.assertTrue((self.work / "preview/digest.html").exists())
        self.assertFalse((self.work / "state.json").exists())

    def test_successful_delivery_is_recorded_and_rerun_does_not_search_or_send(self):
        with patch("x_digest.provider.search", return_value=found()) as search, patch("x_digest.app.send_message") as send:
            first = run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
            second = run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        self.assertEqual(first["status"], "smtp_accepted")
        self.assertEqual(second["status"], "already_sent")
        search.assert_called_once()
        send.assert_called_once()
        self.assertEqual(load_state(self.work / "state.json")["deliveries"][0]["post_ids"], [POST_ID])

    def test_search_failure_stops_before_mail(self):
        with patch("x_digest.provider.search", side_effect=ProviderError("test search failure")), patch("x_digest.app.send_message") as send:
            with self.assertRaises(ProviderError):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        send.assert_not_called()
        self.assertFalse((self.work / "state.json").exists())

    def test_generated_but_uncited_posts_cannot_be_emailed(self):
        response = found()
        response.cited_urls = []
        with patch("x_digest.provider.search", return_value=response), patch("x_digest.app.send_message") as send:
            with self.assertRaisesRegex(ConfigurationError, "没有通过"):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        send.assert_not_called()

    def test_mail_failure_does_not_consume_posts_or_mark_day_sent(self):
        with patch("x_digest.provider.search", return_value=found()), patch("x_digest.app.send_message", side_effect=smtplib.SMTPException("test failure")):
            with self.assertRaises(smtplib.SMTPException):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        self.assertFalse((self.work / "state.json").exists())

    def test_missing_smtp_authorization_stops_before_paid_api_call(self):
        with patch.dict("os.environ", {k: v for k, v in ENV.items() if k != "SMTP_AUTH_CODE"}, clear=True):
            with patch("x_digest.provider.search") as search:
                with self.assertRaisesRegex(ConfigurationError, "SMTP_AUTH_CODE"):
                    run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        search.assert_not_called()

    def test_invalid_recipient_stops_before_paid_api_call(self):
        with patch.dict("os.environ", {"MAIL_TO": "invalid-mailbox"}):
            with patch("x_digest.provider.search") as search:
                with self.assertRaises(ValueError):
                    run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        search.assert_not_called()

    def test_configuration_check_does_not_contact_services(self):
        with patch("x_digest.provider.search") as search, patch("x_digest.app.send_message") as send, patch("sys.stdout", new=StringIO()):
            self.assertEqual(main(["--check", "--send"]), 0)
        search.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
