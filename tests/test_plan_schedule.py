from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import plan_schedule


SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_time(day=25, hour=8, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=SHANGHAI)


class SchedulePlanTests(unittest.TestCase):
    def test_sunday_plans_both_editions_for_each_catch_up_invocation(self):
        for hour in (8, 9, 10, 14, 19):
            with self.subTest(hour=hour):
                self.assertEqual(plan_schedule.editions_for_event("schedule", now=local_time(hour=hour)),
                                 ["daily", "weekly"])

    def test_window_boundaries(self):
        for now, expected in ((local_time(hour=7, minute=59, second=59), []),
                              (local_time(), ["daily", "weekly"]),
                              (local_time(hour=19, minute=59, second=59), ["daily", "weekly"]),
                              (local_time(hour=20), [])):
            with self.subTest(now=now):
                self.assertEqual(plan_schedule.editions_for_event("schedule", now=now), expected)

    def test_weekday_only_plans_daily_and_monday_does_not_claim_previous_week(self):
        for day in (23, 24, 26):
            self.assertEqual(plan_schedule.editions_for_event("schedule", now=local_time(day=day)), ["daily"])

    def test_current_shanghai_time_controls_delayed_triggers(self):
        # UTC Saturday evening is Sunday in Shanghai; Sunday evening becomes Monday.
        self.assertEqual(plan_schedule.editions_for_event(
            "schedule", now=datetime(2026, 10, 25, 0, tzinfo=timezone.utc)), ["daily", "weekly"])
        self.assertEqual(plan_schedule.editions_for_event(
            "schedule", now=datetime(2026, 10, 26, 0, tzinfo=timezone.utc)), ["daily"])
        self.assertEqual(plan_schedule.editions_for_event(
            "schedule", now=datetime(2026, 10, 25, 16, tzinfo=timezone.utc)), [])

    def test_master_and_weekly_switches(self):
        self.assertEqual(plan_schedule.editions_for_event("schedule", now=local_time(), enabled=False), [])
        self.assertEqual(plan_schedule.editions_for_event(
            "schedule", now=local_time(), weekly_enabled=False), ["daily"])

    def test_manual_dispatch_ignores_schedule_switches_and_window(self):
        for edition in ("daily", "weekly"):
            self.assertEqual(plan_schedule.editions_for_event(
                "workflow_dispatch", edition, now=local_time(hour=3),
                enabled=False, weekly_enabled=False), [edition])

    def test_push_and_other_events_never_plan_delivery(self):
        for event in ("push", "pull_request", "", "untrusted\n::error::value"):
            self.assertEqual(plan_schedule.editions_for_event(event, now=local_time()), [])

    def test_naive_dates_invalid_editions_and_nonboolean_switches_are_rejected(self):
        invalid = ({"now": datetime(2026, 10, 25, 8)}, {"now": None},
                   {"edition": "unknown"}, {"enabled": "false"}, {"weekly_enabled": 1})
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                plan_schedule.editions_for_event("schedule", **{"now": local_time(), **override})


class ScheduleMainTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output_path = Path(self.directory.name) / "output"
        os.environ.update(GITHUB_EVENT_NAME="schedule", GITHUB_OUTPUT=str(self.output_path))
        self.stdout, self.stderr = StringIO(), StringIO()

    def run_main(self, now=None):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return plan_schedule.main(now=now or local_time())

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output_path.read_text().splitlines())

    def test_outputs_matrix_and_flags_with_fixed_safe_notice(self):
        self.assertEqual(self.run_main(), 0)
        outputs = self.outputs()
        self.assertEqual(json.loads(outputs["matrix"]), {"edition": ["daily", "weekly"]})
        self.assertEqual(outputs["has_editions"], "true")
        self.assertEqual(outputs["daily_enabled"], "true")
        self.assertEqual(outputs["weekly_enabled"], "true")
        self.assertIn("editions=daily,weekly", self.stdout.getvalue())
        self.assertEqual(self.stderr.getvalue(), "")

    def test_disabled_master_produces_empty_matrix_and_both_disabled_flags(self):
        os.environ["DAILY_OVERRIDE"] = " FALSE "
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(json.loads(self.outputs()["matrix"]), {"edition": []})
        self.assertEqual(self.outputs()["has_editions"], "false")
        self.assertEqual(self.outputs()["weekly_enabled"], "false")

    def test_weekly_switch_off_preserves_daily(self):
        os.environ["WEEKLY_OVERRIDE"] = "false"
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(json.loads(self.outputs()["matrix"]), {"edition": ["daily"]})
        self.assertEqual(self.outputs()["weekly_enabled"], "false")

    def test_manual_edition_is_preserved_when_schedule_disabled(self):
        os.environ.update(GITHUB_EVENT_NAME="workflow_dispatch", EDITION="weekly", DAILY_OVERRIDE="false")
        self.assertEqual(self.run_main(local_time(hour=1)), 0)
        self.assertEqual(json.loads(self.outputs()["matrix"]), {"edition": ["weekly"]})

    def test_outputs_append_and_missing_output_file_setting_is_supported(self):
        self.output_path.write_text("existing=value\n")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.outputs()["existing"], "value")
        del os.environ["GITHUB_OUTPUT"]
        self.assertEqual(self.run_main(), 0)

    def test_invalid_switches_and_edition_do_not_echo_environment_values(self):
        sensitive = "private-key\n::error::untrusted"
        for name in ("DAILY_OVERRIDE", "WEEKLY_OVERRIDE", "EDITION"):
            with self.subTest(name=name), patch.dict(os.environ, {name: sensitive}):
                self.assertEqual(self.run_main(), 2)
                self.assertFalse(self.output_path.exists())
                self.assertNotIn("private-key", self.stdout.getvalue() + self.stderr.getvalue())
                self.assertNotIn("untrusted", self.stdout.getvalue() + self.stderr.getvalue())

    def test_output_write_failure_is_reported_without_the_destination_path(self):
        os.environ["GITHUB_OUTPUT"] = str(Path(self.directory.name) / "private-path" / "output")
        self.assertEqual(self.run_main(), 2)
        self.assertIn("invalid_schedule_configuration", self.stderr.getvalue())
        self.assertNotIn("private-path", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
