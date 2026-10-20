"""Persist successful deliveries without silently losing deduplication history."""

from __future__ import annotations

from datetime import date as Date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_POST_ID = re.compile(r"[1-9][0-9]{0,24}\Z")


def _parse_date(value: str) -> Date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("Delivery date must use YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid delivery date") from exc


def _validate_sent_at(value: str) -> None:
    if not isinstance(value, str) or not value or "T" not in value:
        raise ValueError("sent_at must be an ISO8601 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid sent_at timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("sent_at must include a timezone")


def _validate_post_ids(value: list[str]) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not _POST_ID.fullmatch(item) for item in value
    ):
        raise ValueError("post_ids must be a list of positive numeric strings")
    if len(value) != len(set(value)):
        raise ValueError("A delivery contains duplicate post IDs")
    return list(value)


def _validated_state(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
        raise ValueError("Unsupported or missing delivery-state version")
    if not isinstance(state.get("deliveries"), list):
        raise ValueError("Delivery state must contain a deliveries list")
    deliveries = []
    dates = set()
    for entry in state["deliveries"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid delivery record")
        day = entry.get("date")
        _parse_date(day)
        _validate_sent_at(entry.get("sent_at"))
        ids = _validate_post_ids(entry.get("post_ids"))
        if day in dates:
            raise ValueError("Delivery state contains a duplicate date")
        dates.add(day)
        deliveries.append({"date": day, "sent_at": entry["sent_at"], "post_ids": ids})
    return {"version": 1, "deliveries": deliveries}


def _validate_history_days(history_days: int) -> None:
    if type(history_days) is not int or history_days < 1:
        raise ValueError("history_days must be a positive integer")


def load_state(path: Path) -> dict:
    """Return empty history only when the file is absent, never when corrupt."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {"version": 1, "deliveries": []}
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Delivery state is corrupt; preserve it before repairing") from exc
    return _validated_state(state)


def seen_post_ids(state: dict, *, today: Date, history_days: int = 45) -> set[str]:
    """Get IDs from today and the previous history_days - 1 calendar days."""
    _validate_history_days(history_days)
    if type(today) is not Date:
        raise ValueError("today must be a datetime.date")
    clean = _validated_state(state)
    cutoff = today - timedelta(days=history_days - 1)
    return {
        post_id
        for delivery in clean["deliveries"]
        if cutoff <= _parse_date(delivery["date"]) <= today
        for post_id in delivery["post_ids"]
    }


def already_sent(state: dict, date: str) -> bool:
    """Check whether a successful delivery is recorded for this local date."""
    _parse_date(date)
    return any(entry["date"] == date for entry in _validated_state(state)["deliveries"])


def record_delivery(
    path: Path,
    state: dict,
    *,
    date: str,
    post_ids: list[str],
    sent_at: str,
    history_days: int = 45,
) -> dict:
    """Atomically record a completed SMTP delivery and return its new state.

    Call only after SMTP succeeds. A repeated date is an error. The input state
    and existing file remain untouched if validation or replacement fails.
    This function does not provide a lock: serialize concurrent runs externally.
    """
    clean = _validated_state(state)
    day = _parse_date(date)
    _validate_history_days(history_days)
    _validate_sent_at(sent_at)
    ids = _validate_post_ids(post_ids)
    if any(entry["date"] == date for entry in clean["deliveries"]):
        raise ValueError("A delivery is already recorded for this date")
    cutoff = day - timedelta(days=history_days - 1)
    # Preserve future-dated records if a historical run is recorded deliberately.
    deliveries = [entry for entry in clean["deliveries"] if _parse_date(entry["date"]) >= cutoff]
    deliveries.append({"date": date, "sent_at": sent_at, "post_ids": ids})
    deliveries.sort(key=lambda entry: entry["date"])
    updated = {"version": 1, "deliveries": deliveries}

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(updated, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return updated
