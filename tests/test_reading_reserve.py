"""Reserve candidates retain provenance and cannot bypass date/quality policy."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from x_digest.app import load_settings
from x_digest.reading_reserve import load_reserve, save_reserve


NOW = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)


def article(number=1, *, source="X", age=1, kind="recent", checked=NOW, category=None):
    published = NOW - timedelta(days=age) if age is not None else None
    if source == "X":
        identifier = str(((int(published.timestamp() * 1000) - 1288834974657) << 22) + number)
        identity, url, author = "x:" + identifier, f"https://x.com/author{number}/status/{identifier}", f"author{number}"
    else:
        identity, url, author = f"xueqiu:{number}", f"https://xueqiu.com/{1000 + number}/{number}", f"雪球作者{number}"
    value = {
        "source": source, "id": identity, "url": url, "author": author,
        "published_at": published.isoformat() if published else "", "verified_at": checked.isoformat(),
        "content_basis": "excerpt" if source == "X" else "original_text", "reading_kind": kind,
        "title": "有具体论据的合成阅读材料", "summary": "这是测试用的合成摘要。", "reason": "可学习推理和检验方法。",
        "learning_points": ["识别问题与假设", "检验论据是否充分"],
        "scores": {"depth": 25, "evidence": 20, "learning": 20, "readability": 9, "timeliness": 8},
        "score_total": 82, "evidence_excerpt": "这是一条有十二个以上字符的合成引句。",
        "content_form": "long_post" if source == "X" else "article", "is_long_read": True,
        "category": category or ("AI" if source == "X" else "价值投资"), "text_characters": 1200,
        "estimated_read_minutes": 5,
    }
    if source == "雪球":
        value["author_id"] = str(1000 + number)
    return value


def link(number=1, *, discovered=NOW):
    return {"source": "雪球", "id": f"xueqiu:{number}", "url": f"https://xueqiu.com/{1000+number}/{number}",
            "author_id": str(1000+number), "title": "企业财报与长期投资的分析方法", "reading_kind": "link_candidate",
            "content_basis": "search_metadata", "published_at": "", "discovered_at": discovered.isoformat()}


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "reading-reserve.json"
        self.settings = load_settings(Path(__file__).resolve().parents[1] / "settings.json")

    def load(self, *, now=NOW, seen=None):
        return load_reserve(self.path, settings=self.settings, seen=seen or set(), now=now)

    def save(self, values, *, now=NOW):
        save_reserve(self.path, values, now=now)

    def test_valid_verified_candidates_are_reusable_without_network(self):
        values = [article(), article(2, source="雪球"), link(3)]
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
            self.save(values)
            chosen, diagnostic = self.load()
        self.assertEqual({i["id"] for i in chosen}, {i["id"] for i in values})
        self.assertEqual(diagnostic["items_accepted"], 3)

    def test_expired_recent_cannot_become_classic(self):
        old_recent = article(age=8, category="投资")
        valid_classic = article(2, age=100, kind="classic", category="投资")
        self.save([old_recent, valid_classic])
        items, diagnostic = self.load()
        self.assertEqual([i["id"] for i in items], [valid_classic["id"]])
        self.assertEqual(diagnostic["items_expired"], 1)
        self.assertEqual(items[0]["reading_kind"], "classic")

    def test_stale_offers_and_classic_snapshots_are_rejected(self):
        expired_offer = article(category="学生羊毛", age=4)
        stale_classic = article(2, kind="classic", age=90, category="读书", checked=NOW-timedelta(days=31))
        forbidden_classic = article(3, kind="classic", age=90, category="科技")
        self.save([expired_offer, stale_classic, forbidden_classic])
        items, diagnostic = self.load()
        self.assertEqual(items, [])
        self.assertEqual(diagnostic["items_expired"], 2)
        self.assertEqual(diagnostic["items_invalid"], 1)

    def test_seen_and_blocked_accounts_do_not_return(self):
        values = [article(), article(2)]
        self.settings["blocked_accounts"] = ["@AUTHOR2"]
        self.save(values)
        items, diagnostic = self.load(seen={values[0]["id"]})
        self.assertEqual(items, [])
        self.assertEqual(diagnostic["items_seen"], 1)
        self.assertEqual(diagnostic["items_invalid"], 1)

    def test_bad_cache_is_optional_and_missing_cache_is_distinct(self):
        self.assertEqual(self.load()[1]["missing_cache"], 1)
        for raw in (b"not JSON", b'{"version":1,"version":1,"saved_at":"2026-10-23T00:00:00Z","items":[]}',
                    json.dumps({"version": 1, "saved_at": NOW.isoformat(), "items": [], "unexpected": True}).encode()):
            with self.subTest(raw=raw[:20]):
                self.path.write_bytes(raw)
                items, diagnostic = self.load()
                self.assertEqual(items, [])
                self.assertEqual(diagnostic["invalid_cache"], 1)

    def test_links_and_undated_articles_expire_after_48_hours(self):
        stale = NOW - timedelta(hours=49)
        undated = article(4, source="雪球", age=None, kind="undated", checked=stale)
        self.save([link(discovered=stale), undated, link(2)])
        items, diagnostic = self.load()
        self.assertEqual([i["id"] for i in items], ["xueqiu:2"])
        self.assertEqual(diagnostic["items_expired"], 2)
        self.assertNotIn("scores", items[0])
        self.settings["reading"]["xueqiu_link_fallback"] = False
        self.assertEqual(self.load()[0], [])

    def test_provenance_identity_score_and_rendering_errors_are_rejected(self):
        valid = article()
        variants = [
            {**valid, "url": valid["url"].replace("author1", "different")},
            {**valid, "published_at": (NOW-timedelta(days=2)).isoformat()},
            {**valid, "score_total": 99},
            {**valid, "summary": ""},
            {**valid, "content_basis": "search_metadata"},
            {**link(), "score_total": 90},
            {**link(), "title": "推荐股票稳赚加群"},
            {**valid, "unknown": "bad"},
        ]
        for item in variants:
            with self.subTest(item=item):
                self.path.write_text(json.dumps({"version": 1, "saved_at": NOW.isoformat(), "items": [item]}))
                self.assertEqual(self.load()[0], [])
                self.assertEqual(self.load()[1]["items_invalid"], 1)

    def test_save_uses_latest_snapshot_and_drops_source_bodies_and_unknown_fields(self):
        old = article(checked=NOW-timedelta(hours=1))
        new = article()
        new.update(text="SOURCE_BODY", SMTP_AUTH_CODE="not-persisted", extra="unknown")
        self.save([old, new])
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["verified_at"], NOW.isoformat())
        self.assertNotIn("SOURCE_BODY", self.path.read_text())
        self.assertNotIn("SMTP_AUTH_CODE", self.path.read_text())
        self.assertNotIn("extra", data["items"][0])

    def test_save_does_not_keep_email_or_api_key_in_public_display_fields(self):
        self.save([{**article(), "summary": "联系私密邮箱 test-reader@qq.com"},
                   {**article(2), "reason": "sk-12345678901234567890"}])
        self.assertEqual(json.loads(self.path.read_text())["items"], [])

    def test_save_is_bounded_and_atomic_on_write_failure(self):
        self.save([link(i) for i in range(1, 141)])
        self.assertEqual(len(json.loads(self.path.read_text())["items"]), 120)
        before = self.path.read_bytes()
        with patch("x_digest.reading_reserve.os.replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.save([link(199)])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
