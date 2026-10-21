from datetime import datetime, timedelta, timezone
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
        with patch("x_digest.provider.search_by_category", return_value=found()), patch("x_digest.app.send_message") as send:
            result = run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        send.assert_not_called()
        self.assertEqual(result["status"], "preview")
        self.assertTrue((self.work / "preview/digest.html").exists())
        self.assertFalse((self.work / "state.json").exists())

    def test_successful_delivery_is_recorded_and_rerun_does_not_search_or_send(self):
        with patch("x_digest.provider.search_by_category", return_value=found()) as search, patch("x_digest.app.send_message") as send:
            first = run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
            second = run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        self.assertEqual(first["status"], "smtp_accepted")
        self.assertEqual(second["status"], "already_sent")
        search.assert_called_once()
        send.assert_called_once()
        self.assertEqual(load_state(self.work / "state.json")["deliveries"][0]["post_ids"], [POST_ID])

    def test_search_failure_stops_before_mail(self):
        with patch("x_digest.provider.search_by_category", side_effect=ProviderError("test search failure")), patch("x_digest.app.send_message") as send:
            with self.assertRaises(ProviderError):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        send.assert_not_called()
        self.assertFalse((self.work / "state.json").exists())

    def test_generated_but_uncited_posts_cannot_be_emailed(self):
        response = found()
        response.cited_urls = []
        with patch("x_digest.provider.search_by_category", return_value=response), patch("x_digest.app.send_message") as send:
            with self.assertRaisesRegex(ConfigurationError, "没有通过"):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        send.assert_not_called()

    def test_mail_failure_does_not_consume_posts_or_mark_day_sent(self):
        with patch("x_digest.provider.search_by_category", return_value=found()), patch("x_digest.app.send_message", side_effect=smtplib.SMTPException("test failure")):
            with self.assertRaises(smtplib.SMTPException):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        self.assertFalse((self.work / "state.json").exists())

    def test_empty_search_candidates_can_be_edited_from_independently_verified_content(self):
        retrieved = SimpleNamespace(posts=[], cited_urls=[URL], usage={})
        verified = {'id': POST_ID, 'url': URL, 'author': 'example', 'text': '官方返回的原帖正文',
                    'published_at': (NOW - timedelta(hours=1)).isoformat(), 'verified_at': NOW.isoformat()}
        with patch('x_digest.provider.search_by_category', return_value=retrieved), \
             patch('x_digest.oembed.fetch_verified_posts', return_value=[verified]) as fetch, \
             patch('x_digest.provider.curate_verified_posts', return_value=found()) as curate, \
             patch('x_digest.app.send_message') as send:
            result = run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        self.assertEqual(result['count'], 1)
        fetch.assert_called_once_with([URL], now=NOW)
        self.assertEqual(curate.call_args.kwargs['posts'], [verified])
        send.assert_not_called()
        self.assertFalse((self.work / 'state.json').exists())

    def test_unverified_content_never_reaches_the_editor_or_mail(self):
        retrieved = SimpleNamespace(posts=[], cited_urls=[URL], usage={})
        with patch('x_digest.provider.search_by_category', return_value=retrieved), \
             patch('x_digest.oembed.fetch_verified_posts', return_value=[]), \
             patch('x_digest.provider.curate_verified_posts') as curate, \
             patch('x_digest.app.send_message') as send:
            with self.assertRaisesRegex(ConfigurationError, 'oembed_verified_posts'):
                run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        curate.assert_not_called()
        send.assert_not_called()

    def test_old_citations_are_audited_without_billed_editing_or_post_fetches(self):
        self.settings['max_classic_count'] = 0
        old_id = str((int((NOW - timedelta(days=8)).timestamp() * 1000) - 1288834974657) << 22)
        retrieved = SimpleNamespace(posts=[], cited_urls=[f'https://x.com/example/status/{old_id}'], usage={})
        with patch('x_digest.provider.search_by_category', return_value=retrieved), \
             patch('x_digest.oembed.fetch_verified_posts') as fetch, \
             patch('x_digest.provider.curate_verified_posts') as curate:
            with self.assertRaisesRegex(ConfigurationError, '"citation_old":1'):
                run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        fetch.assert_not_called()
        curate.assert_not_called()
        audit = json.loads((self.work / 'retrieval-audit.json').read_text())
        self.assertEqual(audit['sources'][0]['status'], 'old')

    def test_old_search_summary_is_replaced_by_grounded_classic_editing(self):
        old_time = NOW - timedelta(days=200)
        old_id = str((int(old_time.timestamp() * 1000) - 1288834974657) << 22)
        old_url = f'https://x.com/example/status/{old_id}'
        raw = {'url': old_url, 'category': '投资', 'summary': '不可直接信任的旧搜索摘要', 'reason': '旧搜索理由'}
        retrieved = SimpleNamespace(posts=[raw], cited_urls=[old_url], usage={})
        verified = {'id': old_id, 'url': old_url, 'author': 'example', 'text': '已核实的长期投资原则',
                    'published_at': old_time.isoformat(), 'verified_at': NOW.isoformat()}
        grounded = {**raw, 'summary': '依据核实正文重新编辑的投资原则', 'reason': '学习长期风险管理'}
        curated = SimpleNamespace(posts=[grounded], cited_urls=[old_url], usage={})
        with patch('x_digest.provider.search_by_category', return_value=retrieved), \
             patch('x_digest.oembed.fetch_verified_posts', return_value=[verified]), \
             patch('x_digest.provider.curate_verified_posts', return_value=curated) as edit, \
             patch('x_digest.app.send_message') as send:
            result = run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        self.assertTrue(edit.call_args.kwargs['classic'])
        self.assertEqual(edit.call_args.kwargs['posts'], [verified])
        self.assertEqual(result['classic_count'], 1)
        self.assertEqual(result['recent_count'], 0)
        data = json.loads((self.work / 'preview/digest.json').read_text())
        self.assertEqual(data['posts'][0]['summary'], grounded['summary'])
        self.assertEqual(data['posts'][0]['reading_kind'], 'classic')
        send.assert_not_called()

    def test_unavailable_old_post_body_never_uses_generated_classic_summary(self):
        old_id = str((int((NOW - timedelta(days=200)).timestamp() * 1000) - 1288834974657) << 22)
        url = f'https://x.com/example/status/{old_id}'
        retrieved = SimpleNamespace(posts=[{'url': url, 'category': '投资', 'summary': '未经核实的旧摘要',
                                           'reason': '不能直接发出'}], cited_urls=[url], usage={})
        with patch('x_digest.provider.search_by_category', return_value=retrieved), \
             patch('x_digest.oembed.fetch_verified_posts', return_value=[]), \
             patch('x_digest.provider.curate_verified_posts') as edit, \
             patch('x_digest.app.send_message') as send:
            with self.assertRaises(ConfigurationError):
                run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        edit.assert_not_called()
        send.assert_not_called()

    def test_recent_editor_failure_still_allows_verified_classic_editing(self):
        old_time = NOW - timedelta(days=200)
        old_id = str((int(old_time.timestamp() * 1000) - 1288834974657) << 22)
        old_url = f'https://x.com/example/status/{old_id}'
        retrieved = SimpleNamespace(posts=[], cited_urls=[URL, old_url], usage={})
        verified = [{'id': POST_ID, 'url': URL}, {'id': old_id, 'url': old_url}]
        curated = SimpleNamespace(posts=[{'url': old_url, 'category': '投资', 'summary': '核实后的长期原则',
                                         'reason': '学习风险管理'}], cited_urls=[old_url], usage={})
        with patch('x_digest.provider.search_by_category', return_value=retrieved), \
             patch('x_digest.oembed.fetch_verified_posts', return_value=verified), \
             patch('x_digest.provider.curate_verified_posts', side_effect=[ProviderError('recent editor unavailable'), curated]) as edit, \
             patch('x_digest.app.send_message'):
            result = run_digest(self.settings, work_dir=self.work, send=False, now=NOW)
        self.assertEqual(edit.call_count, 2)
        self.assertEqual(result['classic_count'], 1)
        self.assertEqual(result['recent_count'], 0)

    def test_missing_smtp_authorization_stops_before_paid_api_call(self):
        with patch.dict("os.environ", {k: v for k, v in ENV.items() if k != "SMTP_AUTH_CODE"}, clear=True):
            with patch("x_digest.provider.search_by_category") as search:
                with self.assertRaisesRegex(ConfigurationError, "SMTP_AUTH_CODE"):
                    run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        search.assert_not_called()

    def test_invalid_recipient_stops_before_paid_api_call(self):
        with patch.dict("os.environ", {"MAIL_TO": "invalid-mailbox"}):
            with patch("x_digest.provider.search_by_category") as search:
                with self.assertRaises(ValueError):
                    run_digest(self.settings, work_dir=self.work, send=True, now=NOW)
        search.assert_not_called()

    def test_configuration_check_does_not_contact_services(self):
        with patch("x_digest.provider.search_by_category") as search, patch("x_digest.app.send_message") as send, patch("sys.stdout", new=StringIO()):
            self.assertEqual(main(["--check", "--send"]), 0)
        search.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
