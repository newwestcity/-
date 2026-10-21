"""Mail safety and delivery tests; all posts are synthetic test fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import ssl
import unittest
from unittest.mock import patch

from x_digest.mail import build_message, send_message


def fixture_post(**updates):
    post = {
        "id": "123456789",
        "url": "https://x.com/test_author/status/123456789",
        "author": "合成测试作者",
        "category": "读书",
        "summary": "合成测试摘要，不代表真实推荐。",
        "reason": "仅用于验证邮件渲染。",
    }
    post.update(updates)
    return post


class BuildMessageTests(unittest.TestCase):
    def build(self, posts, **overrides):
        kwargs = dict(
            recipient="reader@qq.com", sender="sender@qq.com", digest_date="2026-10-20"
        )
        kwargs.update(overrides)
        return build_message(posts, **kwargs)

    def test_untrusted_post_content_cannot_inject_html(self):
        malicious = '<img src=x onerror="alert(1)">&'
        message = self.build([
            fixture_post(author=malicious, category=malicious, summary=malicious, reason=malicious)
        ], digest_date="2026-10-20 <test>")
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertNotIn("<img", html)
        self.assertNotIn("<test>", html)
        self.assertEqual(html.count("&lt;img"), 4)
        self.assertIn("&amp;", html)

    def test_preserves_recipients_original_links_and_shortfall(self):
        message = self.build([fixture_post()])
        self.assertEqual(message["To"], "reader@qq.com")
        self.assertEqual(message["From"], "sender@qq.com")
        text = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("https://x.com/test_author/status/123456789", text)
        self.assertIn('href="https://x.com/test_author/status/123456789"', html)
        self.assertIn("本期精选 1 条，目标 10 条", text)
        self.assertIn("宁缺毋滥", text)
        self.assertTrue(message.is_multipart())

    def test_rejects_non_post_urls_and_host_tricks(self):
        for url in (
            "http://x.com/test/status/1",
            "https://x.com.evil.example/test/status/1",
            "https://x.com@evil.example/test/status/1",
            "https://evil.example/test/status/1",
            "javascript:alert(1)",
            "https://x.com/test/status/1?redirect=evil",
            "https://x.com/test",
            'https://x.com/test/status/1\" onclick=\"alert(1)',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.build([fixture_post(url=url)])

    def test_accepts_legacy_twitter_post_links(self):
        message = self.build([fixture_post(url="https://twitter.com/test/status/123456789")])
        text = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("https://twitter.com/test/status/123456789", text)

    def test_rejects_header_injection(self):
        for field in ("recipient", "sender", "digest_date"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build([fixture_post()], **{field: "reader@qq.com\r\nBcc: attacker@example.com"})

    def test_empty_digest_has_no_invented_recommendation(self):
        message = self.build([])
        text = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("本期精选 0 条", text)
        self.assertIn("没有符合筛选要求的帖子", text)
        self.assertNotIn("https://", text)

    def test_classics_are_identified_as_old_reading_with_beijing_publication_date(self):
        post = fixture_post(
            reading_kind="classic", published_at="2024-01-01T18:30:00+00:00"
        )
        message = self.build([post])
        for content_type in ("plain", "html"):
            content = message.get_body(preferencelist=(content_type,)).get_content()
            self.assertIn("其中近期 0 条，经典阅读 1 条", content)
            self.assertIn("本期近期检索未取得合格帖子，仅提供经典阅读", content)
            self.assertIn("经典阅读 · 原帖 2024-01-02（北京时间）", content)
        for invalid_date in (None, "", "2024-01-01", "2024-01-01T18:30:00", "invalid"):
            with self.subTest(published_at=invalid_date), self.assertRaises(ValueError):
                self.build([fixture_post(reading_kind="classic", published_at=invalid_date)])


class SendMessageTests(unittest.TestCase):
    def message(self):
        return build_message([], recipient="reader@qq.com", sender="sender@qq.com", digest_date="2026-10-20")

    @patch("x_digest.mail.smtplib.SMTP_SSL")
    def test_uses_verified_tls_and_keeps_credentials_out_of_output(self, smtp_class):
        message = self.message()
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            send_message(message, username="sender@qq.com", password="synthetic-test-secret")
        self.assertEqual(output.getvalue(), "")
        args, kwargs = smtp_class.call_args
        self.assertEqual(args, ("smtp.qq.com", 465))
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["context"].check_hostname)
        self.assertEqual(kwargs["timeout"], 30)
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("sender@qq.com", "synthetic-test-secret")
        smtp.send_message.assert_called_once_with(message)

    @patch("x_digest.mail.smtplib.SMTP_SSL")
    def test_missing_credentials_never_open_a_connection(self, smtp_class):
        for username, password in (("", "test"), ("sender@qq.com", ""), (" ", "test")):
            with self.subTest(username=username), self.assertRaises(ValueError):
                send_message(self.message(), username=username, password=password)
        smtp_class.assert_not_called()

    @patch("x_digest.mail.smtplib.SMTP_SSL")
    def test_quit_failure_after_data_acceptance_does_not_mark_delivery_failed(self, smtp_class):
        import smtplib
        smtp_class.return_value.__exit__.side_effect = smtplib.SMTPServerDisconnected("test QUIT failure")
        send_message(self.message(), username="sender@qq.com", password="test-only")
        smtp_class.return_value.__enter__.return_value.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
