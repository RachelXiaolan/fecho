"""出日报的进度条：点了生成之后，网页能看到做到哪一步、排在第几、后台是不是还活着。

以前云端点「重新生成」只回一句「已排队」，之后就没下文了：卡在排队、卡在模型超时、
后台根本没在跑，看起来一模一样。9/23 那天等了很久，最后出来的是兜底稿。
"""
import unittest
from pathlib import Path
from unittest import mock

import _env  # noqa: F401  必须在 import fecho 之前
from test_cloud_auth import A, WebCase
from test_core import D, mock_llm, reset

from fecho import db, digest, jobs, store, worker


class TestProgressRecording(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)
        with db.cursor() as c:
            c.execute("DELETE FROM jobs")
            c.execute("DELETE FROM app_settings")

    def test_generate_reports_each_stage_in_order(self):
        store.record_progress("t", "AI-2541 做了一件事", date=D)
        seen = []
        with mock_llm("[1] done | 做完了\n"), \
                mock.patch.object(digest, "verify_assignments", return_value={"changed": [], "error": None}):
            digest.generate("t", D, force=True, progress=lambda stage, detail=None: seen.append(stage))
        self.assertEqual(seen, ["verify", "aliases", "daily", "voice"])

    def test_worker_writes_progress_and_a_heartbeat(self):
        job = jobs.enqueue("t", "regenerate", D)
        stages = []
        real = jobs.set_progress

        def spy(job_id, stage, detail=None):
            stages.append(stage)
            real(job_id, stage, detail)

        with mock.patch.object(jobs, "set_progress", side_effect=spy), \
                mock.patch.object(worker, "sync_issues"), \
                mock.patch("fecho.service.end_of_day",
                           side_effect=lambda *a, progress=None, **k: (progress("daily"), {"status": "generated"})[1]):
            worker.run_job(dict(job, attempts=0))
        self.assertEqual(stages, ["sync", "daily"])
        st = jobs.status("t", D)
        self.assertEqual(st["job"]["progress"]["stage"], "daily")
        self.assertIsNotNone(st["worker_seen_at"], "写进度顺便算一次心跳")

    def test_progress_write_failure_never_breaks_the_report(self):
        report = jobs.reporter("no-such-job")
        with mock.patch.object(jobs, "set_progress", side_effect=RuntimeError("db down")):
            report("daily")          # 不抛就对了

    def test_status_counts_the_queue_ahead_and_ignores_other_kinds(self):
        other = jobs.enqueue("someone", "regenerate", D)
        mine = jobs.enqueue("t", "regenerate", D)
        jobs.enqueue("t", "voice", D)                # 重出口播稿不算出日报
        with db.cursor() as c:                       # 保证先后
            c.execute("UPDATE jobs SET created_at='2000-01-01T00:00:00+00:00', run_after='2000-01-01T00:00:00+00:00'"
                      " WHERE job_id=?", (other["job_id"],))
        st = jobs.status("t", D)
        self.assertEqual(st["job"]["job_id"], mine["job_id"])
        self.assertEqual(st["queue_ahead"], 1)
        self.assertEqual(st["stages"], ["sync", "verify", "aliases", "daily", "voice"])

    def test_worker_tick_is_a_heartbeat_even_with_nothing_to_do(self):
        with mock.patch.object(worker, "enqueue_due", return_value=[]), \
                mock.patch.object(worker, "claim", return_value=[]):
            worker.tick(None)
        self.assertIsNotNone(jobs.status("t", D)["worker_seen_at"])


class TestStatusEndpoint(WebCase):
    def test_status_includes_the_report_author_so_a_fallback_is_visible(self):
        """任务「成功」了也可能是兜底稿，网页要看得出来。"""
        job = jobs.enqueue(A, "regenerate", self.today)
        with db.cursor() as c:
            c.execute("UPDATE jobs SET status='succeeded', started_at=created_at, finished_at=created_at"
                      " WHERE job_id=?", (job["job_id"],))
        digest._persist(A, self.today, "daily", "# 兜底", "fp", "fallback", None, 1,
                        ["日报 LLM 失败（LLM 请求失败: timed out），已输出兜底稿"])
        r = self.get("/api/jobs/status?date=" + self.today, A)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["job"]["status"], "succeeded")
        self.assertEqual(body["report"]["generator"], "fallback")
        self.assertTrue(any("timed out" in w for w in body["report"]["warnings"]))

    def test_other_peoples_jobs_are_not_visible(self):
        jobs.enqueue("bob@feedmob.com", "regenerate", self.today)
        self.assertIsNone(self.get("/api/jobs/status?date=" + self.today, A).json()["job"])


class TestProgressStripContract(unittest.TestCase):
    html = (Path(__file__).resolve().parents[1] / "fecho" / "presets" / "dashboard.html").read_text(encoding="utf-8")

    def test_page_polls_while_a_job_is_active_and_flags_fallback_and_a_dead_worker(self):
        self.assertIn("/api/jobs/status?date=", self.html)
        self.assertIn("gen_fallback", self.html)
        self.assertIn("gen_worker_down", self.html)
        self.assertIn("checkGen(true)", self.html, "点了生成要马上开始盯进度")
