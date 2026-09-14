"""这天一条记录都没有时点「重新生成」：直接说生成不出，不排队。

真遇到过：9/14 没有任何记录，点了好几次「重新生成」，每次都提示「已排队，几分钟后刷新
就能看到」，后台每次都跑出一个空结果——人以为坏了。
"""
import _env  # noqa: F401  必须在 import fecho 之前
import json

from fecho import db  # noqa: E402
from test_cloud_auth import WebCase, A  # noqa: E402

EMPTY_DAY = "2030-01-01"


class TestRegenerateWithNothingRecorded(WebCase):
    def jobs_for(self, date):
        with db.cursor() as c:
            return c.execute("SELECT COUNT(*) n FROM jobs WHERE author=? AND date=?",
                             (A, date)).fetchone()["n"]

    def test_web_says_so_and_does_not_queue(self):
        r = self.post("/api/regenerate", A, {"date": EMPTY_DAY})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["empty"])
        self.assertFalse(body["queued"])
        self.assertEqual(self.jobs_for(EMPTY_DAY), 0)

    def test_a_day_with_records_still_queues(self):
        r = self.post("/api/regenerate", A, {"date": self.today})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["queued"])
        self.assertFalse(r.json().get("empty"))
        self.assertEqual(self.jobs_for(self.today), 1)

    def test_agent_asking_for_a_report_gets_the_same_answer(self):
        r = self.post("/mcp", A, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "end_of_day", "arguments": {"date": EMPTY_DAY}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("没有任何记录", json.dumps(r.json(), ensure_ascii=False))
        self.assertEqual(self.jobs_for(EMPTY_DAY), 0)

    def test_dashboard_shows_a_distinct_message(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parents[1] / "fecho" / "presets" /
                "dashboard.html").read_text(encoding="utf-8")
        regen = html[html.index("async function regenerate"):]
        regen = regen[:regen.index("$('#regenerate').addEventListener")]
        self.assertIn("result.empty", regen)
        self.assertIn("toast_nothing_to_generate", regen)


if __name__ == "__main__":
    import unittest
    unittest.main()
