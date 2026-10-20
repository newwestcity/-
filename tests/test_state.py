import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from x_digest.state import already_sent, load_state, record_delivery, seen_post_ids


def delivery(day="2026-10-19", post_ids=None):
    return {"date": day, "sent_at": day + "T08:00:00+08:00",
            "post_ids": ["101"] if post_ids is None else post_ids}


def state(*entries):
    return {"version": 1, "deliveries": list(entries)}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.json"

    def test_only_absent_file_is_treated_as_empty_history(self):
        self.assertEqual(load_state(self.path), state())
        for content in ["{bad json", "", "null", "[]"]:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_state(self.path)
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_structurally_invalid_records_fail_instead_of_resetting_history(self):
        bad_states = [
            {"version": True, "deliveries": []},
            {"version": 2, "deliveries": []},
            {"version": 1},
            state({"date": "2026-02-30", "sent_at": "2026-10-19T08:00:00Z", "post_ids": ["101"]}),
            state({**delivery(), "sent_at": "2026-10-19T08:00:00"}),
            state({**delivery(), "post_ids": [101]}),
            state(delivery(post_ids=["101", "101"])),
            state(delivery(post_ids=["0"])),
            state(delivery(), delivery()),
        ]
        for invalid in bad_states:
            with self.subTest(invalid=invalid):
                self.path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_state(self.path)

    def test_history_window_keeps_boundary_and_excludes_expired_or_future_posts(self):
        today = date(2026, 10, 20)
        boundary = today - timedelta(days=44)
        old = today - timedelta(days=45)
        history = state(delivery(str(boundary), ["101"]), delivery(str(old), ["102"]),
                        delivery(str(today), ["103"]), delivery("2026-10-21", ["104"]))
        self.assertEqual(seen_post_ids(history, today=today), {"101", "103"})
        self.assertEqual(seen_post_ids(history, today=today, history_days=1), {"103"})

    def test_record_success_prunes_expired_history_without_mutating_input(self):
        original = state(delivery("2026-09-05", ["100"]), delivery())
        before = copy.deepcopy(original)
        new = record_delivery(self.path, original, date="2026-10-20", post_ids=["201", "202"],
                              sent_at="2026-10-20T00:00:00Z")
        self.assertEqual(original, before)
        self.assertEqual(load_state(self.path), new)
        self.assertEqual([d["date"] for d in new["deliveries"]], ["2026-10-19", "2026-10-20"])
        self.assertTrue(already_sent(new, "2026-10-20"))
        self.assertFalse(already_sent(new, "2026-10-21"))

    def test_duplicate_date_never_rewrites_history(self):
        original = state(delivery())
        self.path.write_text(json.dumps(original), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            record_delivery(self.path, original, date="2026-10-19", post_ids=["202"],
                            sent_at="2026-10-19T09:00:00+08:00")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(load_state(self.path), original)

    def test_atomic_replace_failure_preserves_file_and_object_and_cleans_temporary(self):
        original = state(delivery())
        self.path.write_text(json.dumps(original), encoding="utf-8")
        before_bytes = self.path.read_bytes()
        before_object = copy.deepcopy(original)
        with patch("x_digest.state.os.replace", side_effect=PermissionError("simulated failure")):
            with self.assertRaises(PermissionError):
                record_delivery(self.path, original, date="2026-10-20", post_ids=["202"],
                                sent_at="2026-10-20T08:00:00+08:00")
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(original, before_object)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_new_record_does_not_create_file(self):
        for kwargs in [dict(date="2026-10-20", post_ids=["bad"], sent_at="2026-10-20T08:00:00Z"),
                       dict(date="2026-10-20", post_ids=["101"], sent_at="not a timestamp")]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                record_delivery(self.path, state(), **kwargs)
            self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
