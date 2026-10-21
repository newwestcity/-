"""Bounded delivery history for daily reading mail and weekly project mail.

Only call ``record_delivery`` after SMTP accepts a message. Callers must hold a
lock around reading, sending, and recording; atomic replacement is not a lock.
Legacy X history participates in item deduplication, never edition scheduling.
"""

from __future__ import annotations

from datetime import date as Date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_WEEK = re.compile(r"([0-9]{4})-W([0-9]{2})\Z")
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,24}\Z")
_GITHUB_ID = re.compile(
    r"github:([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,100})\Z"
)
_MAX_BYTES = 2 * 1024 * 1024
_MAX_DELIVERIES = 1000
_MAX_ITEMS = 100
_MAX_HISTORY_DAYS = 180
_MISSING = object()

Edition = Literal["daily", "weekly"]


def _parse_date(value: str) -> Date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("Delivery date must use YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid delivery date") from exc


def _validate_period(edition: str, period: str) -> None:
    if edition in {"daily", "legacy-x"}:
        _parse_date(period)
    elif edition == "weekly":
        match = _WEEK.fullmatch(period) if isinstance(period, str) else None
        if not match:
            raise ValueError("Weekly period must use YYYY-Www")
        try:
            Date.fromisocalendar(int(match[1]), int(match[2]), 1)
        except ValueError as exc:
            raise ValueError("Invalid ISO week") from exc
    else:
        raise ValueError("Unsupported delivery edition")


def _validate_public_period(edition: str, period: str) -> None:
    if not isinstance(edition, str) or edition not in {"daily", "weekly"}:
        raise ValueError("edition must be daily or weekly")
    _validate_period(edition, period)


def _validate_sent_at(value: str) -> None:
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        raise ValueError("sent_at must be an ISO8601 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid sent_at timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("sent_at must include a timezone")


def _validate_item_ids(value: list[str], *, legacy: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise ValueError("item_ids must be a list of at most 100 IDs")
    identities = set()
    for item in value:
        if not isinstance(item, str) or len(item) > 150:
            raise ValueError("Invalid item ID")
        source, _, identifier = item.partition(":")
        if source in {"x", "xueqiu"} and _NUMERIC_ID.fullmatch(identifier):
            pass
        elif not legacy and source == "github":
            match = _GITHUB_ID.fullmatch(item)
            if not match or match[2] in {".", ".."}:
                raise ValueError("Invalid GitHub item ID")
        else:
            raise ValueError("IDs must use x:<id>, xueqiu:<id>, or github:owner/repo")
        if legacy and source != "x":
            raise ValueError("Legacy history may contain only X IDs")
        identity = item.casefold() if source == "github" else item
        if identity in identities:
            raise ValueError("A delivery contains duplicate item IDs")
        identities.add(identity)
    return list(value)


def _validated_state(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 2:
        raise ValueError("Unsupported or missing reading-state version")
    entries = state.get("deliveries")
    if not isinstance(entries, list) or len(entries) > _MAX_DELIVERIES:
        raise ValueError("Delivery state must contain at most 1000 deliveries")
    deliveries = []
    periods = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid delivery record")
        edition, period = entry.get("edition"), entry.get("period")
        # Reject unhashable JSON values with ValueError, not a raw TypeError.
        if not isinstance(edition, str):
            raise ValueError("Unsupported delivery edition")
        _validate_period(edition, period)
        day = _parse_date(entry.get("date"))
        if edition in {"daily", "legacy-x"} and period != day.isoformat():
            raise ValueError("Daily or legacy period must match delivery date")
        if edition == "weekly" and period != f"{day.isocalendar().year:04d}-W{day.isocalendar().week:02d}":
            raise ValueError("Weekly period must match the delivery date's ISO week")
        _validate_sent_at(entry.get("sent_at"))
        ids = _validate_item_ids(entry.get("item_ids"), legacy=edition == "legacy-x")
        identity = (edition, period)
        if identity in periods:
            raise ValueError("Delivery state contains a duplicate edition and period")
        periods.add(identity)
        deliveries.append({"edition": edition, "period": period, "date": entry["date"],
                           "sent_at": entry["sent_at"], "item_ids": ids})
    return {"version": 2, "deliveries": deliveries}


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Delivery state contains duplicate JSON keys")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return _MISSING
    if len(raw) > _MAX_BYTES:
        raise ValueError("Delivery state exceeds the 2 MiB limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("Delivery state is corrupt; preserve it before repairing") from exc


def _legacy_deliveries(state: object) -> list[dict]:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
        raise ValueError("Unsupported or missing legacy delivery-state version")
    entries = state.get("deliveries")
    if not isinstance(entries, list) or len(entries) > _MAX_DELIVERIES:
        raise ValueError("Legacy state must contain at most 1000 deliveries")
    converted = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid legacy delivery record")
        ids = entry.get("post_ids")
        if not isinstance(ids, list) or len(ids) > _MAX_ITEMS or any(
            not isinstance(item, str) or not _NUMERIC_ID.fullmatch(item) for item in ids
        ):
            raise ValueError("Invalid legacy post IDs")
        converted.append({"edition": "legacy-x", "period": entry.get("date"),
                          "date": entry.get("date"), "sent_at": entry.get("sent_at"),
                          "item_ids": [f"x:{item}" for item in ids]})
    return _validated_state({"version": 2, "deliveries": converted})["deliveries"]


def load_state(path: Path, legacy_path: Path | None = None) -> dict:
    """Read v2 and merge validated v1 X history in memory, without writing.

    Only absent files are empty. Invalid current or legacy history fails closed.
    Repeated imports preserve one legacy record per date and union its X IDs.
    """
    raw = _read_json(path)
    clean = _validated_state(raw) if raw is not _MISSING else {"version": 2, "deliveries": []}
    if legacy_path is None:
        return clean
    raw_legacy = _read_json(legacy_path)
    if raw_legacy is _MISSING:
        return clean
    existing = {(entry["edition"], entry["period"]): entry for entry in clean["deliveries"]}
    for entry in _legacy_deliveries(raw_legacy):
        key = (entry["edition"], entry["period"])
        if key in existing:
            existing[key]["item_ids"] = list(dict.fromkeys(existing[key]["item_ids"] + entry["item_ids"]))
        else:
            clean["deliveries"].append(entry)
            existing[key] = entry
    return _validated_state(clean)


def _validate_history_days(history_days: int) -> None:
    if type(history_days) is not int or not 1 <= history_days <= _MAX_HISTORY_DAYS:
        raise ValueError("history_days must be an integer between 1 and 180")


def _cutoff(day: Date, history_days: int) -> Date:
    return day - timedelta(days=min(history_days - 1, day.toordinal() - 1))


def seen_ids(state: dict, *, today: Date, history_days: int = 90) -> set[str]:
    """Return source-qualified IDs within the inclusive lookback through today."""
    _validate_history_days(history_days)
    if type(today) is not Date:
        raise ValueError("today must be a datetime.date")
    clean = _validated_state(state)
    cutoff = _cutoff(today, history_days)
    return {item for entry in clean["deliveries"]
            if cutoff <= _parse_date(entry["date"]) <= today for item in entry["item_ids"]}


def already_sent(state: dict, *, edition: Edition, period: str) -> bool:
    """Check this edition's period independently of all other editions."""
    _validate_public_period(edition, period)
    return any(entry["edition"] == edition and entry["period"] == period
               for entry in _validated_state(state)["deliveries"])


def record_delivery(
    path: Path,
    state: dict,
    *,
    edition: Edition,
    period: str,
    date: str,
    item_ids: list[str],
    sent_at: str,
    history_days: int = 180,
) -> dict:
    """Atomically record successful SMTP delivery; caller serializes all runs.

    Reject duplicate (edition, period) before pruning. Preserve future records
    for historical runs. Input state and existing file survive validation or
    replacement failures unchanged.
    """
    clean = _validated_state(state)
    _validate_public_period(edition, period)
    _validate_history_days(history_days)
    day = _parse_date(date)
    if any(entry["edition"] == edition and entry["period"] == period for entry in clean["deliveries"]):
        raise ValueError("A delivery is already recorded for this edition and period")
    cutoff = _cutoff(day, history_days)
    entries = [entry for entry in clean["deliveries"] if _parse_date(entry["date"]) >= cutoff]
    entries.append({"edition": edition, "period": period, "date": date,
                    "item_ids": item_ids, "sent_at": sent_at})
    entries.sort(key=lambda entry: (entry["date"], entry["edition"], entry["period"]))
    updated = _validated_state({"version": 2, "deliveries": entries})
    encoded = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise ValueError("Delivery state exceeds the 2 MiB limit")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return updated
