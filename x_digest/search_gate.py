"""Limit relay-backed web searches across both daily source branches.

These long-running tool requests share one channel. Waiting for a slot does
not consume an individual HTTP request's timeout. Ordinary chat editing uses
its own transport and does not acquire this gate.
"""

from contextlib import contextmanager
from threading import BoundedSemaphore


_SEARCH_SLOTS = BoundedSemaphore(1)


@contextmanager
def search_request_slot():
    """Hold the shared single-request slot through opening and reading HTTP."""
    _SEARCH_SLOTS.acquire()
    try:
        yield
    finally:
        _SEARCH_SLOTS.release()
