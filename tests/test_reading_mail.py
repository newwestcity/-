"""Reader-facing source, trust-boundary and delivery rendering checks."""

import unittest

from x_digest.mail import build_reading_message


def article(**updates):
    result = {
        "source": "X", "id": "x:123456789",
        "url": "https://x.com/test_author/status/123456789",
        "title": "测试导读标题", "author": "测试作者", "category": "AI",
        "summary": "这是合成测试摘要。", "reason": "有具体论证与可学习的方法。",
        "learning_points": ["理解问题如何拆解", "学习怎样验证结果"],
        "scores": {"depth": 25, "evidence": 20, "learning": 20, "readability": 8, "timeliness": 9},
        "score_total": 82, "published_at": "2026-10-20T18:00:00+00:00",
        "reading_kind": "recent", "content_basis": "excerpt",
        "estimated_read_minutes": 6,
        "text": "PRIVATE_BODY_DO_NOT_REPOST", "evidence_excerpt": "PRIVATE_QUOTE_DO_NOT_REPOST",
    }
    result.update(updates)
    return result


def project(**updates):
    result = {
        "source": "GitHub", "id": "github:test-owner/demo-tool",
        "url": "https://github.com/test-owner/demo-tool",
        "title": "demo-tool：合成测试工具",
        "summary": "用于渲染测试的自动化工具。", "reason": "有快速开始与具体复刻练习。",
        "learning_points": ["搭建命令行接口", "验证一条自动化流程"],
        "scores": {"learnability": 25, "reproducibility": 25, "practicality": 17, "popularity": 6, "maintenance": 8},
        "score_total": 81, "stars": 1234, "license": "MIT", "language": "Python",
        "pushed_at": "2026-10-20T18:00:00+00:00", "weekly_star_growth": None,
        "difficulty": "中等", "prerequisites": ["Python 基础", "本地 Python 环境"],
        "effort_estimate": "估计：半天到一天",
        "reproduction_steps": ["按快速开始启动示例", "替换一处数据输入", "为失败情况加一条校验"],
        "readme_text": "PRIVATE_README_DO_NOT_REPOST", "reproduction_verified": False,
    }
    result.update(updates)
    return result


class ReadingMessageTests(unittest.TestCase):
    def build(self, items, **overrides):
        arguments = {"edition": "daily", "date": "2026-10-21", "sender": "sender@qq.com", "recipient": "reader@qq.com"}
        arguments.update(overrides)
        return build_reading_message(items, **arguments)

    def bodies(self, message):
        return [message.get_body(preferencelist=(kind,)).get_content() for kind in ("plain", "html")]

    def test_daily_sources_long_read_intro_and_explicit_quality_shortfall(self):
        snowball = article(
            source="雪球", id="xueqiu:789", url="https://xueqiu.com/123/789",
            category="价值投资", content_basis="original_text", title="雪球测试标题",
            assessment_basis="依据已核实文章正文评审",
        )
        message = self.build([article(), snowball], coverage={"targets": {"X": 10, "雪球": 4}, "source_status": {"X": "partial", "雪球": "available"}})
        self.assertEqual(message["Subject"], "每日阅读精选｜2026-10-21｜2篇")
        self.assertEqual(message["To"], "reader@qq.com")
        for body in self.bodies(message):
            self.assertIn("X：本期 1 篇，目标 10 篇", body)
            self.assertIn("雪球：本期 1 篇，目标 4 篇", body)
            self.assertIn("宁缺毋滥", body)
            self.assertIn("深度25/30", body)
            self.assertIn("能学到", body)
            self.assertIn("未核实完整长帖或讨论串", body)
            self.assertIn("已取得的雪球原文正文", body)
            self.assertIn("2026-10-21（北京时间）", body)
            self.assertNotIn("PRIVATE_BODY", body)
            self.assertNotIn("PRIVATE_QUOTE", body)

    def test_all_untrusted_display_content_is_html_escaped(self):
        malicious = '<img src=x onerror="alert(1)">&'
        message = self.build([article(title=malicious, author=malicious, category=malicious, summary=malicious, reason=malicious, learning_points=[malicious])])
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertNotIn("<img", html)
        self.assertGreaterEqual(html.count("&lt;img"), 6)
        self.assertIn("&amp;", html)

    def test_weekly_exposes_learning_plan_without_claiming_run_or_weekly_growth(self):
        message = self.build([project()], edition="weekly")
        self.assertEqual(message["Subject"], "每周 GitHub 项目精选｜2026-10-21｜1个")
        for body in self.bodies(message):
            self.assertIn("1,234 stars", body)
            self.assertIn("许可证 MIT", body)
            self.assertIn("可复刻性25/30", body)
            self.assertIn("前置条件", body)
            self.assertIn("复刻练习", body)
            self.assertIn("未实际安装或运行", body)
            self.assertIn("无可比较的周涨星数据", body)
            self.assertNotIn("PRIVATE_README", body)

    def test_only_real_snapshot_growth_is_labeled_when_provided(self):
        message = self.build([project(weekly_star_growth=120)], edition="weekly")
        self.assertIn("7 天星标变化 +120", self.bodies(message)[0])

    def test_bad_urls_and_wrong_source_identity_are_rejected(self):
        cases = [
            article(url="https://x.com.evil.test/test/status/123456789"),
            article(url="https://x.com/test/status/123456789?token=secret"),
            article(url="https://x.com/test/status/123456789#fragment"),
            article(id="x:987"),
            article(source="雪球", url="https://xueqiu.com/123/789", id="xueqiu:788"),
            article(source="雪球", url="https://xueqiu.com@evil.test/123/789", id="xueqiu:789"),
            article(source="雪球", url="https://xueqiu.com/123/789/extra", id="xueqiu:789"),
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.build([item])
        for item in (project(id="github:test-owner/wrong"), project(url="https://github.com/test-owner/demo-tool/tree/main")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.build([item], edition="weekly")

    def test_editorial_score_shape_and_total_are_checked(self):
        for item in (
            article(score_total=99), article(scores={"depth": 82}),
            article(scores={"depth": 31, "evidence": 20, "learning": 20, "readability": 8, "timeliness": 9}),
            article(scores={"depth": True, "evidence": 20, "learning": 20, "readability": 8, "timeliness": 9}),
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.build([item])

    def test_classic_and_unknown_dates_cannot_appear_recent(self):
        message = self.build([article(reading_kind="classic", published_at="2020-01-01T18:00:00+00:00")])
        self.assertIn("经典阅读 · 原文 2020-01-02", self.bodies(message)[0])
        message = self.build([article(reading_kind="undated", published_at="")])
        self.assertIn("原文日期未确认；不计作近期内容", self.bodies(message)[0])
        with self.assertRaises(ValueError):
            self.build([article(reading_kind="classic", published_at="")])
        with self.assertRaises(ValueError):
            self.build([article(published_at="2026-10-20T18:00:00")])

    def test_truncated_snowball_is_labeled_as_excerpt(self):
        message = self.build([article(source="雪球", id="xueqiu:789", url="https://xueqiu.com/123/789", content_basis="original_text", text_truncated=True)])
        self.assertIn("已取得的雪球正文摘录", self.bodies(message)[0])
        self.assertNotIn("已取得的雪球原文正文", self.bodies(message)[0])
        message = self.build([article(source="雪球", id="xueqiu:789", url="https://xueqiu.com/123/789", content_basis="original_text", text_characters=10000, assessment_basis="仅依据已核实正文摘录评审")])
        self.assertIn("已取得的雪球正文摘录", self.bodies(message)[0])

    def test_source_statuses_are_fixed_codes_not_raw_provider_errors(self):
        for coverage in (
            {"notes": ["secret raw error"]},
            {"source_status": {"X": "Bearer secret"}},
            {"source_status": {"X": {"error": "secret"}}},
            {"targets": {"X": True}},
            {"targets": {"GitHub": 10}},
        ):
            with self.subTest(coverage=coverage), self.assertRaises(ValueError):
                self.build([article()], coverage=coverage)

    def test_editions_duplicates_headers_and_invalid_dates_fail_before_rendering(self):
        for field in ("sender", "recipient", "date"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build([article()], **{field: "reader@qq.com\r\nBcc: attacker@test.com"})
        for date in ("2026-2-1", "2026-02-30", "bad"):
            with self.subTest(date=date), self.assertRaises(ValueError):
                self.build([article()], date=date)
        for items, edition in (([article()], "weekly"), ([project()], "daily"), ([article(), article(id="123456789")], "daily")):
            with self.subTest(edition=edition), self.assertRaises(ValueError):
                self.build(items, edition=edition)

    def test_empty_edition_has_no_invented_items_or_links(self):
        message = self.build([])
        text = self.bodies(message)[0]
        self.assertIn("本期没有通过筛选的内容", text)
        self.assertIn("X：本期 0 篇", text)
        self.assertIn("雪球：本期 0 篇", text)
        self.assertNotIn("https://", text)


if __name__ == "__main__":
    unittest.main()
