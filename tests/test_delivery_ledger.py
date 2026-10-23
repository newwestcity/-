"""Exercise durable sends and crash recovery with a local GitHub API model."""

import base64
from copy import deepcopy
from datetime import date
import io
import json
from urllib import error
import unittest
from unittest.mock import Mock, patch

from x_digest.delivery_ledger import BRANCH, Ledger, LedgerError, _NoRedirects, open_ledger


ENV = {"GITHUB_REPOSITORY": "fixture/recommendations", "GH_TOKEN": "test-secret-never-log"}
EMPTY = {"version": 2, "deliveries": []}


def entry(day="2026-10-23", *, ids=None, edition="daily", period=None):
    return {"edition": edition, "period": period or day, "date": day,
            "item_ids": ids or ["xueqiu:123"], "sent_at": day + "T08:00:00+08:00"}


def state(*entries):
    return {"version": 2, "deliveries": list(entries)}


class Response:
    def __init__(self, content):
        self.content = json.dumps(content).encode() if not isinstance(content, bytes) else content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.content[:size]


class Github:
    """Small API model: content updates require the previously observed SHA."""
    def __init__(self):
        self.ref = False
        self.content = None
        self.counter = 0
        self.sha = None
        self.calls = []
        self.raise_status = None

    def _next(self):
        self.counter += 1
        return f"{self.counter:040x}"

    def open(self, req, timeout):
        self.calls.append((req, timeout))
        path = req.full_url.partition("/repos/fixture/recommendations")[2]
        payload = json.loads(req.data) if req.data else None
        if self.raise_status:
            raise error.HTTPError(req.full_url, self.raise_status, "secret error", {}, io.BytesIO(b"test-secret-never-log"))
        if path == "/git/ref/heads/" + BRANCH:
            if not self.ref:
                raise error.HTTPError(req.full_url, 404, "no ref", {}, None)
            return Response({"object": {"sha": self.sha}})
        if path == "/git/trees":
            self.content = payload["tree"][0]["content"].encode()
            self.sha = self._next()
            return Response({"sha": self.sha})
        if path == "/git/commits":
            return Response({"sha": self._next()})
        if path == "/git/refs":
            if self.ref:
                raise error.HTTPError(req.full_url, 422, "raced", {}, None)
            self.ref = True
            return Response({"ref": payload["ref"]})
        if path.startswith("/contents/"):
            if self.content is None:
                raise error.HTTPError(req.full_url, 404, "no file", {}, None)
            if req.method == "PUT":
                if payload["sha"] != self.sha:
                    raise error.HTTPError(req.full_url, 409, "conflict", {}, None)
                self.sha = self._next()
                self.content = base64.b64decode(payload["content"])
                return Response({"content": {"sha": self.sha}})
            return Response({"type": "file", "encoding": "base64", "sha": self.sha,
                             "content": base64.b64encode(self.content).decode()})
        raise AssertionError("Unexpected API path " + path)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.api = Github()
        patcher = patch("x_digest.delivery_ledger.request.build_opener", return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict("os.environ", ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_initial_migration_creates_orphan_branch_atomically(self):
        history = state(entry("2026-10-22"))
        ledger = open_ledger(history)
        self.assertEqual(ledger.history, history)
        posts = [(req.full_url, json.loads(req.data)) for req, _ in self.api.calls if req.method == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0][1].keys(), {"tree"})
        self.assertEqual(posts[1][1]["parents"], [])
        self.assertEqual(posts[2][1]["ref"], "refs/heads/" + BRANCH)
        self.assertNotIn(ENV["GH_TOKEN"], self.api.content.decode())
        self.assertTrue(all(timeout == 30 for _, timeout in self.api.calls))

    def test_durable_history_overrides_empty_or_stale_cache(self):
        expected = state(entry("2026-10-22"))
        open_ledger(expected)
        self.api.calls.clear()
        ledger = open_ledger(state(entry("2026-10-01")))
        self.assertEqual(ledger.history, expected)
        self.assertEqual([req.method for req, _ in self.api.calls], ["GET", "GET"])
        copy = ledger.history
        copy["deliveries"].clear()
        self.assertEqual(ledger.history, expected)

    def test_existing_ledger_does_not_call_corrupt_cache_loader(self):
        expected = state(entry("2026-10-22"))
        open_ledger(expected)
        loader = Mock(side_effect=ValueError("Corrupt cache must be ignored"))
        ledger = open_ledger(loader, allow_create=False)
        loader.assert_not_called()
        self.assertEqual(ledger.history, expected)

    def test_missing_ledger_cannot_silently_bootstrap_in_production(self):
        loader = Mock(return_value=EMPTY)
        with self.assertRaisesRegex(LedgerError, "禁止自动重建"):
            open_ledger(loader, allow_create=False)
        loader.assert_not_called()
        self.assertFalse(self.api.ref)
        self.assertEqual(len(self.api.calls), 1)
        self.assertTrue(all(req.method == "GET" for req, _ in self.api.calls))

    def test_initial_migration_calls_lazy_loader_once_and_validates_history(self):
        expected = state(entry("2026-10-22"))
        loader = Mock(return_value=expected)
        ledger = open_ledger(loader)
        loader.assert_called_once_with()
        self.assertEqual(ledger.history, expected)

    def test_bad_bootstrap_cache_is_not_reset_to_empty_history(self):
        loader = Mock(side_effect=ValueError("Corrupt cache"))
        with self.assertRaisesRegex(LedgerError, "首次迁移"):
            open_ledger(loader)
        loader.assert_called_once_with()
        self.assertFalse(self.api.ref)
        self.assertTrue(all(req.method == "GET" for req, _ in self.api.calls))

    def test_crash_after_reservation_prevents_repeat_before_paid_work(self):
        first = open_ledger(EMPTY)
        first.reserve(**entry())
        second = open_ledger(EMPTY)
        with self.assertRaisesRegex(LedgerError, "未确认"):
            second.check_available(edition="daily", period="2026-10-23")
        with self.assertRaisesRegex(LedgerError, "未确认"):
            second.reserve(**entry())
        second.check_available(edition="weekly", period="2026-W43")
        self.assertEqual(second.pending_ids(today=date(2026, 10, 23)), {"xueqiu:123"})
        self.assertEqual(second.history, EMPTY)

    def test_successful_send_moves_reservation_into_history(self):
        ledger = open_ledger(EMPTY)
        ledger.reserve(**entry())
        ledger.complete(state(entry()))
        restored = open_ledger(EMPTY)
        self.assertEqual(restored.history, state(entry()))
        self.assertEqual(restored.pending_ids(today=date(2026, 10, 23)), set())
        with self.assertRaisesRegex(LedgerError, "已记录"):
            restored.reserve(**entry())

    def test_example_and_daily_same_day_have_independent_durable_reservations(self):
        ledger = open_ledger(EMPTY)
        example = entry(edition="example", ids=["xueqiu:234"])
        daily = entry()
        ledger.reserve(**example)
        ledger.complete(state(example))
        ledger.reserve(**daily)
        ledger.complete(state(example, daily))
        restored = open_ledger(EMPTY)
        self.assertEqual(restored.history, state(example, daily))
        self.assertEqual(restored.pending_ids(today=date(2026, 10, 23)), set())
        for repeated in (example, daily):
            with self.subTest(edition=repeated["edition"]), self.assertRaisesRegex(LedgerError, "已记录"):
                restored.reserve(**repeated)

    def test_concurrent_reservation_uses_compare_and_swap_and_never_overwrites(self):
        first = open_ledger(EMPTY)
        second = open_ledger(EMPTY)
        first.reserve(**entry())
        before = self.api.content
        with self.assertRaisesRegex(LedgerError, "并发"):
            second.reserve(**entry(edition="weekly", period="2026-W43"))
        self.assertEqual(self.api.content, before)
        self.assertEqual(second.pending_ids(today=date(2026, 10, 23)), set())

    def test_explicit_rejection_can_release_but_does_not_mark_delivered(self):
        ledger = open_ledger(EMPTY)
        ledger.reserve(**entry())
        ledger.cancel()
        ledger.cancel()
        restored = open_ledger(EMPTY)
        restored.check_available(edition="daily", period="2026-10-23")
        self.assertEqual(restored.history, EMPTY)
        self.assertEqual(restored.pending_ids(today=date(2026, 10, 23)), set())

    def test_completion_cannot_fabricate_another_item_or_erase_existing_history(self):
        original = entry("2026-10-22", ids=["x:101"])
        ledger = open_ledger(state(original))
        ledger.reserve(**entry())
        before = self.api.content
        for invalid in [EMPTY, state(entry(ids=["x:999"])), state(entry())]:
            with self.subTest(invalid=invalid), self.assertRaises(LedgerError):
                ledger.complete(invalid)
            self.assertEqual(self.api.content, before)
        ledger.complete(state(original, entry()))

    def test_complete_without_reservation_does_not_write(self):
        ledger = open_ledger(EMPTY)
        calls = len(self.api.calls)
        with self.assertRaises(LedgerError):
            ledger.complete(state(entry()))
        self.assertEqual(len(self.api.calls), calls)

    def test_pending_lookback_is_bounded_and_excludes_future_records(self):
        ledger = open_ledger(EMPTY)
        for day, number in [("2026-07-25", 1), ("2026-07-26", 2), ("2026-10-24", 3)]:
            ledger.reserve(**entry(day, ids=["x:" + str(number)]))
            ledger = open_ledger(EMPTY)
        self.assertEqual(ledger.pending_ids(today=date(2026, 10, 23), history_days=90), {"x:2"})

    def test_existing_branch_with_missing_ledger_fails_closed(self):
        self.api.ref, self.api.sha = True, "a" * 40
        with self.assertRaisesRegex(LedgerError, "服务不可用"):
            open_ledger(EMPTY)
        self.assertTrue(all(req.method == "GET" for req, _ in self.api.calls))

    def test_auth_server_errors_and_transport_errors_never_create_empty_history(self):
        for status in [401, 403, 429, 500, 502, 503, 504]:
            self.api.raise_status = status
            with self.subTest(status=status), self.assertRaises(LedgerError) as caught:
                open_ledger(EMPTY)
            self.assertNotIn(ENV["GH_TOKEN"], str(caught.exception))
            self.assertNotIn("secret error", str(caught.exception))
        self.api.raise_status = None
        with patch.object(self.api, "open", side_effect=error.URLError("Bearer test-secret-never-log")), \
             self.assertRaisesRegex(LedgerError, "连接失败") as caught:
            open_ledger(EMPTY)
        self.assertNotIn(ENV["GH_TOKEN"], str(caught.exception))

    def test_corrupt_duplicate_or_invalid_records_are_not_reset(self):
        open_ledger(EMPTY)
        cases = [b"broken", b'{"version":1,"version":1,"history":{},"pending":[]}',
                 json.dumps({"version": 1, "history": EMPTY, "pending": [entry(), entry()]}).encode(),
                 json.dumps({"version": 1, "history": state(entry()), "pending": [entry()]}).encode()]
        for raw in cases:
            self.api.content = raw
            before = len(self.api.calls)
            with self.subTest(raw=raw), self.assertRaises(LedgerError):
                open_ledger(EMPTY)
            self.assertTrue(all(req.method == "GET" for req, _ in self.api.calls[before:]))
            self.assertEqual(self.api.content, raw)

    def test_malformed_git_results_do_not_create_branch(self):
        with patch.object(self.api, "open", side_effect=[
            error.HTTPError("https://api.github.com", 404, "not found", {}, None),
            Response({"sha": "bad-sha"}),
        ]), self.assertRaises(LedgerError):
            open_ledger(EMPTY)
        self.assertFalse(self.api.ref)

    def test_unconfirmed_update_response_never_marks_local_reservation_sent(self):
        ledger = open_ledger(EMPTY)
        original_open = self.api.open
        def confusing(req, timeout):
            response = original_open(req, timeout)
            return Response({"content": {"sha": "bad"}}) if req.method == "PUT" else response
        with patch.object(self.api, "open", side_effect=confusing), self.assertRaisesRegex(LedgerError, "未确认"):
            ledger.reserve(**entry())
        # A crash after the server accepted the update remains blocked after reload.
        with self.assertRaisesRegex(LedgerError, "未确认"):
            open_ledger(EMPTY).check_available(edition="daily", period="2026-10-23")

    def test_invalid_config_and_record_are_rejected_without_network(self):
        for repo in ["", "../repo", "fixture/repo?token=bad", "fixture/repo/extra"]:
            with self.subTest(repo=repo), self.assertRaises(LedgerError):
                Ledger(repo, "test-key")
        ledger = open_ledger(EMPTY)
        calls = len(self.api.calls)
        for update in [{"edition": "legacy-x"}, {"period": "2026-10-24"},
                       {"item_ids": ["x:0"]}, {"sent_at": "no-date"}]:
            with self.subTest(update=update), self.assertRaises(LedgerError):
                ledger.reserve(**{**entry(), **update})
        self.assertEqual(len(self.api.calls), calls)

    def test_redirects_are_refused(self):
        self.assertIsNone(_NoRedirects().redirect_request(None, None, 302, "move", {}, "https://other.test"))


if __name__ == "__main__":
    unittest.main()
