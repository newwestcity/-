from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import smtplib
import unittest
from unittest.mock import patch

from scripts import check_mail


ENV = {"SMTP_USERNAME": "example@qq.com", "SMTP_AUTH_CODE": "synthetic-smtp-only-secret"}


class CheckMailTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.stderr = redirect_stderr(self.output)
        self.stderr.__enter__()
        self.addCleanup(self.stderr.__exit__, None, None, None)

    @patch("scripts.check_mail.send_message")
    def test_default_help_does_not_send_or_require_configuration(self, send):
        self.assertEqual(check_mail.main([]), 0)
        send.assert_not_called()
        self.assertIn("--send-test", self.output.getvalue())

    @patch("scripts.check_mail.send_message")
    def test_missing_fields_are_fixed_names_and_never_open_smtp(self, send):
        self.assertEqual(check_mail.main(["--send-test"]), 2)
        send.assert_not_called()
        lines = self.output.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0]), {
            "SMTP_USERNAME_configured": False,
            "SMTP_AUTH_CODE_configured": False,
            "MAIL_TO_configured": False,
        })
        self.assertIn("missing_required=SMTP_USERNAME,SMTP_AUTH_CODE", lines[1])

    @patch("scripts.check_mail.send_message")
    def test_explicit_send_calls_smtp_once_with_clear_test_mail_and_self_recipient(self, send):
        with patch.dict("os.environ", ENV):
            self.assertEqual(check_mail.main(["--send-test"]), 0)
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertEqual(message["Subject"], "每日阅读精选｜邮箱连接测试")
        self.assertEqual(message["To"], "example@qq.com")
        self.assertEqual(message.get_content_type(), "text/plain")
        body = message.get_content()
        self.assertIn("仅用于连接测试", body)
        self.assertIn("正式日报尚未启用", body)
        self.assertNotIn("0 条", body)
        self.assertNotIn("本期精选", body)
        self.assertEqual(send.call_args.kwargs["host"], "smtp.qq.com")
        self.assertEqual(send.call_args.kwargs["port"], 465)
        self.assertIn("smtp_accepted=true", self.output.getvalue())
        self.assertNotIn(ENV["SMTP_USERNAME"], self.output.getvalue())
        self.assertNotIn(ENV["SMTP_AUTH_CODE"], self.output.getvalue())

    @patch("scripts.check_mail.send_message")
    def test_configured_destination_is_used_without_printing_it(self, send):
        with patch.dict("os.environ", {**ENV, "MAIL_TO": "reader@example.test"}):
            self.assertEqual(check_mail.main(["--send-test"]), 0)
        self.assertEqual(send.call_args.args[0]["To"], "reader@example.test")
        self.assertNotIn("reader@example.test", self.output.getvalue())

    @patch("scripts.check_mail.send_message")
    def test_invalid_mailbox_is_rejected_before_smtp(self, send):
        with patch.dict("os.environ", {**ENV, "MAIL_TO": "invalid\r\nInjected: value"}):
            self.assertEqual(check_mail.main(["--send-test"]), 1)
        send.assert_not_called()
        self.assertIn("invalid_configuration", self.output.getvalue())
        self.assertNotIn("Injected", self.output.getvalue())

    @patch("scripts.check_mail.send_message")
    def test_sensitive_smtp_error_body_is_not_printed(self, send):
        sensitive = "example@qq.com synthetic-smtp-only-secret other-private-value"
        send.side_effect = smtplib.SMTPAuthenticationError(535, sensitive.encode())
        with patch.dict("os.environ", ENV):
            self.assertEqual(check_mail.main(["--send-test"]), 1)
        text = self.output.getvalue()
        self.assertIn("smtp_authentication_error;smtp_code=535", text)
        self.assertNotIn("smtp_accepted=true", text)
        for value in sensitive.split():
            self.assertNotIn(value, text)

    @patch("scripts.check_mail.send_message")
    def test_unexpected_error_is_sanitized_without_traceback(self, send):
        send.side_effect = RuntimeError("example@qq.com synthetic-smtp-only-secret")
        with patch.dict("os.environ", ENV):
            self.assertEqual(check_mail.main(["--send-test"]), 1)
        self.assertIn("unexpected_error", self.output.getvalue())
        self.assertNotIn(ENV["SMTP_USERNAME"], self.output.getvalue())
        self.assertNotIn(ENV["SMTP_AUTH_CODE"], self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
