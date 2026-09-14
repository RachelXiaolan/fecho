"""第 5 步：网页上的设置、admin，以及日报里任务的短名由模型来起。"""
import _env  # noqa: F401  必须在 import fecho 之前
import unittest
from pathlib import Path
from unittest import mock

from fecho import accounts, config, db, digest, llm, store  # noqa: E402
from test_cloud_auth import ADMIN, CloudCase, WebCase, A, B  # noqa: E402

PRESETS = Path(__file__).resolve().parents[1] / "fecho" / "presets"
DAY = "2030-01-10"


class AliasCase(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(A, "Alice")
        with db.cursor() as c:
            c.execute("DELETE FROM task_aliases")
        store.record_progress(A, "把本机采集脚本写完了", date=DAY, freeform=True)
        self.tasks = db.day_tasks(A, DAY)
        self.persona = digest.persona_for(A)
        self.persona["task_aliases"] = {}
        self.key = digest.task_key(self.tasks[0])

    def aliases(self, reply=None, error=None, warnings=None):
        chat = mock.MagicMock(return_value=reply, side_effect=error)
        with mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch.object(llm, "chat", chat):
            got = digest.task_aliases(A, self.tasks, self.persona, warnings)
        return got, chat


class TestAutoAliases(AliasCase):
    def test_named_once_then_reused(self):
        """起一次就存下来。名字不会今天一个明天一个，也不会被某天对话里的候选名带偏。"""
        first, chat = self.aliases("[1] Fecho")
        self.assertEqual(first[self.key], "Fecho")
        self.assertEqual(chat.call_count, 1)
        again, chat = self.aliases("[1] 别的名字")
        self.assertEqual(again[self.key], "Fecho")
        chat.assert_not_called()

    def test_something_that_is_not_a_name_falls_back_to_the_title(self):
        got, _ = self.aliases("[1] done | 这是一句总结不是名字")
        self.assertNotIn(self.key, got)
        self.assertEqual(digest.short_name(self.tasks[0], self.persona, got), self.tasks[0]["title"])

    def test_model_failure_does_not_block_the_report(self):
        warnings = []
        got, _ = self.aliases(error=llm.LLMError("网关超时"), warnings=warnings)
        self.assertEqual(got, {})
        self.assertTrue(any("短名" in w for w in warnings))

    def test_no_model_call_when_llm_is_not_configured(self):
        with mock.patch.object(config, "llm_configured", return_value=False), \
             mock.patch.object(llm, "chat") as chat:
            digest.task_aliases(A, self.tasks, self.persona)
        chat.assert_not_called()

    def test_hand_written_alias_wins(self):
        task = {"task_id": "t1", "issue_key": "AI-1", "title": "很长的标题"}
        persona = dict(self.persona, task_aliases={"AI-1": "手写的名"})
        self.assertEqual(digest.short_name(task, persona, {"AI-1": "模型起的"}), "手写的名")
        self.assertEqual(digest.short_name(task, dict(persona, task_aliases={}), {"AI-1": "模型起的"}),
                         "模型起的")

    def test_alias_rules(self):
        self.assertEqual(digest._valid_alias("「Fecho」"), "Fecho")
        self.assertEqual(digest._valid_alias("**周一带练**"), "周一带练")
        for bad in ("", "AI-2541", "https://x.com", "a | b", "这是一个远远超过十二个字的名字啊",
                    "两行\n名字"):
            self.assertIsNone(digest._valid_alias(bad), bad)

    def test_prompt_prefers_names_already_in_the_issue_title(self):
        task = {"task_id": "t1", "issue_key": "AI-2541", "title": "agent 原生 time-off：Fecho",
                "updates": [{"content_md": "今天讨论要不要改名叫 fmjot"}]}
        system, user = digest._alias_prompt([task])
        self.assertIn("标题里已经有的", system["content"])
        self.assertIn("候选名", system["content"])
        self.assertIn("AI-2541 agent 原生 time-off：Fecho", user["content"])

    def test_report_uses_the_alias_and_it_does_not_mark_the_report_dirty(self):
        def fake_chat(messages, **kw):
            system = messages[0]["content"]
            if "起短名" in system:
                return "[1] 本机采集"
            if "口播稿" in system:
                return "今天主要把本机采集脚本写完了。" * 20
            return "[1] done | 写完了本机采集脚本\n- done | 脚本写完了"

        with mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch.object(llm, "chat", side_effect=fake_chat):
            r = digest.generate(A, DAY, force=True)
        daily = db.get_report(A, DAY, "daily")
        self.assertIn("**本机采集**", daily["content_md"])
        current = digest.fingerprint(db.day_tasks(A, DAY), digest.persona_for(A),
                                     __import__("fecho.pto", fromlist=["status"]).status(A, DAY))
        self.assertEqual(daily["fingerprint"], current, "起了短名不该让日报被标成需要重新生成")
        self.assertEqual(r["status"], "generated")


class TestAdminFlowOverHttp(WebCase):
    def test_request_approve_then_view_someone_else_read_only(self):
        r = self.post("/api/admin/request", A)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get("/api/me", A).json()["admin_request"]["status"], "pending")

        pending = self.get("/api/admin/requests", ADMIN).json()["items"]
        self.assertEqual([p["author"] for p in pending], [A])
        r = self.post("/api/admin/requests/%s" % pending[0]["request_id"], ADMIN, {"approve": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.get("/api/me", A).json()["is_admin"])

        r = self.get("/api/admin/dashboard", A, params={"author": B, "date": self.today})
        self.assertEqual(r.status_code, 200, r.text)
        contents = [u["content_md"] for t in r.json()["today"]["tasks"] for u in t["updates"]]
        self.assertIn("Bob 的秘密进展", contents)

    def test_members_cannot_see_the_admin_side(self):
        for path in ("/api/admin/requests", "/api/admin/users"):
            self.assertEqual(self.get(path, A).status_code, 403, path)
        self.assertEqual(self.get("/api/admin/dashboard", A, params={"author": B}).status_code, 403)

    def test_settings_round_trip(self):
        with db.cursor() as c:     # 公共的 reset 不清这两张表，别的测试留下的开关会串进来
            c.execute("DELETE FROM agent_connections")
            c.execute("DELETE FROM work_folders")
        self.assertEqual(self.post("/api/settings", A, {"daily_time": "19:30"}).status_code, 200)
        self.assertEqual(self.get("/api/me", A).json()["daily_time"], "19:30")
        self.post("/api/agents/codex", A, {"scan_enabled": True})
        agents = {a["agent"]: a for a in self.get("/api/agents", A).json()["items"]}
        self.assertTrue(agents["codex"]["scan_enabled"])
        self.assertFalse(agents["claude-code"]["scan_enabled"])
        self.post("/api/folders", A, {"selected": ["/Users/alice/work"]})
        self.assertEqual([f["path"] for f in self.get("/api/folders", A).json()["items"] if f["selected"]],
                         ["/Users/alice/work"])
        self.assertEqual(self.get("/api/folders", B).json()["items"], [], "别人的白名单看不到")


class TestPagesContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dash = (PRESETS / "dashboard.html").read_text(encoding="utf-8")
        cls.onboard = (PRESETS / "onboard.html").read_text(encoding="utf-8")

    def test_dashboard_has_settings_and_admin(self):
        for needle in ('data-view="settings"', 'data-view="admin"', 'data-page="settings"',
                       'data-page="admin"', "/api/agents", "/api/folders", "/api/settings",
                       "/api/admin/requests", "/api/admin/users", "/api/admin/dashboard"):
            self.assertIn(needle, self.dash)

    def test_admin_entry_is_hidden_until_we_know_you_are_admin(self):
        self.assertIn('data-view="admin" data-admin-only hidden', self.dash)
        self.assertIn('data-view="settings" data-cloud-only hidden', self.dash)
        self.assertIn("[hidden]{display:none!important}", self.dash,
                      ".nav button 的 display:flex 会盖掉 hidden，必须强制隐藏")

    def test_viewing_someone_else_is_read_only(self):
        body = self.dash[self.dash.index("async function mutate"):]
        body = body[:body.index("document.addEventListener")]
        self.assertIn("state.viewAs", body)
        regen = self.dash[self.dash.index("async function regenerate"):]
        self.assertIn("state.viewAs", regen[:200])

    def test_onboarding_lets_you_pick_work_folders(self):
        """安装说明让用户回这一页勾工作文件夹，这一页就必须真的能勾。"""
        self.assertIn("/api/folders", self.onboard)
        self.assertIn("保存勾选", self.onboard)


if __name__ == "__main__":
    unittest.main()
