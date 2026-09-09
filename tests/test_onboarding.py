"""Onboarding and unattended automation contracts."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


TMP = tempfile.mkdtemp(prefix="fecho-onboarding-test-")
os.environ.setdefault("FECHO_HOME", os.path.join(TMP, "home"))
os.environ.setdefault("FECHO_DB", os.path.join(TMP, "fecho.db"))
os.environ.setdefault("FECHO_LOGS_DIR", os.path.join(TMP, "logs"))
os.environ.setdefault("FECHO_LLM_BASE_URL", "")
os.environ.setdefault("FECHO_LLM_API_KEY", "")
os.environ.setdefault("FECHO_MOBIUS_URL", "")
os.environ.setdefault("FECHO_MOBIUS_TOKEN", "")

from fecho import store  # noqa: E402


class TestBeijingClock(unittest.TestCase):
    def test_utc_evening_is_next_beijing_calendar_day(self):
        from fecho import clock

        instant = datetime(2030, 1, 1, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(clock.today(instant), "2030-01-02")
        self.assertEqual(clock.from_iso("2030-01-01T16:30:00Z").hour, 0)

    def test_store_today_uses_product_clock(self):
        with mock.patch("fecho.clock.today", return_value="2030-01-02"):
            self.assertEqual(store.today(), "2030-01-02")

    def test_daily_time_must_be_before_2200_beijing(self):
        from fecho import clock

        for value in ("00:00", "09:05", "21:00", "21:59"):
            self.assertEqual(clock.validate_daily_time(value), value)
        for value in ("22:00", "23:59", "9:00", "21:60", "tomorrow"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "22:00"):
                    clock.validate_daily_time(value)


class TestDailyAutomation(unittest.TestCase):
    def setUp(self):
        from fecho import automation

        self.automation = automation
        self.state = {"enabled": True, "daily_time": "21:00"}

    def test_tick_runs_once_during_beijing_grace_window(self):
        instant = datetime(2030, 1, 1, 13, 7, tzinfo=timezone.utc)  # 21:07 Beijing
        saved = []
        runner = mock.Mock(return_value={"ok": True, "date": "2030-01-01"})

        first = self.automation.tick(
            instant=instant, state=dict(self.state), runner=runner,
            save=lambda value: saved.append(value))
        second = self.automation.tick(
            instant=instant, state=saved[-1], runner=runner,
            save=lambda value: saved.append(value))

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "already-run")
        runner.assert_called_once_with("2030-01-01")

    def test_tick_does_not_run_before_or_long_after_selected_time(self):
        runner = mock.Mock()
        for instant in (
            datetime(2030, 1, 1, 12, 59, tzinfo=timezone.utc),
            datetime(2030, 1, 1, 13, 10, tzinfo=timezone.utc),
        ):
            result = self.automation.tick(
                instant=instant, state=dict(self.state), runner=runner,
                save=lambda value: None)
            self.assertEqual(result["status"], "not-due")
        runner.assert_not_called()

    def test_failed_tick_is_visible_and_does_not_mark_day_complete(self):
        instant = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
        saved = []
        result = self.automation.tick(
            instant=instant, state=dict(self.state),
            runner=lambda date: {"ok": False, "date": date, "error": "scan failed"},
            save=lambda value: saved.append(value))

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("last_run_date", saved[-1])
        self.assertEqual(saved[-1]["last_result"]["error"], "scan failed")

    def test_daily_pipeline_continues_when_mobius_sync_fails(self):
        scan = mock.Mock(return_value={"ok": True, "recorded": 2})
        digest = mock.Mock(return_value={"status": "generated", "update_count": 2})

        result = self.automation.run_daily(
            "2030-01-01",
            sync=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
            scan_func=scan,
            digest_func=digest,
        )

        self.assertTrue(result["ok"])
        self.assertIn("offline", result["warnings"][0])
        scan.assert_called_once_with(days=2)
        digest.assert_called_once_with("2030-01-01", force=False)

    def test_daily_pipeline_stops_when_scan_fails(self):
        digest = mock.Mock()
        result = self.automation.run_daily(
            "2030-01-01", sync=lambda: {"count": 1},
            scan_func=lambda **kw: {"ok": False, "error": "bad transcript"},
            digest_func=digest,
        )
        self.assertFalse(result["ok"])
        self.assertIn("bad transcript", result["error"])
        digest.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
