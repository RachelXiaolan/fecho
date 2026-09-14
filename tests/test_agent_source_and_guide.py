"""两件事：

1. 云端版随手记的来源 agent。真遇到过：Codex 记的一条显示成 unknown-agent。
   Vercel 上跑着多个实例，agent 连上来（initialize）时报的名字只在那个实例的内存里；
   下一个请求落到别的实例就不知道是谁了。现在连上时记进库，别的实例按会话号查回来。
2. 接入与验收指南放进 Fecho 网站：不用登录就能看，面板、登录页、onboard 页都有入口。
"""
import _env  # noqa: F401  必须在 import fecho 之前
import os
import tempfile
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from fecho import db  # noqa: E402
from test_cloud_auth import WebCase, A, B  # noqa: E402

PRESETS = Path(__file__).resolve().parents[1] / "fecho" / "presets"


class TestSourceAgentAcrossInstances(WebCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from fecho import web
        # 第二个实例：同一个库，不同的进程内存
        self.other = TestClient(web.build_app())
        with db.cursor() as c:
            c.execute("DELETE FROM mcp_sessions")

    def auth(self, who, sid=None):
        h = {"Authorization": "Bearer " + self.tok[who]}
        if sid:
            h["Mcp-Session-Id"] = sid
        return h

    def initialize(self, who, name):
        r = self.client.post("/mcp", headers=self.auth(who), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": name}}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.headers["Mcp-Session-Id"]

    def log_on_other_instance(self, who, sid, content):
        r = self.other.post("/mcp", headers=self.auth(who, sid), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "log_progress", "arguments": {"content": content, "freeform": True}}})
        self.assertEqual(r.status_code, 200, r.text)
        with db.cursor() as c:
            return dict(c.execute("SELECT author, source_agent FROM updates WHERE content_md=?",
                                  (content,)).fetchone())

    def test_the_agent_name_survives_landing_on_another_instance(self):
        sid = self.initialize(A, "codex_rmcp_client")
        row = self.log_on_other_instance(A, sid, "Codex 在另一个实例上记的一条")
        self.assertEqual(row, {"author": A, "source_agent": "codex_rmcp_client"})

    def test_someone_elses_session_id_does_not_lend_their_agent_name(self):
        """B 拿着 A 的会话号：记到 B 自己名下，也借不走 A 的 agent 名字。"""
        sid = self.initialize(A, "claude-code")
        row = self.log_on_other_instance(B, sid, "B 冒用 A 的会话号")
        self.assertEqual(row["author"], B)
        self.assertEqual(row["source_agent"], "unknown-agent")


class TestLocalLogIsWrittenOnce(unittest.TestCase):
    def test_scheduled_run_does_not_print_what_it_already_wrote(self):
        """定时任务把屏幕输出也导进同一个日志文件：再 print 一遍每行会出现两次。"""
        home = Path(tempfile.mkdtemp(prefix="fecho-local-log-"))
        os.environ["FECHO_LOCAL_HOME"] = str(home)
        try:
            spec = importlib.util.spec_from_file_location("fecho_local_log", PRESETS / "local" / "fecho_local.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            os.environ.pop("FECHO_LOCAL_HOME", None)
        with mock.patch.object(mod.sys.stdout, "isatty", return_value=False), \
             mock.patch("builtins.print") as printed:
            mod.log("开始扫 2030-01-10")
        printed.assert_not_called()
        self.assertEqual(mod.LOG.read_text(encoding="utf-8").count("开始扫 2030-01-10"), 1)


class TestGuideInTheApp(WebCase):
    def test_guide_is_readable_without_signing_in(self):
        r = self.client.get("/guide")
        self.assertEqual(r.status_code, 200)
        for needle in ("接入与验收", "验收清单", "fecho_local.py status", "/install"):
            self.assertIn(needle, r.text)

    def test_every_entry_point_links_to_it(self):
        for page in ("dashboard.html", "login.html", "onboard.html"):
            self.assertIn('href="/guide"', (PRESETS / page).read_text(encoding="utf-8"), page)
        self.assertIn("/guide", (PRESETS / "install.md").read_text(encoding="utf-8"))

    def test_guide_has_no_external_dependencies(self):
        import re
        html = (PRESETS / "guide.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'<(script|link)[^>]+(src|href)="https?:', html))

    def test_markdown_copy_matches_the_page(self):
        """仓库里的 ONBOARDING.md 是同一份指南的文字版：安装那句话和验收项数要对得上。"""
        md = (Path(__file__).resolve().parents[1] / "ONBOARDING.md").read_text(encoding="utf-8")
        html = (PRESETS / "guide.html").read_text(encoding="utf-8")
        prompt = "读 https://fecho.techmob.net/install ，按里面的步骤帮我接入 Fecho。"
        self.assertIn(prompt, md)
        self.assertIn(prompt, html)
        self.assertEqual(md.count("- [ ] "), html.count('type="checkbox"'))


if __name__ == "__main__":
    unittest.main()
