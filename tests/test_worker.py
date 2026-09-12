"""lu2 上的后台程序：到点排队、领任务、失败补跑。"""
import _env  # noqa: F401  必须在 import fecho 之前
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

from fecho import accounts, db, jobs, worker  # noqa: E402
from test_cloud_auth import A, B, CloudCase  # noqa: E402


def bj(y, mo, d, h, mi):
    """北京时间 → 带时区的 datetime。"""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc) - timedelta(hours=8)


class WorkerCase(CloudCase):
    def setUp(self):
        super().setUp()
        for who, name in ((A, "Alice"), (B, "Bob")):
            accounts.upsert_user(who, name)
        with db.cursor() as c:
            c.execute("DELETE FROM jobs")


class TestEnqueue(WorkerCase):
    def test_queues_each_person_at_their_own_time(self):
        accounts.set_daily_time(A, "18:00")
        accounts.set_daily_time(B, "21:00")
        worker.enqueue_due(now=bj(2030, 1, 10, 18, 5))
        with db.cursor() as c:
            rows = [r["author"] for r in c.execute("SELECT author FROM jobs").fetchall()]
        self.assertEqual(rows, [A], "还没到 Bob 的点")
        worker.enqueue_due(now=bj(2030, 1, 10, 21, 0))
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"], 2)

    def test_one_per_person_per_day(self):
        """轮询每 30 秒跑一次，不能每次都排一个。"""
        for _ in range(3):
            worker.enqueue_due(now=bj(2030, 1, 10, 21, 5))
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM jobs WHERE author=?",
                                       (A,)).fetchone()["n"], 1)


class TestClaim(WorkerCase):
    def test_two_workers_never_take_the_same_job(self):
        job = jobs.enqueue(A, "daily", "2030-01-10")
        first = worker.claim()
        second = worker.claim()
        self.assertEqual([j["job_id"] for j in first], [job["job_id"]])
        self.assertEqual(second, [], "已经被领走的不能再被领一次")

    def test_scheduled_retry_is_not_picked_up_early(self):
        later = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
        jobs.enqueue(A, "regenerate", "2030-01-10", run_after=later)
        self.assertEqual(worker.claim(), [])


class TestFinish(WorkerCase):
    def _job(self):
        jobs.enqueue(A, "daily", "2030-01-10")
        return worker.claim()[0]

    def test_success_is_final(self):
        worker.finish(self._job())
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()["status"], "succeeded")

    def test_first_failure_is_retried_in_half_an_hour(self):
        worker.finish(self._job(), error="网关超时")
        with db.cursor() as c:
            row = dict(c.execute("SELECT * FROM jobs").fetchone())
        self.assertEqual(row["status"], "queued", "还有补跑机会，重新排队")
        self.assertGreater(row["run_after"], datetime.now(timezone.utc).isoformat())
        self.assertEqual(worker.claim(), [], "半小时内不会被领走")

    def test_second_failure_gives_up_and_records_why(self):
        """失败原因要留下来——「该扫了吗」那条通道会把它带给本机弹通知。"""
        job = self._job()
        worker.finish(job, error="网关超时")
        with db.cursor() as c:      # 把补跑时间提前，模拟半小时后
            c.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00+00:00'")
        worker.finish(worker.claim()[0], error="还是超时")
        with db.cursor() as c:
            row = dict(c.execute("SELECT * FROM jobs").fetchone())
        self.assertEqual(row["status"], "failed")
        self.assertIn("还是超时", row["error"])

    def test_failed_job_becomes_a_notice_for_the_laptop(self):
        from fecho import cloudscan
        jobs.enqueue(A, "daily", "2030-01-10")
        job = worker.claim()[0]
        worker.finish(job, error="网关超时")
        worker.finish(dict(job, attempts=9), error="网关超时")
        with db.cursor() as c:      # 让通知看得到这一天
            c.execute("UPDATE jobs SET date=?", (accounts._iso(accounts._now())[:10],))
        self.assertTrue(any("网关超时" in n for n in cloudscan.notices(A)))


class TestRunJob(WorkerCase):
    def test_generates_the_report_and_syncs_issues_first(self):
        from fecho import service
        jobs.enqueue(A, "daily", "2030-01-10")
        job = worker.claim()[0]
        with mock.patch.object(worker, "sync_issues") as sync, \
             mock.patch.object(service, "end_of_day",
                               return_value={"status": "generated", "task_count": 2,
                                             "update_count": 5}) as eod:
            worker.run_job(job)
        sync.assert_called_once_with(A)
        self.assertEqual(eod.call_args.kwargs["author"], A)
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()["status"], "succeeded")

    def test_mobius_failure_does_not_block_the_report(self):
        """同步不上顶多少点归属依据，不该让整份日报出不来。"""
        from fecho import mobius_login, service
        jobs.enqueue(A, "daily", "2030-01-10")
        job = worker.claim()[0]
        with mock.patch.object(mobius_login, "access_token", side_effect=RuntimeError("授权过期")), \
             mock.patch.object(service, "end_of_day",
                               return_value={"status": "generated"}) as eod:
            worker.run_job(job)
        eod.assert_called_once()
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()["status"], "succeeded")

    def test_one_persons_failure_does_not_stop_the_others(self):
        from fecho import service
        jobs.enqueue(A, "daily", "2030-01-10")
        jobs.enqueue(B, "daily", "2030-01-10")
        batch = worker.claim(limit=2)

        def flaky(date, author=None, force=False):
            if author == A:
                raise RuntimeError("Alice 这边炸了")
            return {"status": "generated"}

        with mock.patch.object(worker, "sync_issues"), \
             mock.patch.object(service, "end_of_day", side_effect=flaky):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(worker.run_job, batch))
        with db.cursor() as c:
            rows = {r["author"]: r["status"] for r in
                    c.execute("SELECT author, status FROM jobs").fetchall()}
        self.assertEqual(rows[B], "succeeded")
        self.assertEqual(rows[A], "queued", "失败的那个进了补跑队列")


class TestGuards(unittest.TestCase):
    def test_refuses_to_run_outside_cloud(self):
        """这个程序只在云端版有意义，跑错环境要立刻停，不能默默连本机库。"""
        with self.assertRaises(SystemExit):
            worker.serve()


if __name__ == "__main__":
    unittest.main()
