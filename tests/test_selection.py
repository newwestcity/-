import unittest
from datetime import datetime, timedelta, timezone

from x_digest.selection import canonical_post_url, select_posts


def post(n, category="AI", handle="author"):
    return {"url": f"https://x.com/{handle}/status/{n}", "category": category,
            "summary": "测试用摘要，非真实推荐", "reason": "测试用理由"}


class SelectionTests(unittest.TestCase):
    def test_generated_links_need_independent_source_citations(self):
        found, invented = post(101), post(102)
        chosen = select_posts([found, invented], categories=["AI"], cited_urls=[found["url"]])
        self.assertEqual([p["id"] for p in chosen], ["101"])

    def test_previous_delivery_and_twitter_alias_are_deduplicated_by_id(self):
        a, b = post(101), post(102)
        alias = {**a, "url": "https://twitter.com/author/status/101?s=20"}
        chosen = select_posts([a, alias, b], categories=["AI"], seen_ids={"102"},
                              cited_urls=[a["url"], b["url"]])
        self.assertEqual([p["id"] for p in chosen], ["101"])

    def test_available_topics_get_slots_before_extra_popular_posts(self):
        candidates = [post(n, "AI", f"user{n}") for n in range(101, 106)]
        candidates += [post(201, "读书", "reader"), post(301, "投资", "investor")]
        chosen = select_posts(candidates, categories=["AI", "读书", "投资"], target_count=3,
                              cited_urls=[p["url"] for p in candidates])
        self.assertEqual([p["category"] for p in chosen], ["AI", "读书", "投资"])

    def test_blacklist_and_author_limits_can_yield_fewer_than_requested(self):
        candidates = [post(101), post(102), post(103), post(201, handle="blocked")]
        chosen = select_posts(candidates, categories=["AI"], max_per_author=2,
                              blocked_accounts=["@BLOCKED"],
                              cited_urls=[p["url"] for p in candidates])
        self.assertEqual([p["id"] for p in chosen], ["101", "102"])

    def test_external_links_and_lookalike_hosts_are_rejected(self):
        for url in ["https://x.com.evil.test/a/status/101", "https://x.com@evil.test/a/status/101",
                    "javascript:alert(1)", "https://x.com/a", "https://x.com/a/status/0"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_post_url(url)

    def test_citation_author_cannot_be_replaced_to_bypass_blacklist(self):
        forged = post(101, handle="apparentlygood")
        chosen = select_posts([forged], categories=["AI"], blocked_accounts=["blocked"],
                              cited_urls=["https://x.com/blocked/status/101"])
        self.assertEqual(chosen, [])

    def test_mobile_and_media_links_are_deduplicated_by_post_id(self):
        candidates = [post(101), {**post(101), 'url': 'https://mobile.twitter.com/author/status/101/photo/1'}]
        chosen = select_posts(candidates, categories=['AI'], cited_urls=['https://x.com/author/status/101/video/1'])
        self.assertEqual([p['id'] for p in chosen], ['101'])
        self.assertEqual(chosen[0]['url'], 'https://x.com/author/status/101')

    def test_authorless_routing_url_cannot_supply_a_generated_author(self):
        for source in ('https://x.com/i/web/status/101', 'https://x.com/i/status/101'):
            self.assertEqual(canonical_post_url(source), ('https://x.com/i/status/101', '101', ''))
            chosen = select_posts([post(101, handle='invented')], categories=['AI'], cited_urls=[source])
            self.assertEqual(chosen, [])

    def test_named_source_can_resolve_an_authorless_citation_of_the_same_id(self):
        chosen = select_posts([post(101, handle='generated')], categories=['AI'],
                              cited_urls=['https://x.com/i/web/status/101', 'https://twitter.com/actual/status/101'])
        self.assertEqual(chosen[0]['author'], '@actual')
        self.assertEqual(chosen[0]['url'], 'https://x.com/actual/status/101')

    def test_future_and_old_post_ids_are_rejected_using_snowflake_timestamp(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        def snowflake(hours):
            return str((int(now.timestamp() * 1000) + hours * 3600000 - 1288834974657) << 22)
        candidates = [post(snowflake(-1)), post(snowflake(-200)), post(snowflake(24))]
        chosen = select_posts(candidates, categories=["AI"], now=now,
                              cited_urls=[p["url"] for p in candidates])
        self.assertEqual([p["id"] for p in chosen], [snowflake(-1)])


class ClassicSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
        self.sequence = 0
        self.categories = ["科技", "AI", "股市", "投资", "读书", "学生羊毛"]

    def candidate(self, *, days=0, hours=0, category="投资", handle=None):
        self.sequence += 1
        published = self.now - timedelta(days=days, hours=hours)
        post_id = str(((int(published.timestamp() * 1000) - 1288834974657) << 22) | self.sequence)
        return post(post_id, category, handle or f"user{self.sequence}")

    def ids(self, candidates):
        return {canonical_post_url(item["url"])[1] for item in candidates}

    def choose(self, candidates, **overrides):
        args = {"categories": self.categories, "now": self.now,
                "cited_urls": [item["url"] for item in candidates]}
        args.update(overrides)
        return select_posts(candidates, **args)

    def test_model_classic_flags_and_forged_dates_cannot_supply_verification(self):
        item = self.candidate(days=30, category="读书")
        item.update(reading_kind="classic", is_classic=True, published_at=self.now.isoformat())
        diagnostics = {}
        chosen = self.choose([item], max_classic_count=3, diagnostics=diagnostics)
        self.assertEqual(chosen, [])
        self.assertEqual(diagnostics["too_old"], 1)
        self.assertEqual(diagnostics["classic_eligible"], 0)
        self.assertEqual(diagnostics["classic_selected"], 0)

    def test_only_trusted_old_finance_or_reading_ids_can_be_classics(self):
        trusted = self.candidate(days=30, category="投资")
        unverified = self.candidate(days=30, category="读书")
        chosen = self.choose([trusted, unverified], max_classic_count=3,
                             verified_classic_ids=self.ids([trusted]))
        self.assertEqual({item["id"] for item in chosen}, self.ids([trusted]))
        self.assertEqual(chosen[0]["reading_kind"], "classic")

    def test_old_technology_ai_and_offers_cannot_use_classic_exception(self):
        candidates = [self.candidate(days=30, category=topic) for topic in ("科技", "AI", "学生羊毛")]
        chosen = self.choose(candidates, max_classic_count=3,
                             verified_classic_ids=self.ids(candidates))
        self.assertEqual(chosen, [])

    def test_classic_count_is_capped_at_three_and_diagnostics_count_eligibility(self):
        candidates = [self.candidate(days=30, category=topic) for topic in ("投资", "读书", "股市", "投资")]
        diagnostics = {}
        chosen = self.choose(candidates, max_classic_count=3,
                             verified_classic_ids=self.ids(candidates), diagnostics=diagnostics)
        self.assertEqual(len(chosen), 3)
        self.assertTrue(all(item["reading_kind"] == "classic" for item in chosen))
        self.assertEqual(diagnostics["classic_eligible"], 4)
        self.assertEqual(diagnostics["classic_selected"], 3)
        self.assertEqual(diagnostics["recent_selected"], 0)

    def test_defaults_reject_old_posts_even_if_trusted_ids_are_supplied(self):
        item = self.candidate(days=30)
        self.assertEqual(self.choose([item], verified_classic_ids=self.ids([item])), [])

    def test_recent_posts_take_precedence_over_an_earlier_classic_candidate(self):
        classic = self.candidate(days=30, category="读书")
        recent = [self.candidate(days=1, category=topic) for topic in ("AI", "投资", "读书")]
        diagnostics = {}
        chosen = self.choose([classic, *recent], target_count=3, max_classic_count=3,
                             verified_classic_ids=self.ids([classic]), diagnostics=diagnostics)
        self.assertEqual({item["id"] for item in chosen}, self.ids(recent))
        self.assertEqual({item["category"] for item in chosen}, {"AI", "投资", "读书"})
        self.assertTrue(all(item["reading_kind"] == "recent" for item in chosen))
        self.assertEqual(diagnostics["recent_selected"], 3)
        self.assertEqual(diagnostics["classic_selected"], 0)
        self.assertEqual(diagnostics["classic_eligible"], 1)

    def test_classic_supplements_cover_missing_topics_before_extra_same_topic_items(self):
        recent = [self.candidate(days=1, category="AI"), self.candidate(days=1, category="投资")]
        classics = [self.candidate(days=30, category=topic) for topic in ("投资", "投资", "读书", "股市")]
        chosen = self.choose([*classics, *recent], target_count=4, max_classic_count=3,
                             verified_classic_ids=self.ids(classics))
        self.assertEqual([item["reading_kind"] for item in chosen], ["recent", "recent", "classic", "classic"])
        self.assertEqual({item["category"] for item in chosen}, {"AI", "投资", "读书", "股市"})

    def test_recent_and_classic_share_author_and_category_limits(self):
        recent = self.candidate(days=1, category="投资", handle="shared")
        classics = [self.candidate(days=30, category="读书", handle="shared"),
                    self.candidate(days=30, category="投资"), self.candidate(days=30, category="股市")]
        chosen = self.choose([*classics, recent], max_classic_count=3,
                             max_per_author=1, max_per_category=1,
                             verified_classic_ids=self.ids(classics))
        self.assertEqual([item["category"] for item in chosen], ["投资", "股市"])

    def test_published_date_and_kind_are_computed_from_id_not_model_fields(self):
        recent = self.candidate(days=1, category="投资")
        classic = self.candidate(days=30, category="读书")
        recent.update(reading_kind="classic", published_at="1900-01-01T00:00:00Z")
        classic.update(reading_kind="recent", published_at="2999-01-01T00:00:00Z")
        chosen = self.choose([classic, recent], max_classic_count=3,
                             verified_classic_ids=self.ids([classic]))
        self.assertEqual([item["reading_kind"] for item in chosen], ["recent", "classic"])
        self.assertEqual(chosen[0]["published_at"], (self.now - timedelta(days=1)).isoformat())
        self.assertEqual(chosen[1]["published_at"], (self.now - timedelta(days=30)).isoformat())

    def test_future_verified_ids_are_still_rejected(self):
        item = self.candidate(days=-1, category="投资")
        diagnostics = {}
        chosen = self.choose([item], max_classic_count=3, verified_classic_ids=self.ids([item]),
                             diagnostics=diagnostics)
        self.assertEqual(chosen, [])
        self.assertEqual(diagnostics["future"], 1)

    def test_verification_does_not_bypass_provenance_history_or_blocklist(self):
        item = self.candidate(days=30, category="读书", handle="blocked")
        for options in ({"cited_urls": []}, {"seen_ids": self.ids([item])}, {"blocked_accounts": ["blocked"]}):
            with self.subTest(options=options):
                chosen = self.choose([item], max_classic_count=3,
                                     verified_classic_ids=self.ids([item]), **options)
                self.assertEqual(chosen, [])

    def test_age_boundaries_keep_seven_day_recent_and_seventy_two_hour_offers(self):
        candidates = [self.candidate(days=7, category="投资"),
                      self.candidate(hours=72, category="学生羊毛"),
                      self.candidate(hours=73, category="学生羊毛")]
        chosen = self.choose(candidates, max_classic_count=3,
                             verified_classic_ids=self.ids(candidates))
        self.assertEqual({item["id"] for item in chosen}, self.ids(candidates[:2]))
        self.assertTrue(all(item["reading_kind"] == "recent" for item in chosen))

    def test_classic_parameter_validation_rejects_invalid_trust_or_categories(self):
        bad_options = [
            {"max_classic_count": -1}, {"max_classic_count": 4}, {"max_classic_count": True},
            {"classic_categories": ["AI"]}, {"classic_categories": ["读书", "读书"]},
            {"categories": ["AI"], "classic_categories": ["读书"]},
            {"verified_classic_ids": ["123"]}, {"verified_classic_ids": {123}},
            {"verified_classic_ids": {"not-a-post-id"}},
            {"max_classic_count": 1, "now": None},
        ]
        for options in bad_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.choose([], **options)


if __name__ == "__main__":
    unittest.main()
