"""把任务归到 issue 上：issue 还没有对应的任务、甚至不在同步缓存里，也要能归。

真实问题：Bug Hunter 的一堆进展其实都属于 AI-2660，但「合并到…」只列已有任务。
一个 issue 要先有进展被归过去才会变成任务——没归过去就选不到，选不到就归不过去。
"""
import unittest
from pathlib import Path
from unittest import mock

import _env  # noqa: F401  必须在 import fecho 之前
from test_core import D, reset

from fecho import config, db, mobius, service, store


class TestAttachToIssue(unittest.TestCase):
    def setUp(self):
        reset()

    def test_issue_without_a_task_yet_gets_one_and_takes_the_progress(self):
        free = store.record_progress("t", "Bug Hunter 框架部署到 Cloudflare", date=D, freeform=True)
        with db.cursor() as c:
            self.assertIsNone(c.execute(
                "SELECT 1 FROM tasks WHERE author='t' AND issue_key='AI-2460'").fetchone(),
                "前提：AI-2460 在缓存里，但还没有任务")

        result = service.attach_to_issue(free["task"]["task_id"], "ai-2460", author="t")

        target = result["task"]
        self.assertEqual(target["issue_key"], "AI-2460")
        self.assertEqual(result["moved_updates"], 1)
        self.assertEqual(db.get_task(free["task"]["task_id"])["status"], "merged")
        update = db.list_updates(task_id=target["task_id"])[0]
        self.assertEqual(update["assignment_locked"], 1, "人点名的归属要锁住，模型不能再改")

    def test_issue_outside_the_sync_cache_is_looked_up_on_mobius(self):
        """别人名下的、没指派的、刚建的 issue 都不在缓存里，人点名了就现查。"""
        free = store.record_progress("t", "扒 Orca 早期 commit", date=D, freeform=True)
        fetched = {"identifier": "AI-2677", "title": "把 Orca 推上和 GitHub 早期 commit 拉出来看看"}

        def fake_fetch(author, key, token=None):
            with db.cursor() as c:
                c.execute("INSERT INTO mobius_issues (issue_key,author,title,state,url,updated_at,"
                          "synced_at,raw) VALUES (?,?,?,?,?,?,?,?)",
                          (key, author, fetched["title"], "Todo", "", "", store.now_iso(), "{}"))
            return fetched

        with mock.patch.object(mobius, "fetch_issue", side_effect=fake_fetch) as f:
            result = service.attach_to_issue(free["task"]["task_id"], "AI-2677", author="t")
        f.assert_called_once()
        self.assertEqual(result["task"]["issue_key"], "AI-2677")
        self.assertEqual(result["task"]["title"], fetched["title"])

    def test_issue_mobius_does_not_know_is_refused(self):
        free = store.record_progress("t", "随手一条", date=D, freeform=True)
        with mock.patch.object(mobius, "fetch_issue",
                               side_effect=mobius.MobiusError("Mobius 没有找到 issue AI-9999")):
            with self.assertRaises(ValueError):
                service.attach_to_issue(free["task"]["task_id"], "AI-9999", author="t")
        self.assertEqual(db.get_task(free["task"]["task_id"])["status"], "open", "查不到就原样不动")

    def test_bad_key_and_attaching_to_itself_are_refused(self):
        rec = store.record_progress("t", "AI-2541 进展", date=D)
        with self.assertRaises(ValueError):
            service.attach_to_issue(rec["task"]["task_id"], "随便写的", author="t")
        with self.assertRaises(ValueError):
            service.attach_to_issue(rec["task"]["task_id"], "AI-2541", author="t")

    def test_local_install_uses_the_configured_token(self):
        """云端每人用自己的授权；本机版不传，走配置里那一个。"""
        with mock.patch.object(config, "CLOUD", False):
            self.assertIsNone(mobius.token_for("t"))

    def test_cloud_passes_the_persons_token_when_looking_up_one_issue(self):
        """以前按编号查单个 issue 没带授权，云端一律失败再被吞掉。"""
        from fecho import mobius_login
        with mock.patch.object(config, "CLOUD", True), \
                mock.patch.object(mobius_login, "access_token", return_value="tok-t"), \
                mock.patch.object(mobius, "_rpc", return_value={"content": [{"text":
                    '{"issue": {"identifier": "AI-2677", "title": "x"}}'}]}) as rpc:
            mobius.ensure_issue("t", "AI-2677")
        self.assertEqual(rpc.call_args.kwargs.get("token"), "tok-t")


class TestAttachEndpoint(unittest.TestCase):
    def setUp(self):
        reset()
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("没装 fastapi[server] 额外依赖")
        from fecho import web
        self._orig_token, web.TOKEN = web.TOKEN, "t3st-token"
        self.addCleanup(lambda: setattr(web, "TOKEN", self._orig_token))
        self._orig_author, config.AUTHOR = config.AUTHOR, "t"
        self.addCleanup(lambda: setattr(config, "AUTHOR", self._orig_author))
        self.c = TestClient(web.build_app(), headers={"Authorization": "Bearer t3st-token"})

    def test_attach_endpoint_moves_the_task_and_returns_the_dashboard(self):
        free = store.record_progress("t", "自由任务进展", date=D, freeform=True)
        r = self.c.post("/api/tasks/%s/attach" % free["task"]["task_id"],
                        json={"issue_key": "AI-2460", "date": D})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["issue_key"], "AI-2460")
        self.assertIn("dashboard", r.json())

    def test_review_page_can_name_an_issue_outside_the_cache(self):
        rec = store.record_progress("t", "一条进展", date=D, freeform=True)
        with mock.patch.object(mobius, "fetch_issue") as f:
            def fake(author, key, token=None):
                with db.cursor() as c:
                    c.execute("INSERT INTO mobius_issues (issue_key,author,title,state,url,"
                              "updated_at,synced_at,raw) VALUES (?,?,?,?,?,?,?,?)",
                              (key, author, "新 issue", "Todo", "", "", store.now_iso(), "{}"))
                return {"identifier": key, "title": "新 issue"}
            f.side_effect = fake
            r = self.c.post("/api/reassign", json={"update_id": rec["update_id"],
                                                   "issue_key": "AI-2700"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.list_updates(task_id=store.task_for_issue("t", "AI-2700")["task_id"])[0]
                         ["update_id"], rec["update_id"])


class TestMergePicker(unittest.TestCase):
    html = (Path(__file__).resolve().parents[1] / "fecho" / "presets" / "dashboard.html").read_text(
        encoding="utf-8")

    def test_picker_offers_synced_issues_and_accepts_a_typed_key(self):
        self.assertIn('<datalist id="merge-targets">', self.html)
        self.assertIn("state.data.issues.items||[]", self.html,
                      "候选里要有同步来的 issue，不只是已有任务")
        self.assertIn("/attach`", self.html, "选中 issue 走 attach，能现建任务")
