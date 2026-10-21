from contextlib import redirect_stderr, redirect_stdout
import getpass
from io import StringIO
import subprocess
import unittest
from unittest.mock import patch

from scripts import configure_github as helper


class ConfigureGitHubTests(unittest.TestCase):
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

    @staticmethod
    def success(command, **kwargs):
        if "repos/newwestcity/-/actions/variables" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "repos/newwestcity/-/actions/secrets" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "newwestcity/-\n", "")

    def run_apply(self, **kwargs):
        kwargs.setdefault("mailbox", "example@qq.com")
        return helper.configure(apply=True, **kwargs)

    @patch("scripts.configure_github.subprocess.run")
    @patch("scripts.configure_github.getpass.getpass")
    def test_default_preview_neither_contacts_github_nor_prompts(self, prompt, run):
        self.assertEqual(helper.configure(), 0)
        run.assert_not_called()
        prompt.assert_not_called()
        self.assertIn("缺少 SMTP_USERNAME/MAIL_TO", self.output.getvalue())

    @patch("scripts.configure_github.subprocess.run")
    def test_apply_without_mailbox_stops_before_network(self, run):
        with self.assertRaisesRegex(helper.ConfigureError, "缺少 SMTP_USERNAME"):
            helper.configure(apply=True)
        run.assert_not_called()

    @patch("scripts.configure_github.subprocess.run")
    def test_mailbox_can_come_from_nonsecret_environment(self, run):
        run.side_effect = self.success
        with patch.dict("os.environ", {"SMTP_USERNAME": "example@qq.com"}):
            helper.configure(apply=True)
        writes = [call.args[0] for call in run.call_args_list if call.args[0][1:3] == ["variable", "set"]]
        mailbox_writes = [command for command in writes if command[3] in {"SMTP_USERNAME", "MAIL_TO"}]
        self.assertEqual(len(mailbox_writes), 2)
        self.assertTrue(all(command[-1] == "example@qq.com" for command in mailbox_writes))

    @patch("scripts.configure_github.subprocess.run")
    def test_network_preflight_failure_prevents_all_writes_and_sanitizes_output(self, run):
        secret = "synthetic-secret-that-must-not-appear"
        run.return_value = subprocess.CompletedProcess([], 1, secret, secret)
        with self.assertRaises(helper.ConfigureError) as error:
            self.run_apply()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn(secret, str(error.exception) + self.output.getvalue())

    @patch("scripts.configure_github.subprocess.run")
    def test_secrets_are_sent_only_via_stdin_and_never_printed(self, run):
        run.side_effect = self.success
        values = ["raw-api-test-secret", "raw-smtp-test-secret"]
        with patch("scripts.configure_github.getpass.getpass", side_effect=values):
            self.assertEqual(self.run_apply(prompt_secrets=True), 0)
        calls = [call for call in run.call_args_list if call.args[0][1:3] == ["secret", "set"]]
        self.assertEqual(len(calls), 2)
        for call, value in zip(calls, values):
            self.assertEqual(call.kwargs["input"], value)
            self.assertNotIn(value, call.args[0])
            self.assertNotIn(value, self.output.getvalue())

    @patch("scripts.configure_github.subprocess.run")
    def test_cloud_secret_environment_values_are_never_transferred(self, run):
        run.side_effect = self.success
        with patch.dict("os.environ", {"DIGEST_API_KEY": "proxy-placeholder", "SMTP_AUTH_CODE": "cloud-placeholder"}):
            self.run_apply()
        self.assertFalse(any(call.args[0][1:3] == ["secret", "set"] for call in run.call_args_list))
        self.assertTrue(all(call.kwargs["input"] is None for call in run.call_args_list))
        self.assertNotIn("proxy-placeholder", self.output.getvalue())
        self.assertNotIn("cloud-placeholder", self.output.getvalue())

    @patch("scripts.configure_github.subprocess.run")
    def test_unsupplied_api_base_and_model_do_not_overwrite_remote_values(self, run):
        run.side_effect = self.success
        self.run_apply()
        writes = [call.args[0] for call in run.call_args_list if call.args[0][1:3] == ["variable", "set"]]
        self.assertEqual({command[3] for command in writes}, {"SMTP_USERNAME", "MAIL_TO"})

    @patch("scripts.configure_github.subprocess.run")
    def test_schedule_switch_is_preserved_when_enabled_disabled_or_absent(self, run):
        for remote_value in ("true\n", "false\n", ""):
            with self.subTest(remote_value=remote_value):
                def configured(command, **kwargs):
                    if "repos/newwestcity/-/actions/variables" in command:
                        return subprocess.CompletedProcess(command, 0, remote_value, "")
                    return self.success(command, **kwargs)
                run.reset_mock()
                run.side_effect = configured
                self.run_apply()
                writes = [call.args[0] for call in run.call_args_list if call.args[0][1:3] == ["variable", "set"]]
                self.assertNotIn("DIGEST_ENABLED", {command[3] for command in writes})

    @patch("scripts.configure_github.subprocess.run")
    def test_nonsecret_api_configuration_can_come_from_environment(self, run):
        run.side_effect = self.success
        with patch.dict("os.environ", {"DIGEST_API_BASE": "https://relay.example.test/v1", "DIGEST_MODEL": "verified-model"}):
            self.run_apply()
        writes = [call.args[0] for call in run.call_args_list if call.args[0][1:3] == ["variable", "set"]]
        self.assertTrue(any(command[3] == "DIGEST_API_BASE" and command[-1] == "https://relay.example.test/v1" for command in writes))
        self.assertTrue(any(command[3] == "DIGEST_MODEL" and command[-1] == "verified-model" for command in writes))

    @patch("scripts.configure_github.subprocess.run")
    def test_empty_prompt_preserves_existing_secret_and_does_not_create_blank(self, run):
        def existing(command, **kwargs):
            if "repos/newwestcity/-/actions/secrets" in command:
                return subprocess.CompletedProcess(command, 0, "DIGEST_API_KEY\nSMTP_AUTH_CODE\n", "")
            return self.success(command, **kwargs)
        run.side_effect = existing
        with patch("scripts.configure_github.getpass.getpass", side_effect=["", ""]):
            self.run_apply(prompt_secrets=True)
        self.assertFalse(any(call.args[0][1:3] == ["secret", "set"] for call in run.call_args_list))
        self.assertNotIn("仍缺少", self.output.getvalue())

    @patch("scripts.configure_github.subprocess.run")
    def test_prompt_without_apply_is_refused(self, run):
        with self.assertRaises(helper.ConfigureError):
            helper.configure(prompt_secrets=True)
        run.assert_not_called()

    @patch("scripts.configure_github.subprocess.run")
    def test_terminal_echo_fallback_is_refused_before_writes(self, run):
        run.side_effect = self.success
        with patch("scripts.configure_github.getpass.getpass", side_effect=getpass.GetPassWarning()):
            with self.assertRaisesRegex(helper.ConfigureError, "无法隐藏输入"):
                self.run_apply(prompt_secrets=True)
        self.assertTrue(all(call.args[0][1] == "api" for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
