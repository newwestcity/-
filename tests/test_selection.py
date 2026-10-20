import unittest
from datetime import datetime, timezone

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

    def test_future_and_old_post_ids_are_rejected_using_snowflake_timestamp(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        def snowflake(hours):
            return str((int(now.timestamp() * 1000) + hours * 3600000 - 1288834974657) << 22)
        candidates = [post(snowflake(-1)), post(snowflake(-200)), post(snowflake(24))]
        chosen = select_posts(candidates, categories=["AI"], now=now,
                              cited_urls=[p["url"] for p in candidates])
        self.assertEqual([p["id"] for p in chosen], [snowflake(-1)])


if __name__ == "__main__":
    unittest.main()
