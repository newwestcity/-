import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from x_digest.reading_state import already_sent, load_state, record_delivery, seen_ids


def delivery(day="2026-10-19", ids=None, *, edition="daily", period=None):
    return {"edition": edition, "period": period or day, "date": day,
            "sent_at": day + "T08:00:00+08:00", "item_ids": ["x:101"] if ids is None else ids}


def state(*entries):
    return {"version": 2, "deliveries": list(entries)}


class ReadingStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "reading-state.json"
        self.legacy = self.path.with_name("state.json")

    def record(self, history=None, **kwargs):
        options = {"edition": "daily", "period": "2026-10-20", "date": "2026-10-20",
                   "item_ids": ["x:201", "xueqiu:201", "github:Owner/Repo"],
                   "sent_at": "2026-10-20T08:00:00+08:00"}
        options.update(kwargs)
        return record_delivery(self.path, state() if history is None else history, **options)

    def test_missing_files_are_empty_but_corruption_is_never_reset(self):
        self.assertEqual(load_state(self.path, self.legacy), state())
        for content in ["{bad", "", "null", "[]", "{\"version\":1,\"deliveries\":[]}",
                        '{"version":2,"deliveries":[1],"deliveries":[]}']:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_state(self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_daily_weekly_and_legacy_same_date_have_independent_delivery_keys(self):
        history = state(delivery("2026-10-18", edition="legacy-x"))
        self.assertFalse(already_sent(history, edition="daily", period="2026-10-18"))
        daily = self.record(history, date="2026-10-18", period="2026-10-18")
        both = self.record(daily, edition="weekly", period="2026-W42", date="2026-10-18",
                           item_ids=["github:org/project"])
        self.assertTrue(already_sent(both, edition="weekly", period="2026-W42"))
        self.assertTrue(already_sent(both, edition="daily", period="2026-10-18"))
        self.assertFalse(already_sent(both, edition="daily", period="2026-10-19"))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.record(both, edition="weekly", period="2026-W42", date="2026-10-18")
        self.assertEqual(self.path.read_bytes(), before)

    def test_item_namespace_preserves_distinct_sources_and_github_case(self):
        result = self.record()
        self.assertEqual(seen_ids(result, today=date(2026, 10, 20)),
                         {"x:201", "xueqiu:201", "github:Owner/Repo"})
        self.assertEqual(load_state(self.path), result)

    def test_example_has_its_own_daily_slot_but_participates_in_content_history(self):
        example = self.record(edition="example", item_ids=["x:301", "xueqiu:302"])
        self.assertTrue(already_sent(example, edition="example", period="2026-10-20"))
        self.assertFalse(already_sent(example, edition="daily", period="2026-10-20"))
        self.assertFalse(already_sent(example, edition="daily", period="2026-10-21"))
        self.assertEqual(seen_ids(example, today=date(2026, 10, 21)), {"x:301", "xueqiu:302"})
        both = self.record(example, item_ids=["x:401"])
        self.assertTrue(already_sent(both, edition="daily", period="2026-10-20"))
        self.assertTrue(already_sent(both, edition="example", period="2026-10-20"))
        self.assertEqual(seen_ids(both, today=date(2026, 10, 21)), {"x:301", "xueqiu:302", "x:401"})
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.record(both, edition="example", item_ids=["x:501"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_example_requires_exact_date_period_and_rejects_unlisted_editions(self):
        for edition, period in [("example", "2026-W43"), ("example", "2026-10-21"),
                                ("example", "2026-2-1"), ("example-extra", "2026-10-20"),
                                ("manual", "2026-10-20")]:
            with self.subTest(edition=edition, period=period), self.assertRaises(ValueError):
                self.record(edition=edition, period=period)
            self.assertFalse(self.path.exists())
        invalid = state(delivery("2026-10-20", edition="example", period="2026-10-21"))
        self.path.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            load_state(self.path)

    def test_invalid_ids_never_create_history(self):
        invalid = [["x:0"], ["x:001"], ["x:-1"], ["x:1" + "2" * 25], [1],
                   ["other:1"], ["xueqiu:abc"], ["github:org"], ["github:org/../repo"],
                   ["github:org/.."], ["github:-org/repo"], ["github:org/repo?x=1"],
                   ["x:1", "x:1"], ["github:Org/Repo", "github:org/repo"],
                   [f"x:{index}" for index in range(1, 102)]]
        for ids in invalid:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.record(item_ids=ids)
            self.assertFalse(self.path.exists())

    def test_invalid_records_fail_closed(self):
        invalid = [{"version": True, "deliveries": []}, {"version": 2},
                   state({**delivery(), "edition": []}),
                   state({**delivery(), "edition": "other"}),
                   state({**delivery(), "date": "2026-02-30"}),
                   state({**delivery(), "period": "2026-10-20"}),
                   state({**delivery(), "sent_at": "2026-10-19T08:00:00"}),
                   state({**delivery(), "item_ids": ["xueqiu:1"], "edition": "legacy-x"}),
                   state(delivery(), delivery())]
        for value in invalid:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_state(self.path)

    def test_iso_week_validation_including_year_boundary(self):
        valid = self.record(edition="weekly", period="2020-W53", date="2021-01-03")
        self.assertTrue(already_sent(valid, edition="weekly", period="2020-W53"))
        for period in ["2021-W53", "2026-W00", "2026-W54", "2026-W1", "2026-42", "0000-W01"]:
            with self.subTest(period=period), self.assertRaises(ValueError):
                already_sent(state(), edition="weekly", period=period)
        with self.assertRaises(ValueError):
            self.record(edition="weekly", period="2026-W43", date="2026-10-18")

    def test_seen_window_includes_boundary_excludes_expired_and_future(self):
        today = date(2026, 10, 20)
        boundary = today - timedelta(days=89)
        expired = today - timedelta(days=90)
        history = state(delivery(str(boundary), ["x:1"]), delivery(str(expired), ["x:2"]),
                        delivery(str(today), ["xueqiu:3"]), delivery("2026-10-21", ["x:4"]))
        self.assertEqual(seen_ids(history, today=today), {"x:1", "xueqiu:3"})
        self.assertEqual(seen_ids(history, today=today, history_days=1), {"xueqiu:3"})

    def test_record_prunes_at_180_day_boundary_and_preserves_future_records(self):
        day = date(2026, 10, 20)
        history = state(delivery(str(day - timedelta(days=180)), ["x:1"]),
                        delivery(str(day - timedelta(days=179)), ["x:2"]),
                        delivery("2026-10-21", ["x:3"]))
        original = copy.deepcopy(history)
        updated = self.record(history)
        self.assertEqual(history, original)
        self.assertEqual({item for row in updated["deliveries"] for item in row["item_ids"]},
                         {"x:2", "x:3", "x:201", "xueqiu:201", "github:Owner/Repo"})
        self.assertEqual(load_state(self.path), updated)

    def test_legacy_import_and_repeated_merge_do_not_lose_first_x_or_mark_daily_sent(self):
        legacy = {"version": 1, "deliveries": [{"date": "2026-10-19",
                  "sent_at": "2026-10-19T08:00:00+08:00", "post_ids": ["1812344441312813407"]}]}
        self.legacy.write_text(json.dumps(legacy), encoding="utf-8")
        first = load_state(self.path, self.legacy)
        self.assertFalse(self.path.exists())
        self.assertEqual(seen_ids(first, today=date(2026, 10, 20)), {"x:1812344441312813407"})
        self.assertFalse(already_sent(first, edition="daily", period="2026-10-19"))
        self.assertFalse(already_sent(first, edition="weekly", period="2026-W43"))
        updated = self.record(first)
        self.assertEqual(load_state(self.path, self.legacy), updated)
        legacy["deliveries"][0]["post_ids"].append("102")
        self.legacy.write_text(json.dumps(legacy), encoding="utf-8")
        merged = load_state(self.path, self.legacy)
        self.assertEqual(len(merged["deliveries"]), 2)
        self.assertIn("x:102", seen_ids(merged, today=date(2026, 10, 20)))
        self.assertEqual(load_state(self.path), updated)  # Importing never writes.

    def test_corrupt_legacy_fails_even_when_v2_exists(self):
        self.record()
        for content in ["null", "{broken", json.dumps({"version": 1, "deliveries": [
                {"date": "2026-10-19", "sent_at": "2026-10-19T08:00:00Z", "post_ids": ["bad"]}]})]:
            with self.subTest(content=content):
                self.legacy.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_state(self.path, self.legacy)

    def test_atomic_replace_failure_preserves_file_state_and_removes_temporary(self):
        original = self.record()
        before_bytes, before_object = self.path.read_bytes(), copy.deepcopy(original)
        with patch("x_digest.reading_state.os.replace", side_effect=PermissionError("simulated")):
            with self.assertRaises(PermissionError):
                self.record(original, period="2026-10-21", date="2026-10-21")
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(original, before_object)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_fsync_failure_leaves_existing_file_untouched(self):
        original = self.record()
        before = self.path.read_bytes()
        with patch("x_digest.reading_state.os.fsync", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self.record(original, period="2026-10-21", date="2026-10-21")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_bounded_input_and_history_options(self):
        self.path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            load_state(self.path)
        self.path.unlink()
        for count in [0, 181, True, 1.5]:
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    self.record(history_days=count)
                with self.assertRaises(ValueError):
                    seen_ids(state(), today=date(2026, 10, 20), history_days=count)
        with self.assertRaises(ValueError):
            seen_ids(state(), today=datetime(2026, 10, 20))
        with self.assertRaises(ValueError):
            already_sent(state(), edition="legacy-x", period="2026-10-20")


if __name__ == "__main__":
    unittest.main()
