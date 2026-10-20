from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import check_connection as helper


class Response:
    def __init__(self, data, status=200):
        self.status = status
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.read = Mock(side_effect=lambda limit: self.body[:limit])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CheckConnectionTests(unittest.TestCase):
    def setUp(self):
        self.key = "synthetic-api-secret-KEEP-PRIVATE"
        self.env = patch.dict("os.environ", {"DIGEST_API_KEY": self.key}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.report_path = Path(self.directory.name) / "report.json"
        self.output = StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stderr = redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)
        self.opener = Mock()
        self.build = patch("scripts.check_connection.request.build_opener", return_value=self.opener)
        self.build_mock = self.build.start()
        self.addCleanup(self.build.stop)
        self.respond({})

    def respond(self, data, status=200):
        self.response = Response(data, status)
        self.opener.open.return_value = self.response

    def run_check(self, mode="api"):
        return helper.main(["--mode", mode, "--report", str(self.report_path)])

    def report(self):
        return json.loads(self.report_path.read_text())

    def assert_private(self, *values):
        artifacts = self.output.getvalue()
        if self.report_path.exists():
            artifacts += self.report_path.read_text()
        for value in (self.key, *values):
            self.assertNotIn(value, artifacts)
        self.assertNotIn("Authorization", artifacts)

    def test_no_mode_only_help_without_network_or_report(self):
        self.assertEqual(helper.main([]), 0)
        self.assertIn("--mode", self.output.getvalue())
        self.build_mock.assert_not_called()
        self.opener.open.assert_not_called()
        self.assertFalse(self.report_path.exists())

    def test_missing_key_does_not_make_request(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.run_check(), 1)
        self.build_mock.assert_not_called()
        self.assertIsNone(self.report()["http_status"])
        self.assert_private()

    def test_invalid_arguments_never_echo_values_or_make_request(self):
        self.assertEqual(helper.main(["--mode", self.key]), 2)
        self.assertEqual(helper.main(["--unknown", self.key]), 2)
        self.build_mock.assert_not_called()
        self.assert_private()

    def test_api_single_get_returns_only_status_and_model_membership(self):
        self.respond({"data": [{"id": "unrelated-model"}, {"id": helper.DEFAULT_MODEL}], "secret": self.key})
        self.assertEqual(self.run_check(), 0)
        self.opener.open.assert_called_once()
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, helper.DEFAULT_API_BASE + "/models")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + self.key)
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 120})
        self.assertEqual(self.report(), {"http_status": 200, "configured_model_present": True})
        self.assertEqual(json.loads(self.output.getvalue()), self.report())
        self.assert_private("unrelated-model", helper.DEFAULT_MODEL, helper.DEFAULT_API_BASE)

    def test_api_reports_absent_model_without_printing_list(self):
        self.respond({"data": [{"id": "not-the-configured-model"}]})
        self.assertEqual(self.run_check(), 0)
        self.assertFalse(self.report()["configured_model_present"])
        self.assert_private("not-the-configured-model")

    def test_custom_environment_values_are_not_echoed(self):
        base, model = "https://private-config.example/v1", "private-model"
        self.respond({"data": [{"id": model}]})
        with patch.dict("os.environ", {"DIGEST_API_BASE": base, "DIGEST_MODEL": model}):
            self.assertEqual(self.run_check(), 0)
        self.assertEqual(self.opener.open.call_args.args[0].full_url, base + "/models")
        self.assertTrue(self.report()["configured_model_present"])
        self.assert_private(base, model)

    def test_chat_exactly_matches_documented_minimal_request(self):
        self.respond({"choices": [{"finish_reason": "stop", "message": {"content": "Hello!"}}]})
        self.assertEqual(self.run_check("chat"), 0)
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, helper.DEFAULT_API_BASE + "/chat/completions")
        self.assertEqual(json.loads(req.data), {"model": helper.DEFAULT_MODEL,
                                              "messages": [{"role": "user", "content": "Say hello in one sentence."}]})
        self.assertTrue(self.report()["model_reply_received"])
        self.assertFalse(self.report()["daily_digest_ready"])
        self.assert_private("Hello!")
        self.opener.open.assert_called_once()

    def test_catalog_selects_only_a_listed_gpt_model_with_one_get(self):
        self.respond({'data': [{'id': 'gpt-4o-mini'}, {'id': 'gpt-4.1-mini'}, {'id': self.key}]})
        self.assertEqual(self.run_check('catalog'), 0)
        self.assertEqual(self.report(), {'http_status': 200, 'suggested_model': 'gpt-4.1-mini'})
        self.opener.open.assert_called_once()
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), 'GET')
        self.assertEqual(req.full_url, helper.DEFAULT_API_BASE + '/models')
        self.assertIsNone(req.data)
        self.assert_private()

    def test_catalog_does_not_guess_when_no_known_gpt_model_is_listed(self):
        self.respond({'data': [{'id': helper.DEFAULT_MODEL}, {'id': 'gpt-unknown\nDIGEST_API_KEY=bad'}]})
        self.assertEqual(self.run_check('catalog'), 1)
        self.assertIsNone(self.report()['suggested_model'])
        self.opener.open.assert_called_once()
        self.assert_private('DIGEST_API_KEY=bad')

    def test_catalog_can_select_the_already_configured_public_model_id(self):
        self.respond({'data': [{'id': 'gpt-4.1-mini'}]})
        with patch.dict('os.environ', {'DIGEST_MODEL': 'gpt-4.1-mini'}):
            self.assertEqual(self.run_check('catalog'), 0)
        self.assertEqual(self.report()['suggested_model'], 'gpt-4.1-mini')
        self.assert_private()

    def test_catalog_selects_a_smaller_model_from_the_providers_actual_gpt_ids(self):
        self.respond({'data': [{'id': name} for name in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-luna')]})
        self.assertEqual(self.run_check('catalog'), 0)
        self.assertEqual(self.report()['suggested_model'], 'gpt-6-luna')
        self.opener.open.assert_called_once()

    def test_responses_search_requires_completed_tool_and_independent_post_sources(self):
        self.respond({'status': 'completed', 'output': [
            {'type': 'web_search_call', 'status': 'completed', 'action': {'sources': [
                {'url': 'https://twitter.com/author/status/123?s=20'},
                {'url': 'https://x.com/author/status/123'},
            ]}},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': self.key,
                                            'annotations': [{'type': 'url_citation', 'url': 'https://x.com/second/status/456'}]}]},
        ]})
        self.assertEqual(self.run_check('responses-search'), 0)
        self.assertEqual(self.report()['x_source_count'], 2)
        self.assertTrue(self.report()['search_completed'])
        self.assertFalse(self.report()['daily_digest_ready'])
        self.opener.open.assert_called_once()
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, helper.DEFAULT_API_BASE + '/responses')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data)['max_tool_calls'], 2)
        self.assert_private()

    def test_responses_search_does_not_trust_generated_urls_or_unfinished_tools(self):
        for data in [
            {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'https://x.com/author/status/123'}]}]},
            {'status': 'incomplete', 'output': [{'type': 'web_search_call', 'status': 'completed',
                                               'action': {'sources': [{'url': 'https://x.com/author/status/123'}]}}]},
            {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed',
                                               'action': {'sources': [{'url': 'https://example.test/article'}]}}]},
        ]:
            self.respond(data)
            self.assertEqual(self.run_check('responses-search'), 1)
            self.assertEqual(self.report()['x_source_count'], 0)
        self.assert_private()

    def test_responses_metadata_accepts_web_mobile_and_media_post_links(self):
        self.respond({'status': 'completed', 'output': [
            {'type': 'web_search_call', 'status': 'completed', 'action': {'sources': [
                {'url': 'https://x.com/i/web/status/123'},
                {'url': 'https://mobile.twitter.com/author/status/456/photo/1'},
                {'url': 'https://x.com/author/status/456/video/1'},
            ]}},
        ]})
        self.assertEqual(self.run_check('responses-search'), 0)
        self.assertEqual(self.report()['x_source_count'], 2)
        self.assertEqual(self.report()['source_record_count'], 3)

    def test_generated_post_candidates_are_diagnostic_only(self):
        self.respond({'status': 'completed', 'output': [
            {'type': 'web_search_call', 'status': 'completed', 'action': {}},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'https://x.com/author/status/123'}]},
        ]})
        self.assertEqual(self.run_check('responses-search'), 1)
        self.assertEqual(self.report()['unverified_post_urls'], ['https://x.com/author/status/123'])
        self.assertEqual(self.report()['x_source_count'], 0)

    def test_chat_without_completed_model_reply_is_not_success(self):
        responses = [{"error": {"message": self.key}}, {"choices": []},
                     {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
                     {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
                     {"choices": [{"message": {"content": "partial"}}]},
                     {"choices": [{"finish_reason": None, "message": {"content": "partial"}}]},
                     {"choices": [{"finish_reason": "unknown", "message": {"content": "partial"}}]}]
        for data in responses:
            self.respond(data)
            self.assertEqual(self.run_check("chat"), 1)
            self.assertFalse(self.report()["model_reply_received"])
        self.assert_private("partial")

    def test_bad_request_exposes_classification_and_redacted_message(self):
        error = {"error": {"param": "tools[0].type", "message": "unsupported tool type " + self.key}}
        body = BytesIO(json.dumps(error).encode())
        self.opener.open.side_effect = HTTPError("https://private.test/" + self.key, 400, self.key, {}, body)
        self.assertEqual(self.run_check("search"), 1)
        self.assertEqual(self.report()["rejected_field"], "tools[0].type")
        self.assertEqual(self.report()["error_category"], "search_tool_parameter_rejected")
        self.assertIn("unsupported tool type", self.report()["provider_error_hint"])
        self.assert_private("private.test")
        self.opener.open.assert_called_once()

    def test_error_hint_redacts_before_truncation_and_strips_other_sensitive_material(self):
        text = 'word ' * 96 + self.key + ' sk-another-key Bearer other-token student@example.test https://private.test/?key=other-token\nnext'
        body = BytesIO(json.dumps({"error": {"message": text}}).encode())
        self.opener.open.side_effect = HTTPError('https://private.test/', 400, self.key, {}, body)
        self.assertEqual(self.run_check('chat'), 1)
        hint = self.report()['provider_error_hint']
        self.assertLessEqual(len(hint), 500)
        self.assertNotIn('\n', hint)
        self.assertNotIn(self.key[:8], hint)
        self.assert_private('sk-another-key', 'other-token', 'student@example.test', 'private.test')
        short_hint = helper._error_hint('sk-another-key Bearer other-token student@example.test https://private.test/?key=other-token\nnext', (self.key,))
        for value in ('sk-another-key', 'other-token', 'student@example.test', 'private.test', '\n'):
            self.assertNotIn(value, short_hint)
        self.assertNotIn('other-secret', helper._error_hint('upstream: Bearer\x00other-secret', (self.key,)))

    def test_unknown_bad_request_fields_and_oversized_errors_are_not_published(self):
        for raw in [json.dumps({"error": {"param": self.key, "message": self.key}}).encode(),
                    self.key.encode() * 2000]:
            self.opener.open.side_effect = HTTPError("https://private.test/", 400, self.key, {}, BytesIO(raw))
            self.assertEqual(self.run_check("chat"), 1)
            self.assertNotIn("rejected_field", self.report())
        self.assert_private()

    def test_invalid_base_urls_stop_before_network(self):
        invalid = [
            "http://example.test/v1", "https://user:password@example.test/v1",
            "https://example.test/v1?token=" + self.key, "https://example.test/v1?",
            "https://example.test/v1#", "https://example.test/api", "https:///v1",
            "https://example.test:bad/v1", "https://example.test:99999/v1",
            "https://example.test/v1\n", "https://example.test\\other/v1",
        ]
        for base in invalid:
            with self.subTest(base_index=invalid.index(base)):
                with patch.dict("os.environ", {"DIGEST_API_BASE": base}):
                    self.assertEqual(self.run_check(), 1)
        self.build_mock.assert_not_called()
        self.assert_private("user:password")

    def test_invalid_model_and_header_values_stop_before_network(self):
        for variable, value in [("DIGEST_MODEL", ""), ("DIGEST_MODEL", "bad\nmodel"), ("DIGEST_API_KEY", "bad\nkey")]:
            with patch.dict("os.environ", {variable: value}):
                self.assertEqual(self.run_check(), 1)
        self.build_mock.assert_not_called()

    def test_tls_verification_and_no_redirect_handler(self):
        self.run_check()
        handlers = self.build_mock.call_args.args
        self.assertIsInstance(handlers[0], helper.NoRedirects)
        context = handlers[1]._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIsNone(handlers[0].redirect_request(None, None, 302, "", {}, "https://other.test/"))

    def test_search_single_post_uses_bounded_candidate_glm_payload(self):
        self.respond({"choices": [{"finish_reason": "stop", "message": {"content": "Do not print this body"}}]})
        self.assertEqual(self.run_check("search"), 0)
        self.opener.open.assert_called_once()
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, helper.DEFAULT_API_BASE + "/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], helper.DEFAULT_MODEL)
        self.assertLessEqual(payload["max_tokens"], 1500)
        self.assertEqual(payload["tools"], [{"type": "web_search", "web_search": {
            "enable": True, "search_query": "site:x.com AI 科技 最近", "search_result": True,
        }}])
        self.assertIn("近48小时", payload["messages"][1]["content"])
        report = self.report()
        self.assertEqual(report["choices_finish_reasons"], [{"index": 0, "finish_reason": "stop"}])
        self.assertEqual(report["source_metadata_status"], "no_candidate_links_found")
        self.assertFalse(report["daily_digest_ready"])
        self.assertTrue(report["manual_protocol_verification_required"])
        self.assert_private("Do not print this body")

    def test_named_structured_links_are_candidates_with_paths_only(self):
        self.respond({
            "new_search_metadata": [{"url": "https://x.com/author/status/123?s=20"}],
            "choices": [{"finish_reason": "stop", "message": {
                "content": "Generated body must stay private",
                "annotations": [{"citationURL": "https://twitter.com/second/status/456"}],
            }}],
            "different_protocol_field": {"link": "https://www.x.com/third/status/789"},
        })
        self.assertEqual(self.run_check("search"), 0)
        report = self.report()
        self.assertEqual([link["url"] for link in report["possible_source_links"]], [
            "https://x.com/author/status/123", "https://x.com/second/status/456", "https://x.com/third/status/789",
        ])
        self.assertEqual(report["possible_source_links"][0]["path"], '$["new_search_metadata"][0]["url"]')
        self.assertIn("different_protocol_field", report["top_level_keys"])
        self.assertEqual(report["source_metadata_status"], "candidate_links_found")
        self.assertFalse(report["daily_digest_ready"])
        self.assertIn("manually verify", report["notice"])
        self.assertNotIn("author/status", self.output.getvalue())
        self.assert_private("Generated body must stay private")

    def test_body_links_and_generated_json_are_never_search_evidence(self):
        link = "https://x.com/body/status/123"
        self.respond({"choices": [{
            "text": link,
            "message": {
                "content": json.dumps({"url": link}),
                "reasoning_content": {"url": link},
                "tool_calls": [{"function": {"arguments": {"url": link}}}],
            },
        }], "content": [{"url": link}], "text": {"url": link}})
        self.assertEqual(self.run_check("search"), 0)
        self.assertEqual(self.report()["possible_source_links"], [])
        self.assertEqual(self.report()["source_metadata_status"], "no_candidate_links_found")
        self.assert_private(link)

    def test_external_or_invalid_urls_and_unnamed_strings_are_not_sources(self):
        invalid = [
            "https://example.test/article", "http://x.com/author/status/123",
            "https://x.com.attacker.test/author/status/123", "https://user@x.com/author/status/123",
            "https://x.com/author", "https://x.com/search?q=AI", "https://x.com/a/status/0",
            "https://x.com/a/status/123/extra", "https://x.com:443/a/status/123",
        ]
        self.respond({"results": [{"url": value} for value in invalid],
                      "not_a_named_source": "https://x.com/author/status/123"})
        self.assertEqual(self.run_check("search"), 0)
        self.assertEqual(self.report()["possible_source_links"], [])

    def test_secret_echoes_in_keys_paths_and_finish_reason_are_redacted(self):
        nested_key = 'nested-"' + self.key + "\\source"
        self.respond({
            "echo-" + self.key: {nested_key: {"url": "https://x.com/author/status/123"}},
            "Authorization: Bearer " + self.key: True,
            "choices": [{"finish_reason": self.key, "message": {"content": self.key}}],
        })
        self.assertEqual(self.run_check("search"), 0)
        report = self.report()
        self.assertEqual(len(report["possible_source_links"]), 1)
        self.assertEqual(report["choices_finish_reasons"][0]["finish_reason"], "unrecognized")
        self.assert_private()

    def test_secret_with_quotes_and_backslashes_cannot_leak_via_escaped_paths(self):
        key = 'secret-"quoted"-\\value'
        self.respond({key: {"url": "https://x.com/author/status/123"}})
        with patch.dict("os.environ", {"DIGEST_API_KEY": key}):
            self.assertEqual(self.run_check("search"), 0)
        self.assert_private(key, json.dumps(key)[1:-1])

    def test_secret_in_valid_x_handle_is_not_recorded(self):
        with patch.dict("os.environ", {"DIGEST_API_KEY": "shortkey"}):
            self.respond({"sources": [{"url": "https://x.com/shortkey/status/123"}]})
            self.assertEqual(self.run_check("search"), 0)
        self.assertEqual(self.report()["possible_source_links"], [])
        self.assert_private("shortkey")

    def test_entire_report_including_numbers_and_fixed_keys_is_sanitized(self):
        self.respond({"choices": [{"finish_reason": "stop"}]})
        with patch.dict("os.environ", {"DIGEST_API_KEY": "200"}):
            self.assertEqual(self.run_check("search"), 0)
        self.assert_private("200")
        self.assertNotEqual(self.report()["http_status"], 200)
        self.assertNotIn("private-value", json.dumps(helper._sanitize({"private-value": ["private-value"]}, ("private-value",))))

    def test_unknown_finish_reason_cannot_echo_arbitrary_text(self):
        self.respond({"choices": [{"finish_reason": "arbitrary remote generated prose"}]})
        self.assertEqual(self.run_check("search"), 0)
        self.assertEqual(self.report()["choices_finish_reasons"][0]["finish_reason"], "unrecognized")
        self.assert_private("arbitrary remote generated prose")

    def test_http_failures_have_status_without_body_headers_or_retries(self):
        for code in (302, 401, 403, 429, 500):
            body = Mock(wraps=BytesIO(self.key.encode()))
            self.opener.open.reset_mock()
            self.opener.open.side_effect = HTTPError("https://private.test/" + self.key, code, self.key,
                                                    {"Authorization": self.key}, body)
            self.assertEqual(self.run_check("search"), 1)
            self.opener.open.assert_called_once()
            self.assertEqual(self.report()["http_status"], code)
            body.read.assert_not_called()
        self.assert_private("private.test")

    def test_network_exception_details_are_never_printed_or_retried(self):
        for exception in (URLError(self.key), TimeoutError(self.key), RuntimeError(self.key)):
            self.opener.open.reset_mock()
            self.opener.open.side_effect = exception
            self.assertEqual(self.run_check(), 1)
            self.opener.open.assert_called_once()
            self.assertIsNone(self.report()["http_status"])
        self.assert_private()

    def test_unexpected_non_success_status_is_reported_safely(self):
        self.respond({"error": self.key}, 403)
        self.assertEqual(self.run_check(), 1)
        self.assertEqual(self.report()["http_status"], 403)
        self.response.read.assert_not_called()
        self.opener.open.assert_called_once()
        self.assert_private()

    def test_response_read_is_capped_at_two_mib_plus_one_and_never_retried(self):
        self.respond(b"x" * (helper.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(self.run_check("search"), 1)
        self.response.read.assert_called_once_with(2 * 1024 * 1024 + 1)
        self.opener.open.assert_called_once()
        self.assertEqual(self.report()["error"], "Response exceeded the size limit.")

    def test_invalid_json_and_non_objects_produce_safe_errors(self):
        for body in (self.key.encode(), b"\xff", b"[]", b"null"):
            self.respond(body)
            self.assertEqual(self.run_check("search"), 1)
            self.assertFalse(self.report()["daily_digest_ready"])
        self.assert_private()

    def test_write_error_does_not_echo_report_path_or_exception(self):
        with patch("scripts.check_connection._write_report", side_effect=OSError(self.key)):
            self.assertEqual(helper.main(["--mode", "api", "--report", self.key]), 1)
        self.assert_private()

    def test_report_replaces_stale_contents_with_sanitized_output(self):
        self.report_path.write_text(self.key)
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(self.report_path.stat().st_mode & 0o777, 0o600)
        self.assert_private()


if __name__ == "__main__":
    unittest.main()
