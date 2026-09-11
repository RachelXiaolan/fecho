"""云端版扫描的服务器一侧：该扫了吗、白名单、收结果、agent 连接。"""
import _env  # noqa: F401  必须在 import fecho 之前
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fecho import accounts, cloudscan, config, db, jobs, store  # noqa: E402
from test_cloud_auth import ADMIN, CloudCase, WebCase, A, B  # noqa: E402

WORK = "/Users/alice/Documents/work"


def bj(y, mo, d, h, mi):
    """北京时间 → 带时区的 datetime。"""
    return datetime(y, mo, d, h - 8, mi, tzinfo=timezone.utc) if h >= 8 else \
        datetime(y, mo, d, h + 16, mi, tzinfo=timezone.utc) - timedelta(days=1)


class ScanCase(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(A, "Alice")
        with db.cursor() as c:        # 让「昨天」也在账号存在之后，便于测补扫
            c.execute("UPDATE users SET created_at='2030-01-01T00:00:00+00:00' WHERE author=?", (A,))
            for t in ("work_folders", "agent_connections", "scan_checkins"):
                c.execute("DELETE FROM %s" % t)
        cloudscan.set_scan_enabled(A, "claude-code", True)
        cloudscan.set_selected(A, [WORK])


class TestDue(ScanCase):
    def test_nothing_is_scanned_without_a_whitelist(self):
        """没有白名单就什么都不读——隐私的默认值。"""
        cloudscan.set_selected(A, [])
        r = cloudscan.due(A, now=bj(2030, 1, 10, 21, 0))
        self.assertFalse(r["due"])
        self.assertIn("工作文件夹", r["reason"])

    def test_nothing_is_scanned_without_an_enabled_agent(self):
        cloudscan.set_scan_enabled(A, "claude-code", False)
        self.assertFalse(cloudscan.due(A, now=bj(2030, 1, 10, 21, 0))["due"])

    def test_due_fifteen_minutes_before_the_report(self):
        with db.cursor() as c:     # 昨天已扫，排除补扫的干扰
            c.execute("INSERT INTO scan_checkins VALUES (?,?,?,?,?,?)",
                      (A, "2030-01-09", "done", 1, None, "2030-01-09T12:50:00+00:00"))
        self.assertFalse(cloudscan.due(A, now=bj(2030, 1, 10, 20, 44))["due"])
        r = cloudscan.due(A, now=bj(2030, 1, 10, 20, 45))
        self.assertEqual((r["due"], r["date"], r["scan_at"]), (True, "2030-01-10", "20:45"))

    def test_changing_the_time_takes_effect_immediately(self):
        """网页上改时间，本机下次来问就按新时间——不用重装。"""
        accounts.set_daily_time(A, "18:00")
        r = cloudscan.due(A, now=bj(2030, 1, 10, 17, 50))
        self.assertEqual(r["scan_at"], "17:45")
        self.assertEqual(r["date"], "2030-01-10")

    def test_not_due_again_after_a_successful_scan(self):
        cloudscan.submit(A, [], date="2030-01-10", finished=True)
        r = cloudscan.due(A, now=bj(2030, 1, 10, 22, 0))
        self.assertFalse(r["due"])
        self.assertIn("已经扫过", r["reason"])

    def test_catches_up_yesterday_if_the_laptop_was_off(self):
        """昨晚没开机：今天早上一联网，先补昨天。"""
        r = cloudscan.due(A, now=bj(2030, 1, 10, 9, 0))
        self.assertEqual((r["due"], r["date"]), (True, "2030-01-09"))

    def test_does_not_catch_up_before_the_account_existed(self):
        """今天刚装的人，不会一上来就去扫昨天。"""
        with db.cursor() as c:
            c.execute("UPDATE users SET created_at='2030-01-10T01:00:00+00:00' WHERE author=?", (A,))
        self.assertFalse(cloudscan.due(A, now=bj(2030, 1, 10, 9, 0))["due"])

    def test_account_date_is_counted_in_beijing_time(self):
        """北京凌晨 2 点建的号，UTC 还是前一天。按 UTC 日期算，就会被要求去补扫
        建号之前那天——端到端实测时真遇到了。"""
        with db.cursor() as c:
            c.execute("UPDATE users SET created_at='2030-01-09T18:00:00+00:00' WHERE author=?", (A,))
        r = cloudscan.due(A, now=bj(2030, 1, 10, 9, 0))    # 建号是北京 01-10 02:00
        self.assertFalse(r["due"], "01-09 在建号之前，不该去补扫")

    def test_failed_scan_waits_half_an_hour(self):
        failed_at = bj(2030, 1, 10, 20, 50)
        with db.cursor() as c:
            c.execute("INSERT INTO scan_checkins VALUES (?,?,?,?,?,?)",
                      (A, "2030-01-10", "failed", 0, "boom", failed_at.isoformat()))
        self.assertFalse(cloudscan.due(A, now=failed_at + timedelta(minutes=20))["due"])
        self.assertTrue(cloudscan.due(A, now=failed_at + timedelta(minutes=31))["due"])

    def test_answer_carries_everything_the_local_side_needs(self):
        """本机不自己记状态：扫哪天、扫哪里、扫谁、从什么时候开始，全由这个回答带过去。"""
        r = cloudscan.due(A, now=bj(2030, 1, 10, 21, 0))
        self.assertEqual(r["folders"], [WORK])
        self.assertEqual([a["agent"] for a in r["agents"]], ["claude-code"])
        self.assertIn(".claude/projects", r["agents"][0]["transcripts"])
        self.assertEqual(r["check_every_minutes"], 15)

    def test_failed_report_becomes_a_notice(self):
        """服务器没法在用户的 Mac 上弹通知，借本机每 15 分钟来问的机会带过去。"""
        today = store.today()
        with db.cursor() as c:
            c.execute("INSERT INTO jobs (job_id, author, kind, date, status, attempts, run_after,"
                      " error, created_at) VALUES ('j1',?, 'daily', ?, 'failed', 2, 'x', '网关超时', 'x')",
                      (A, today))
        self.assertIn("网关超时", cloudscan.due(A)["notices"][0])


class TestWhitelist(ScanCase):
    def test_folder_match_is_by_whole_path_segment(self):
        """/work 不能顺带放行 /workshop。"""
        self.assertTrue(cloudscan.in_scope(WORK, [WORK]))
        self.assertTrue(cloudscan.in_scope(WORK + "/fecho", [WORK]))
        self.assertFalse(cloudscan.in_scope(WORK + "shop/private", [WORK]))
        self.assertFalse(cloudscan.in_scope("/Users/alice/Documents/diary", [WORK]))
        self.assertFalse(cloudscan.in_scope("", [WORK]))

    def test_reported_folders_start_unticked(self):
        """白名单只能由人主动勾，agent 上报的新文件夹默认不算。"""
        cloudscan.report_folders(A, [{"path": "/Users/alice/personal"}, {"path": "relative/x"}])
        folders = {f["path"]: f["selected"] for f in cloudscan.folders_of(A)}
        self.assertFalse(folders["/Users/alice/personal"])
        self.assertTrue(folders[WORK], "之前勾过的要保留")
        self.assertNotIn("relative/x", folders, "相对路径对不上聊天记录里的目录，不收")

    def test_reporting_again_keeps_ticks(self):
        cloudscan.report_folders(A, [{"path": WORK, "last_used": "2030-01-10"}])
        self.assertEqual(cloudscan.selected_folders(A), [WORK])


class TestSubmit(ScanCase):
    def entry(self, text, project=WORK, key=None, date="2030-01-10"):
        return {"content": text, "kind": "done", "project": project, "date": date,
                "agent": "claude-code", "session_id": "s1", "source_event_key": key}

    def test_in_scope_entries_are_recorded_under_the_right_person(self):
        r = cloudscan.submit(A, [self.entry("把登录做完了")], date="2030-01-10")
        self.assertEqual(r["recorded"], 1)
        with db.cursor() as c:
            row = dict(c.execute("SELECT author, ingestion_method FROM updates").fetchone())
        self.assertEqual(row, {"author": A, "ingestion_method": "transcript-scan"})

    def test_out_of_scope_entries_are_dropped_by_the_server_too(self):
        """本机那边漏过了，服务器这一道也不收。"""
        r = cloudscan.submit(A, [self.entry("私人日记", project="/Users/alice/diary")])
        self.assertEqual((r["recorded"], r["out_of_scope"]), (0, 1))

    def test_same_event_key_is_recorded_once(self):
        """扫描中断后重扫，同一条不会记两遍。"""
        e = self.entry("接好了 Supabase", key="claude-code:s1:3")
        cloudscan.submit(A, [e])
        r = cloudscan.submit(A, [e])
        self.assertEqual((r["recorded"], r["duplicate"]), (0, 1))

    def test_finished_marks_the_day_scanned(self):
        cloudscan.submit(A, [self.entry("x")], date="2030-01-10", finished=True)
        self.assertFalse(cloudscan.due(A, now=bj(2030, 1, 10, 22, 0))["due"])

    def test_error_marks_the_day_failed(self):
        cloudscan.submit(A, [], date="2030-01-10", finished=True, error="读不到文件")
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT status FROM scan_checkins").fetchone()["status"],
                             "failed")

    def test_late_results_requeue_that_days_report(self):
        """出日报时电脑没联网，事后补扫交上来——那天的日报要自动重出。"""
        with db.cursor() as c:
            c.execute("INSERT INTO reports (report_id, author, date, kind, content_md, fingerprint,"
                      " generator, created_at) VALUES ('r1',?, '2030-01-10','daily','旧的','f','llm','x')",
                      (A,))
        r = cloudscan.submit(A, [self.entry("补扫出来的一条")], date="2030-01-10")
        self.assertEqual(r["regenerate_queued"], ["2030-01-10"])
        with db.cursor() as c:
            job = dict(c.execute("SELECT kind, date FROM jobs WHERE author=?", (A,)).fetchone())
        self.assertEqual(job, {"kind": "regenerate", "date": "2030-01-10"})

    def test_no_requeue_when_no_report_exists_yet(self):
        r = cloudscan.submit(A, [self.entry("正常时间扫到的")], date="2030-01-10")
        self.assertEqual(r["regenerate_queued"], [])


class TestAgents(ScanCase):
    def test_client_names_map_to_agents(self):
        self.assertEqual(cloudscan.agent_id("claude-code"), "claude-code")
        self.assertEqual(cloudscan.agent_id("Codex-MCP-Client"), "codex")
        self.assertEqual(cloudscan.agent_id("hermes"), "hermes")
        self.assertIsNone(cloudscan.agent_id("some-random-client"))

    def test_unused_agents_are_not_scanned_by_default(self):
        """没用过的 agent 没有聊天记录可扫。"""
        states = {a["agent"]: a for a in cloudscan.agents(A)}
        self.assertTrue(states["claude-code"]["scan_enabled"])
        self.assertFalse(states["codex"]["scan_enabled"])
        self.assertFalse(states["codex"]["connected"])

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(ValueError):
            cloudscan.set_scan_enabled(A, "openclaw", True)


class TestCloudMcpAndApi(WebCase):
    def mcp(self, who, method, params=None, sid=None):
        headers = {"Authorization": "Bearer " + self.tok[who]}
        if sid:
            headers["Mcp-Session-Id"] = sid
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post("/mcp", headers=headers, json=body)

    def call(self, who, name, **args):
        r = self.mcp(who, "tools/call", {"name": name, "arguments": args}).json()
        return r["result"]["content"][0]["text"]

    def test_connecting_an_agent_lights_it_up(self):
        """网页上的「已连接」靠 agent 握手时报的名字。"""
        self.mcp(A, "initialize", {"clientInfo": {"name": "claude-code"}})
        states = {a["agent"]: a for a in self.get("/api/agents", A).json()["items"]}
        self.assertTrue(states["claude-code"]["connected"])
        self.assertFalse(states["codex"]["connected"])

    def test_cloud_tools_show_only_in_cloud(self):
        names = {t["name"] for t in self.mcp(A, "tools/list").json()["result"]["tools"]}
        self.assertTrue({"submit_scan", "report_work_folders", "set_daily_time"} <= names)

    def test_report_folders_then_tick_on_the_web(self):
        self.call(A, "report_work_folders", folders=[{"path": WORK}, {"path": "/Users/a/diary"}])
        self.assertEqual({f["path"] for f in self.get("/api/folders", A).json()["items"]},
                         {WORK, "/Users/a/diary"})
        self.post("/api/folders", A, {"selected": [WORK]})
        self.assertEqual(cloudscan.selected_folders(A), [WORK])

    def test_submit_scan_through_mcp(self):
        cloudscan.set_selected(A, [WORK])
        text = self.call(A, "submit_scan", date=self.today, finished=True,
                         entries=[{"content": "MCP 交上来的扫描结果", "kind": "done",
                                   "project": WORK}])
        self.assertIn("新记 1 条", text)
        self.assertIn("完成", text)

    def test_set_daily_time_through_mcp(self):
        """跟 agent 说「改到 20:00」就行。"""
        text = self.call(A, "set_daily_time", time="20:00")
        self.assertIn("19:45", text)
        self.assertEqual(accounts.get_user(A)["daily_time"], "20:00")

    def test_end_of_day_is_queued(self):
        from fecho import service
        with mock.patch.object(service, "end_of_day") as eod:
            text = self.call(A, "end_of_day")
        eod.assert_not_called()
        self.assertIn("已排队", text)

    def test_team_digest_is_admin_only(self):
        self.assertIn("只有 admin", self.call(A, "team_digest"))
        self.assertIn("团队日报", self.call(ADMIN, "team_digest"))

    def test_doctor_reports_cloud_state(self):
        text = self.call(A, "fecho_doctor")
        self.assertIn("云端版", text)
        self.assertIn(A, text)

    def test_scan_due_endpoint_needs_a_token(self):
        self.assertEqual(self.client.get("/api/scan/due").status_code, 401)
        self.assertIn("due", self.get("/api/scan/due", A).json())

    def test_healthz_is_public_in_cloud(self):
        """部署平台和监控要能不登录访问，只返回版本号。"""
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"ok", "version"})

    def test_install_doc_points_at_this_server(self):
        doc = self.client.get("/install").text
        self.assertIn("http://testserver/mcp", doc)
        self.assertIn("report_work_folders", doc)
        self.assertNotIn("__URL__", doc)


class TestLocalModeUnchanged(unittest.TestCase):
    def test_cloud_only_tools_are_refused_locally(self):
        from fecho import mcp_server
        with self.assertRaises(RuntimeError):
            mcp_server.call_tool("submit_scan", {"entries": []})
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertNotIn("submit_scan", names)


if __name__ == "__main__":
    unittest.main()
