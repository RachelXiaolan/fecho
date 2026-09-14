"""同事电脑上跑的本机采集脚本（presets/local/fecho_local.py）。

它不能 import fecho（同事电脑上没装），所以提示词和解析规则是抄了一份过去的。
这里最要紧的是盯住三件事：
1. 抄过去的那份和服务器上 scan.py 不走样
2. 隐私边界：白名单外的对话、工具输出、宿主样板，一个字都不能交给 agent
3. 每个 agent 只扫自己的对话：Claude 的交给 claude 提炼，Codex 的交给 codex 提炼
"""
import _env  # noqa: F401  必须在 import fecho 之前
import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fecho import scan  # noqa: E402
from test_cloud_auth import WebCase, A, B  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "fecho" / "presets" / "local" / "fecho_local.py"
WORK = "/Users/alice/Documents/work"


def load_script(home):
    os.environ["FECHO_LOCAL_HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location("fecho_local_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.pop("FECHO_LOCAL_HOME", None)


def claude_line(ts, role, cwd, content):
    return json.dumps({"type": role, "timestamp": ts, "cwd": cwd,
                       "message": {"role": role, "content": content}}, ensure_ascii=False)


def codex_lines(session, cwd, ts, text):
    return [
        json.dumps({"type": "session_meta", "timestamp": ts,
                    "payload": {"id": session, "cwd": cwd}}),
        json.dumps({"type": "response_item", "timestamp": ts,
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": text}]}},
                   ensure_ascii=False),
    ]


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fecho-local-"))
        self.mod = load_script(self.tmp / "home")
        self.claude_glob = str(self.tmp / "projects" / "*" / "*.jsonl")
        self.codex_glob = str(self.tmp / "codex" / "*" / "*.jsonl")

    def _write(self, path, lines, mtime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        t = datetime.fromisoformat(mtime).timestamp()
        os.utime(path, (t, t))
        return path

    def write_transcript(self, lines, name="s1.jsonl", mtime="2030-01-10T12:00:00+08:00"):
        return self._write(self.tmp / "projects" / "proj" / name, lines, mtime)

    def write_codex(self, lines, name="r1.jsonl", mtime="2030-01-10T12:00:00+08:00"):
        return self._write(self.tmp / "codex" / "2030" / name, lines, mtime)

    def collect_claude(self, date="2030-01-10", folders=(WORK,)):
        return self.mod.collect(date, list(folders), "claude-code", self.claude_glob)


class TestSameRulesAsServer(ScriptCase):
    """脚本里抄的那份规则一旦和服务器不一致，同一段对话本机和本机版会抽出不同的东西。"""

    def test_prompt_is_identical(self):
        self.assertEqual(self.mod.PROMPT, scan.PROMPT)

    def test_boilerplate_filter_is_identical(self):
        self.assertEqual(self.mod.BOILERPLATE.pattern, scan._BOILERPLATE.pattern)

    def test_chunk_size_is_identical(self):
        self.assertEqual(self.mod.CHUNK_CHARS, scan.CHUNK_CHARS)

    def test_event_key_is_identical(self):
        part = [{"ts": "2030-01-10T01:00:00Z"}, {"ts": "2030-01-10T02:00:00Z"}]
        self.assertEqual(self.mod.event_key("claude-code", "s1", part, 3),
                         scan._event_key("claude-code", "s1", part, 3))

    def test_no_issue_block_matches_server(self):
        self.assertEqual(self.mod.PROMPT.replace("{issues}", self.mod.NO_ISSUES),
                         scan._prompt([]))

    def test_transcript_locations_match_the_server(self):
        from fecho import cloudscan
        for agent, spec in cloudscan.AGENTS.items():
            self.assertEqual(self.mod.TRANSCRIPTS[agent], spec["transcripts"])

    def test_runs_on_the_python_macs_ship_with(self):
        """同事电脑上是 macOS 自带的 Python 3.9。脚本只能用标准库、不能用 3.10+ 的语法。"""
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        allowed = {"argparse", "glob", "hashlib", "json", "os", "plistlib", "re", "shutil",
                   "subprocess", "sys", "time", "urllib.error", "urllib.request", "datetime",
                   "pathlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    self.assertIn(n.name, allowed, "脚本不能依赖 %s" % n.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed, "脚本不能依赖 %s" % node.module)
            self.assertNotIsInstance(node, getattr(ast, "Match", ()), "3.9 没有 match 语句")


class TestPrivacy(ScriptCase):
    def test_only_whitelisted_folders_are_read(self):
        self.write_transcript([
            claude_line("2030-01-10T02:00:00Z", "user", WORK + "/项目A", "把登录做完"),
            claude_line("2030-01-10T02:01:00Z", "user", "/Users/alice/Documents/private", "私事"),
            claude_line("2030-01-10T02:02:00Z", "user", WORK + "-备份", "名字像但不是子目录"),
        ])
        texts = [r["text"] for rows in self.collect_claude().values() for r in rows]
        self.assertEqual(texts, ["把登录做完"])

    def test_tool_output_and_host_boilerplate_are_dropped(self):
        """工具调用和输出是对话里的大头，也最可能带着文件内容、密钥这类东西。"""
        self.write_transcript([
            claude_line("2030-01-10T02:00:00Z", "assistant", WORK, [
                {"type": "text", "text": "改好了"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "cat .env"}},
            ]),
            claude_line("2030-01-10T02:00:05Z", "user", WORK, [
                {"type": "tool_result", "content": "SECRET=abc123"}]),
            claude_line("2030-01-10T02:00:06Z", "user", WORK, "<system-reminder>宿主注入</system-reminder>"),
        ])
        rendered = "".join(self.mod.render(rows) for rows in self.collect_claude().values())
        self.assertIn("改好了", rendered)
        for leaked in ("cat .env", "SECRET", "宿主注入"):
            self.assertNotIn(leaked, rendered)

    def test_days_are_cut_in_beijing_time(self):
        """记录里是 UTC。北京早上 8 点前的对话，UTC 还是前一天——按 UTC 切会把它丢到昨天。"""
        self.write_transcript([
            claude_line("2030-01-09T17:30:00Z", "user", WORK, "北京 1 月 10 日凌晨 1 点半"),
            claude_line("2030-01-10T16:30:00Z", "user", WORK, "北京 1 月 11 日凌晨"),
        ])
        texts = [r["text"] for rows in self.collect_claude().values() for r in rows]
        self.assertEqual(texts, ["北京 1 月 10 日凌晨 1 点半"])

    def test_files_untouched_since_before_that_day_are_not_opened(self):
        path = self.write_transcript(
            [claude_line("2030-01-10T02:00:00Z", "user", WORK, "x")],
            mtime="2030-01-09T12:00:00+08:00")
        with mock.patch.object(self.mod, "normalized_records",
                               side_effect=AssertionError("不该打开 %s" % path)):
            self.assertEqual(self.collect_claude(), {})


class TestParse(ScriptCase):
    def test_parses_lines_and_ignores_issue_column(self):
        raw = "done | AI-1 | 登录做完了\npitfall | - | 试了 A 不行\n随便一句话"
        self.assertEqual(self.mod.parse_entries(raw), [
            {"kind": "done", "content": "登录做完了"},
            {"kind": "pitfall", "content": "试了 A 不行"}])

    def test_none_means_nothing(self):
        self.assertEqual(self.mod.parse_entries("NONE"), [])

    def test_garbage_is_an_error_not_silence(self):
        """格式不对要报错：当成「没进展」的话，这段就永远不会被重试了。"""
        with self.assertRaises(ValueError):
            self.mod.parse_entries("好的，我来总结一下……")


class TestScanDay(ScriptCase):
    def setUp(self):
        super().setUp()
        self.write_transcript([
            claude_line("2030-01-10T02:00:00Z", "user", WORK, "把登录做完"),
            claude_line("2030-01-10T02:05:00Z", "assistant", WORK, "登录做完了，测试通过"),
        ])
        self.cfg = {"url": "http://x", "token": "t", "runners": {"claude-code": "claude"}}
        self.due = {"due": True, "date": "2030-01-10", "folders": [WORK],
                    "agents": [{"agent": "claude-code", "transcripts": self.claude_glob}]}
        self.calls = []

    def fake_api(self, cfg, method, path, body=None, timeout=60):
        self.calls.append((path, body))
        return {"recorded": len((body or {}).get("entries") or [])}

    def test_uploads_entries_then_marks_the_day_done(self):
        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "run_agent", return_value="done | - | 登录做完了"):
            self.assertEqual(self.mod.scan_day(self.cfg, self.due), 0)
        (p1, first), (p2, last) = self.calls
        self.assertEqual(p1, "/api/scan/submit")
        entry = first["entries"][0]
        self.assertEqual((entry["content"], entry["project"], entry["date"], entry["agent"]),
                         ("登录做完了", WORK, "2030-01-10", "claude-code"))
        self.assertTrue(entry["source_event_key"].startswith("scan:"))
        self.assertEqual((last["finished"], last["error"], last["entries"]), (True, None, []))

    def test_each_agent_scans_only_its_own_transcripts(self):
        """Claude 的对话交给 claude 提炼，Codex 的交给 codex 提炼，不交叉。"""
        self.write_codex(codex_lines("cx1", WORK, "2030-01-10T03:00:00Z", "codex 这边写完了报表"))
        self.cfg["runners"]["codex"] = "codex"
        self.due["agents"].append({"agent": "codex", "transcripts": self.codex_glob})
        seen = []

        def fake_agent(cfg, agent, text):
            seen.append((agent, text))
            return "done | - | %s 那边的进展" % agent

        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "run_agent", side_effect=fake_agent):
            self.assertEqual(self.mod.scan_day(self.cfg, self.due), 0)
        by_agent = dict(seen)
        self.assertEqual(set(by_agent), {"claude-code", "codex"})
        self.assertIn("把登录做完", by_agent["claude-code"])
        self.assertNotIn("报表", by_agent["claude-code"])
        self.assertIn("报表", by_agent["codex"])
        self.assertNotIn("登录", by_agent["codex"])
        uploaded = {e["agent"]: e for _, body in self.calls for e in (body or {}).get("entries") or []}
        self.assertEqual(uploaded["codex"]["session_id"], "codex:cx1")

    def test_enabled_agent_that_cannot_be_woken_is_a_failure(self):
        """网页上开着 Codex 的扫描、这台电脑却叫不醒它：必须报失败让服务器重试，
        不能悄悄当成「今天 Codex 没有对话」。"""
        self.due["agents"].append({"agent": "codex", "transcripts": self.codex_glob})
        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "run_agent", return_value="NONE"):
            self.assertEqual(self.mod.scan_day(self.cfg, self.due), 1)
        self.assertIn("codex", self.calls[-1][1]["error"])

    def test_failure_is_reported_so_the_server_retries(self):
        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "run_agent", side_effect=RuntimeError("claude 超时")):
            self.assertEqual(self.mod.scan_day(self.cfg, self.due), 1)
        _, last = self.calls[-1]
        self.assertTrue(last["finished"])
        self.assertIn("claude 超时", last["error"])

    def test_retry_does_not_pay_twice_for_what_already_uploaded(self):
        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "run_agent", return_value="done | - | 登录做完了") as agent:
            self.mod.scan_day(self.cfg, self.due)
            self.mod.scan_day(self.cfg, self.due)
        self.assertEqual(agent.call_count, 1)

    def test_agent_never_sees_other_folders(self):
        self.write_transcript([claude_line("2030-01-10T03:00:00Z", "user",
                                           "/Users/alice/Documents/private", "私事")], name="s2.jsonl")
        seen = []
        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "run_agent",
                               side_effect=lambda cfg, agent, text: seen.append(text) or "NONE"):
            self.mod.scan_day(self.cfg, self.due)
        self.assertTrue(seen)
        self.assertFalse(any("私事" in t for t in seen))


class TestInstall(ScriptCase):
    def setUp(self):
        super().setUp()
        self.api_calls = []
        self.args = argparse.Namespace(url="http://x/", token="fecho_t", agents=None)

    def fake_api(self, cfg, method, path, body=None, timeout=60):
        self.api_calls.append((method, path, body))
        return {}

    def install(self, awake):
        with mock.patch.object(self.mod, "api", side_effect=self.fake_api), \
             mock.patch.object(self.mod, "used_agents", return_value=["claude-code", "codex"]), \
             mock.patch.object(self.mod, "find_cli", side_effect=lambda a: "/bin/" + a), \
             mock.patch.object(self.mod, "probe",
                               side_effect=lambda cfg, a: (a in awake, "Not logged in")), \
             mock.patch.object(self.mod, "work_folders", return_value=[]), \
             mock.patch.object(self.mod, "install_launchd") as launchd:
            self.mod.cmd_install(self.args)
        return launchd

    def test_agents_that_cannot_be_woken_are_skipped_and_switched_off(self):
        """真遇到过：命令行 claude 没登录（桌面版登录了不算）。它叫不醒就别指望它每晚扫，
        在服务器上明确关掉，Codex 照常装。"""
        launchd = self.install(awake={"codex"})
        launchd.assert_called_once()
        cfg = json.loads(self.mod.CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(cfg["runners"], {"codex": "/bin/codex"})
        toggles = {path: body["scan_enabled"] for method, path, body in self.api_calls
                   if path.startswith("/api/agents/")}
        self.assertEqual(toggles, {"/api/agents/codex": True, "/api/agents/claude-code": False})

    def test_nothing_is_installed_when_no_agent_can_be_woken(self):
        with self.assertRaises(SystemExit):
            self.install(awake=set())
        self.assertFalse(self.mod.CONFIG.exists())
        self.assertFalse(any(p.startswith("/api/agents/") for _, p, _ in self.api_calls))

    def test_finds_codex_hidden_inside_the_chatgpt_app(self):
        """ChatGPT 桌面版把 codex 命令行藏在 app 包里，不在 PATH 上。"""
        fake = self.tmp / "ChatGPT.app" / "Contents" / "Resources" / "codex"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        spec = {"name": "codex", "also": [str(fake)]}
        with mock.patch.dict(self.mod.CLI, {"codex": spec}), \
             mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod.find_cli("codex"), str(fake))


class TestServerEndpoints(WebCase):
    def test_submit_records_in_scope_and_drops_the_rest(self):
        from fecho import cloudscan, db
        cloudscan.set_selected(A, [WORK])
        r = self.post("/api/scan/submit", A, {"date": self.today, "entries": [
            {"content": "登录做完了", "project": WORK + "/项目A", "agent": "claude-code",
             "source_event_key": "scan:k1"},
            {"content": "私事", "project": "/Users/alice/private", "source_event_key": "scan:k2"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["recorded"], 1)
        with db.cursor() as c:
            rows = [x["content_md"] for x in c.execute(
                "SELECT content_md FROM updates WHERE author=?", (A,)).fetchall()]
        self.assertIn("登录做完了", rows)
        self.assertNotIn("私事", rows)

    def test_finished_with_error_schedules_a_retry(self):
        from fecho import cloudscan
        cloudscan.set_selected(A, [WORK])
        cloudscan.set_scan_enabled(A, "claude-code", True)
        r = self.post("/api/scan/submit", A, {"date": self.today, "entries": [],
                                              "finished": True, "error": "claude 超时"})
        self.assertEqual(r.status_code, 200, r.text)
        row = cloudscan._checkin(A, self.today)
        self.assertEqual(row["status"], "failed", "记成失败，「该扫了吗」才会半小时后再让本机试")
        self.assertIn("claude 超时", row["error"])

    def test_report_folders(self):
        r = self.post("/api/folders/report", A, {"folders": [{"path": WORK, "last_used": "2030-01-10"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([f["path"] for f in r.json()["folders"]], [WORK])
        self.assertEqual(self.get("/api/folders", B).json()["items"], [], "别人看不到")

    def test_script_and_skill_are_downloadable_with_the_real_url(self):
        r = self.client.get("/local/fecho_local.py")
        self.assertEqual(r.status_code, 200)
        self.assertIn("def check", r.text)
        self.assertIn("http://testserver/local/fecho_local.py", r.text)
        self.assertEqual(self.client.get("/local/SKILL.md").status_code, 200)
        self.assertEqual(self.client.get("/local/..%2Fdashboard.html").status_code, 404)

    def test_submit_needs_a_token(self):
        self.assertEqual(self.post("/api/scan/submit", None, {"entries": []}).status_code, 401)


class TestFreshCloudReportIsNotDirty(WebCase):
    def test_report_just_generated_is_not_marked_for_regeneration(self):
        """真踩过：云端出日报用数据库里的显示名，网页判断时用邮箱，指纹永远对不上，
        黄条「需要重新生成」点多少次都消不掉。"""
        from fecho import digest
        digest.generate(A, self.today, force=True)
        r = self.get("/api/dashboard", A, params={"date": self.today})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["reports"]["daily"])
        self.assertFalse(r.json()["reports"]["dirty"])


if __name__ == "__main__":
    unittest.main()
