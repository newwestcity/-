"""X and Snowball share a transport slot without gating ordinary editing."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
import unittest
from unittest.mock import patch

from x_digest import discovery, editorial, provider, search_gate
from x_digest.app import load_settings


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
X_URL = "https://x.com/example/status/2023456789012345678"
SNOW_URL = "https://xueqiu.com/123456/456789"
SEARCH_RESPONSE = json.dumps({"status": "completed", "output": [{
    "type": "web_search_call", "status": "completed", "action": {"sources": [
        {"url": X_URL, "title": "AI practice"}, {"url": SNOW_URL, "title": "价值投资方法"},
    ]}}]}).encode()
CHAT_RESPONSE = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[]}'}}]}).encode()


class ObservedSemaphore:
    """Report a second acquisition attempt even while it waits for the first."""
    def __init__(self):
        self.semaphore = BoundedSemaphore(1)
        self.mutex = Lock()
        self.attempts = 0
        self.second_attempt = Event()

    def acquire(self):
        with self.mutex:
            self.attempts += 1
            if self.attempts == 2:
                self.second_attempt.set()
        return self.semaphore.acquire()

    def release(self):
        self.semaphore.release()


class Transport:
    def __init__(self, *, fail_first=False, hold_first=True):
        self.mutex = Lock()
        self.first_read = Event()
        self.release_first = Event()
        self.active = 0
        self.maximum_active = 0
        self.calls = []
        self.fail_first = fail_first
        self.hold_first = hold_first

    def open(self, req, timeout):
        with self.mutex:
            self.calls.append((req.full_url, timeout))
            number = len(self.calls)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        return Response(self, number, chat=req.full_url.endswith("/chat/completions"))


class Response:
    def __init__(self, transport, number, *, chat):
        self.transport, self.number, self.chat = transport, number, chat

    def __enter__(self):
        return self

    def __exit__(self, *args):
        with self.transport.mutex:
            self.transport.active -= 1
        return False

    def read(self, size):
        if self.number == 1 and not self.chat:
            self.transport.first_read.set()
            if self.transport.hold_first and not self.transport.release_first.wait(5):
                raise AssertionError("The test did not release its first request")
            if self.transport.fail_first:
                raise OSError("synthetic transport failure")
        return (CHAT_RESPONSE if self.chat else SEARCH_RESPONSE)[:size]


class SearchGateTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(Path(__file__).resolve().parents[1] / "settings.json")
        self.settings.update(categories=["AI"], max_tool_calls=1)
        self.semaphore = ObservedSemaphore()
        patcher = patch("x_digest.search_gate._SEARCH_SLOTS", self.semaphore)
        patcher.start()
        self.addCleanup(patcher.stop)

    def x_request(self):
        return provider.search(base_url="https://api.example.test/v1", api_key="test-only", model="test-model",
                               settings=self.settings, seen_ids=set(), now=NOW, timeout=17, retrieval_only=True)

    def snow_request(self):
        return discovery._one("https://api.example.test/v1/responses", "test-only", {"model": "test-model"})

    def run_both_sources(self, *, fail_first=False):
        transport = Transport(fail_first=fail_first)
        with patch("urllib.request.build_opener", return_value=transport), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.x_request)
            try:
                self.assertTrue(transport.first_read.wait(5))
                second = pool.submit(self.snow_request)
                self.assertTrue(self.semaphore.second_attempt.wait(5))
                # Snowball reached the shared gate, yet its HTTP open/timeout
                # has not started while X still holds the response body.
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.maximum_active, 1)
            finally:
                transport.release_first.set()
            if fail_first:
                with self.assertRaises(provider.ProviderError):
                    first.result(timeout=5)
            else:
                self.assertEqual(first.result(timeout=5).cited_urls, [X_URL])
            self.assertEqual(second.result(timeout=5)[1], "completed")
        self.assertEqual(transport.maximum_active, 1)
        self.assertEqual(transport.active, 0)
        self.assertEqual([timeout for _, timeout in transport.calls], [17, discovery.REQUEST_TIMEOUT])

    def test_different_source_threads_hold_one_slot_through_response_read(self):
        self.run_both_sources()

    def test_transport_failure_releases_slot_for_the_other_source(self):
        self.run_both_sources(fail_first=True)

    def test_an_editor_chat_can_finish_while_search_slot_is_occupied(self):
        transport = Transport(hold_first=False)
        with patch("urllib.request.build_opener", return_value=transport), ThreadPoolExecutor(max_workers=1) as pool:
            with search_gate.search_request_slot():
                future = pool.submit(editorial._chat, endpoint="https://api.example.test/v1/chat/completions",
                                     api_key="test-only", model="test-model", prompt="test", target=1)
                self.assertEqual(future.result(timeout=5), [])
        self.assertEqual(self.semaphore.attempts, 1)
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
