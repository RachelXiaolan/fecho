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


if __name__ == "__main__":
    unittest.main(verbosity=2)
