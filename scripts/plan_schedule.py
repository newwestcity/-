"""Plan bounded catch-up runs using the current Asia/Shanghai calendar.

All scheduled invocations use the same plan: daily reading, plus the GitHub
edition on Sunday. A delayed Sunday trigger never sends the previous ISO week's
edition on Monday. The delivery ledger decides whether each planned edition has
already been sent; this planner does not read or modify that ledger.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def editions_for_event(event_name: str, edition: str = "daily", *, now: datetime,
                       enabled: bool = True, weekly_enabled: bool = True) -> list[str]:
    """Return the editions eligible now, without network access or side effects.

    Scheduled catch-up is allowed from 08:00 inclusive to 20:00 exclusive.
    Manual dispatch ignores schedule switches and the scheduled time window.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("Schedule time must include a timezone")
    if type(enabled) is not bool or type(weekly_enabled) is not bool:
        raise ValueError("Schedule switches must be booleans")
    if edition not in {"daily", "weekly"}:
        raise ValueError("Unsupported reading edition")
    if event_name == "workflow_dispatch":
        return [edition]
    if event_name != "schedule" or not enabled:
        return []
    local_now = now.astimezone(SHANGHAI)
    if not 8 <= local_now.hour < 20:
        return []
    result = ["daily"]
    if local_now.weekday() == 6 and weekly_enabled:
        result.append("weekly")
    return result


def _switch(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value not in {"", "true", "false"}:
        raise ValueError("Invalid schedule switch")
    return value != "false"


def main(*, now: datetime | None = None) -> int:
    try:
        enabled = _switch("DAILY_OVERRIDE")
        weekly_enabled = _switch("WEEKLY_OVERRIDE")
        editions = editions_for_event(
            os.environ.get("GITHUB_EVENT_NAME", ""),
            os.environ.get("EDITION", "daily") or "daily",
            now=now if now is not None else datetime.now(SHANGHAI),
            enabled=enabled, weekly_enabled=weekly_enabled,
        )
        outputs = {
            "matrix": json.dumps({"edition": editions}, separators=(",", ":")),
            "has_editions": str(bool(editions)).lower(),
            "daily_enabled": str(enabled).lower(),
            "weekly_enabled": str(enabled and weekly_enabled).lower(),
        }
        destination = os.environ.get("GITHUB_OUTPUT")
        if destination:
            with Path(destination).open("a", encoding="utf-8") as stream:
                for name, value in outputs.items():
                    stream.write(name + "=" + value + "\n")
    except (ValueError, TypeError, OSError):
        # Environment values can contain credentials or workflow commands.
        print("::error::invalid_schedule_configuration", file=sys.stderr)
        return 2
    print("::notice title=Reading schedules::editions=" + (",".join(editions) or "none")
          + ";window=08:00-20:00 Asia/Shanghai;weekly_day=Sunday")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
