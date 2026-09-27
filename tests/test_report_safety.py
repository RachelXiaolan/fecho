"""生成日报期间人改了日报：人的版本不能被覆盖（Tony 的 PR #4）。

以前 digest.generate 在开头读一次现有日报，几分钟后模型写完直接覆盖。这几分钟里
人手改了日报，改动就被挤进历史，当前显示的变回自动稿。现在写回前核对一遍：
和开始时不一样就说明有人改过，人的版本留着，自动稿存进历史。

agent 调 end_of_day 以前排的是 regenerate（覆盖人改过的日报），现在默认走 refresh。
"""
import unittest
from unittest import mock

import _env  # noqa: F401  必须在 import fecho 之前
from test_cloud_auth import A, CloudCase
from test_core import D, reset

from fecho import config, db, digest, jobs, llm, mcp_server, store


def model(on_first_call=None):
    """写日报的第一次调用时顺手做点别的事（模拟人在这几分钟里改日报）。"""
    state = {"n": 0}

    def chat(messages, **kw):
        state["n"] += 1
        if state["n"] == 1 and on_first_call:
            on_first_call()
        user = messages[-1]["content"]
        return "[1] done | 写完了" if "今天推进了" in user else "今天做完了一件事。" * 20
    return chat


class TestEditDuringGeneration(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)
        with db.cursor() as c:
            c.execute("DELETE FROM report_history")   # 公共的 reset 不清历史
        store.record_progress("t", "AI-2541 做了一件事", date=D)
        digest._persist("t", D, "daily", "# 旧的自动稿", "old", "llm", None, 1)
        digest._persist("t", D, "voice", "旧口播", "old", "llm", None, 1)

    def generate(self, chat, **kw):
        with mock.patch.object(config, "llm_configured", return_value=True), \
                mock.patch.object(llm, "chat", side_effect=chat), \
                mock.patch.object(digest, "verify_assignments", return_value={"changed": [], "error": None}), \
                mock.patch.object(digest, "task_aliases", return_value={}):
            return digest.generate("t", D, force=True, **kw)

    def history(self, kind="daily"):
        return [h for h in db.report_history("t", D) if h["kind"] == kind]

    def test_an_edit_made_while_the_model_writes_is_kept(self):
        r = self.generate(model(lambda: digest.save_human_edit("t", D, "# 人手改的版本\n")),
                          keep_human=False)          # 连「重新生成」都不能覆盖生成期间的改动
        current = db.get_report("t", D, "daily")
        self.assertEqual(r["status"], "kept-human")
        self.assertEqual(current["generator"], "human")
        self.assertIn("人手改的版本", current["content_md"])
        self.assertTrue(any(h["generator"] == "llm" and "写完了" in h["content_md"] for h in self.history()),
                        "自动稿存进历史，人可以对照")
        self.assertEqual(db.get_report("t", D, "voice")["content_md"], "旧口播",
                         "日报没写进去，口播稿也不能单独换掉")

    def test_without_interference_the_report_is_replaced_as_before(self):
        r = self.generate(model())
        self.assertEqual(r["status"], "generated")
        self.assertIn("写完了", db.get_report("t", D, "daily")["content_md"])
        self.assertEqual([h["content_md"] for h in self.history()], ["# 旧的自动稿"], "旧版照旧进历史")

    def test_first_report_of_the_day_is_written(self):
        with db.cursor() as c:
            c.execute("DELETE FROM reports")
        self.assertEqual(self.generate(model())["status"], "generated")
        self.assertIsNotNone(db.get_report("t", D, "voice"))


class TestAgentEndOfDay(CloudCase):
    def setUp(self):
        super().setUp()
        with db.cursor() as c:
            c.execute("DELETE FROM jobs")
        store.record_progress(A, "做了一件事", date=D, freeform=True)

    def call(self, **args):
        ctx = mcp_server.MCPContext(session_id="s", author=A)
        return mcp_server._cloud_call("end_of_day", dict(args, date=D), A, ctx)

    def kinds(self):
        with db.cursor() as c:
            return [r["kind"] for r in c.execute("SELECT kind FROM jobs WHERE author=?", (A,)).fetchall()]

    def test_default_keeps_hand_edits(self):
        """以前排的是 regenerate，agent 一句「收工」就把人改过的日报覆盖了。"""
        self.call()
        self.assertEqual(self.kinds(), ["refresh"])

    def test_force_regenerates(self):
        self.call(force=True)
        self.assertEqual(self.kinds(), ["regenerate"])

    def test_agent_request_runs_now_not_after_the_upload_window(self):
        self.call()
        with db.cursor() as c:
            row = c.execute("SELECT run_after, created_at FROM jobs WHERE author=?", (A,)).fetchone()
        self.assertLessEqual(row["run_after"], jobs._now_iso(), "人让 agent 收工，就该马上出")
