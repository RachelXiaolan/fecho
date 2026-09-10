"""核心逻辑单测：配对、归并、整理、降级。

不依赖 pytest：python3 tests/test_core.py
不碰生产库、不联网：全部指向临时目录，Mobius issue 用假缓存。
"""
import contextlib
import json
import os
import queue
import time
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="fecho-test-")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update({
    "FECHO_DB": os.path.join(TMP, "t.db"),
    "FECHO_LOGS_DIR": os.path.join(TMP, "logs"),
    "FECHO_CONFIG_DIR": os.path.join(TMP, "config"),
    "FECHO_TOKENS": os.path.join(TMP, "config", "tokens.json"),
    "FECHO_HOME": os.path.join(TMP, "home"),
    "FECHO_PERSONAS_DIR": os.path.join(ROOT, "fecho", "presets", "personas"),
    "FECHO_PTO_FILE": os.path.join(TMP, "config", "pto.json"),
    "FECHO_LLM_BASE_URL": "", "FECHO_LLM_API_KEY": "",
    "FECHO_MOBIUS_URL": "", "FECHO_MOBIUS_TOKEN": "",
})
os.makedirs(os.path.join(TMP, "config"), exist_ok=True)
with open(os.environ["FECHO_TOKENS"], "w") as f:
    json.dump({"tk": {"author": "t", "display_name": "T", "persona": "default"}}, f)
with open(os.environ["FECHO_PTO_FILE"], "w") as f:
    json.dump({"t": ["2030-01-02"]}, f)

sys.path.insert(0, ROOT)
from fecho import config, db, digest, llm, match, store  # noqa: E402

D = "2030-01-01"


@contextlib.contextmanager
def mock_llm(reply):
    """让 llm.chat 返回固定内容。测归属验证这类逻辑时不该真去打模型。"""
    orig_chat, orig_cfg = llm.chat, config.llm_configured
    llm.chat = lambda *a, **k: reply
    config.llm_configured = lambda: True
    try:
        yield
    finally:
        llm.chat, config.llm_configured = orig_chat, orig_cfg
ISSUES = [
    {"issue_key": "AI-2541", "title": "写一个提交工作日志的系统（给agent专用）"},
    {"issue_key": "AI-2539", "title": "本地试用 awesome-gpt-image-2 并评估复用价值"},
    {"issue_key": "AI-2460", "title": "申请新lu3服务器 8G 部署产品级的QM"},
    # 字面上和「闲鱼选品」零重合，但说的是同一件事——语义归属的典型案例
    {"issue_key": "AI-2224", "title": "与小Lu商量，有关Linux.do积分的事情（例如开小店等）"},
]


def seed_issues(author="t"):
    with db.cursor() as c:
        c.execute("DELETE FROM mobius_issues WHERE author=?", (author,))
        for i in ISSUES:
            c.execute("INSERT INTO mobius_issues (issue_key,author,title,state,url,"
                      "updated_at,synced_at,raw) VALUES (?,?,?,?,?,?,?,?)",
                      (i["issue_key"], author, i["title"], "In Progress", "", "",
                       store.now_iso(), "{}"))


def reset():
    db.init()
    with db.cursor() as c:
        c.execute("DELETE FROM task_events")
        c.execute("DELETE FROM assignment_events")
        c.execute("DELETE FROM updates")
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM reports")
        c.execute("DELETE FROM scan_runs")
        c.execute("DELETE FROM assignment_verifications")
    seed_issues()


class TestMatching(unittest.TestCase):
    def setUp(self):
        reset()

    def test_explicit_issue_key_in_text_wins(self):
        r = store.record_progress("t", "顺手把 AI-2541 的配对引擎写了", date=D)
        self.assertEqual(r["match"]["method"], "explicit")
        self.assertEqual(r["task"]["issue_key"], "AI-2541")

    def test_never_guesses_an_issue_from_wording(self):
        """字面像不代表是同一件事。归属交给读得懂意思的模型（scan 里的 m3、
        或调用方 agent 自己），这一层配不上就老实走自由任务。"""
        r = store.record_progress("t", "本地试了下 awesome-gpt-image-2，出图质量一般", date=D)
        self.assertEqual(r["match"]["method"], "new-task")
        self.assertIsNone(r["task"]["issue_key"])

    def test_unrelated_work_becomes_a_freeform_task(self):
        r = store.record_progress("t", "帮同事看了下他那个爬虫为什么超时", date=D)
        self.assertEqual(r["match"]["method"], "new-task")
        self.assertEqual(r["task"]["source"], "freeform")
        self.assertIsNone(r["task"]["issue_key"])

    def test_vague_followup_stays_on_the_task_from_the_same_session(self):
        """「跑通了接口」这种话本身配不到任何 issue，只能靠同一个对话的上下文。"""
        s = "sess-1"
        a = store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D, session_id=s)
        b = store.record_progress("t", "接口和数据库跑通了", date=D, session_id=s)
        self.assertEqual(b["match"]["method"], "task-continue")
        self.assertEqual(b["task"]["task_id"], a["task"]["task_id"])

    def test_different_session_does_not_inherit_the_task(self):
        store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D, session_id="s1")
        b = store.record_progress("t", "帮同事看了下他那个爬虫为什么超时", date=D, session_id="s2")
        self.assertEqual(b["task"]["source"], "freeform")

    def test_explicit_issue_beats_session_inertia(self):
        """会话惯性只是默认值：正文里写了 issue 号时必须让位。"""
        s = "sess-2"
        store.record_progress("t", "开始做工作日志系统", date=D, session_id=s)
        b = store.record_progress("t", "顺手把 AI-2539 那个也试了", date=D, session_id=s)
        self.assertEqual(b["task"]["issue_key"], "AI-2539")

    def test_session_fallback_is_marked_low_confidence(self):
        """同一对话里换了话题又没有特征词时会错归——这是已知代价。
        V0 不假装能解决，只要求这次配对被标成低置信，让 agent 看得见、能改。"""
        s = "sess-3"
        a = store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D, session_id=s)
        b = store.record_progress("t", "顺手把那个东西也弄了一下", date=D, session_id=s)
        self.assertEqual(b["task"]["task_id"], a["task"]["task_id"])
        self.assertEqual(b["match"].get("via"), "same-session")

    def test_agent_can_override_a_wrong_guess(self):
        s = "sess-4"
        store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D, session_id=s)
        b = store.record_progress("t", "顺手把那个东西也弄了一下", date=D, session_id=s,
                                  issue="AI-2460")
        self.assertEqual(b["match"]["method"], "explicit")
        self.assertEqual(b["task"]["issue_key"], "AI-2460")

    def test_invalid_calendar_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "有效日期"):
            store.record_progress("t", "一条进展", date="2030-99-99")

    def test_unknown_explicit_issue_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不在已同步的 Mobius issue"):
            store.record_progress("t", "一条进展", date=D, issue="AI-999999")

    def test_unknown_issue_mentioned_in_content_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不在已同步的 Mobius issue"):
            store.record_progress("t", "完成了 AI-999999", date=D)


class TestNoSemanticGuessing(unittest.TestCase):
    """真实翻车：闲鱼选品的内容被配进「写一个提交工作日志的系统」，唯一的共同点
    是两句话里都出现了 agent。字面相似度不该用来判归属——这条路已经删掉了。"""

    def setUp(self):
        reset()

    def test_decide_never_returns_a_keyword_matched_issue(self):
        for text in ("本地试了下 awesome-gpt-image-2，出图一般",
                     "LDC 建议售价只是价格带映射，agent 无这部分数据",
                     "读完 PRD，把工作日志系统的 V0 边界定下来了"):
            d = match.decide(text, tasks=[])
            self.assertEqual(d["method"], "new-task",
                             "%r 不该被字面猜出归属" % text[:16])

    def test_similarity_still_serves_dedupe(self):
        """similarity 干的是另一件事：比两条**进展之间**像不像，用来挡扫描
        重跑产生的近似重复。文本对文本正是字符串相似度擅长的。"""
        a = "反向链路 catch_up 做完了，agent 开工时自己拉回昨日日报"
        self.assertGreaterEqual(
            match.similarity(a, "catch_up 反向链路做完，agent 开工时自己拉回昨天的日报"),
            config.SCAN_DEDUPE_SIMILARITY, "同一件事的两种说法应判为近似重复")
        self.assertLess(match.similarity(a, "闲鱼那边抓了十六个商品的详情"),
                        config.SCAN_DEDUPE_SIMILARITY, "不同的事不该被当成重复")


class TestScanDoesNotCascade(unittest.TestCase):
    """扫描是批量抽取，同 session 的条目没有对话先后关系。
    一条配错时，会话惯性会把后面全带偏——实测就是这么发生的。"""

    def setUp(self):
        reset()

    def test_scan_entry_does_not_inherit_session_task(self):
        a = store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D,
                                  session_id="s1", source_agent="scan")
        b = store.record_progress("t", "闲鱼那边抓了十六个商品的详情", date=D,
                                  session_id="s1", source_agent="scan")
        self.assertNotEqual(b["task"]["task_id"], a["task"]["task_id"])
        self.assertEqual(b["match"]["method"], "new-task")

    def test_named_scan_producer_does_not_inherit_session_task(self):
        a = store.record_progress(
            "t", "开始做 AI-2541 的工作日志系统", date=D, session_id="codex:s1",
            source_agent="codex", ingestion_method="transcript-scan")
        b = store.record_progress(
            "t", "闲鱼那边抓了十六个商品的详情", date=D, session_id="codex:s1",
            source_agent="codex", ingestion_method="transcript-scan")
        self.assertNotEqual(b["task"]["task_id"], a["task"]["task_id"])
        self.assertEqual(b["match"]["method"], "new-task")

    def test_interactive_entry_still_inherits(self):
        """但交互式记录保留会话兜底——那里「刚才在聊什么」是真实上下文。"""
        a = store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D,
                                  session_id="s2", source_agent="claude-code")
        b = store.record_progress("t", "顺手把那个东西也弄了一下", date=D,
                                  session_id="s2", source_agent="claude-code")
        self.assertEqual(b["task"]["task_id"], a["task"]["task_id"])


class TestScanAssignsIssues(unittest.TestCase):
    """扫描时归属由读得懂意思的模型判，不再靠字面相似度。"""

    def setUp(self):
        reset()

    def test_parses_issue_column(self):
        from fecho import scan
        got = scan.parse_entries(
            "done | AI-2541 | 配对引擎写完了\n"
            "pitfall | - | 闲鱼 LDC 售价只是价格带映射\n"
            "done | 内容里带 | 竖线也不该被截断")
        self.assertEqual(got[0]["issue"], "AI-2541")
        self.assertIsNone(got[1]["issue"], "写 - 的该归自由任务")
        self.assertEqual(got[2]["content"], "竖线也不该被截断")

    def test_ignores_issue_keys_not_in_the_candidate_list(self):
        """模型编的 issue 号不能当真。光校验形状不够——它能吐出格式完全正确
        但根本不存在的号，那就是凭空造归属。按真实候选列表校验。"""
        from fecho import scan
        got = scan.parse_entries(
            "done | AI-2541 | 真实存在的\ndone | AI-9999 | 格式对但不存在\ndone | 不知道 | 压根不是号",
            valid_keys={"AI-2541"})
        self.assertEqual(got[0]["issue"], "AI-2541")
        self.assertIsNone(got[1]["issue"], "格式合法但不在候选列表里，不能认")
        self.assertIsNone(got[2]["issue"])

    def test_prompt_lists_candidates_and_forbids_inventing(self):
        from fecho import scan
        p = scan._prompt([{"issue_key": "AI-2541", "title": "写一个提交工作日志的系统"}])
        self.assertIn("AI-2541：写一个提交工作日志的系统", p)
        self.assertIn("不许自己编", p)

    def test_prompt_without_issues_still_valid(self):
        from fecho import scan
        self.assertIn("没有在办的 issue", scan._prompt([]))


class TestDedupeKeepsData(unittest.TestCase):
    """判为近似重复的照样写进库，只是不进日报。

    相似度是纯字符串判断，没有后续环节能纠错——直接不写就等于这条内容从没
    存在过，日报看不到、人也翻不到。留一行几乎没成本，丢一条真实进展成本很高。
    """

    def setUp(self):
        reset()

    def _pair(self):
        a = "反向链路 catch_up 做完了，agent 开工时自己拉回昨日日报"
        b = "catch_up 反向链路做完，agent 开工时自己拉回昨天的日报"
        return (store.record_progress("t", a, date=D, source_agent="scan", issue="AI-2541"),
                store.record_progress("t", b, date=D, source_agent="scan", issue="AI-2541"))

    def test_near_duplicate_is_stored_not_dropped(self):
        first, second = self._pair()
        self.assertEqual(second["verdict"], "duplicate")
        with db.cursor() as c:
            row = c.execute("SELECT status, meta FROM updates WHERE update_id=?",
                            (second["update_id"],)).fetchone()
        self.assertIsNotNone(row, "判为重复的进展必须还在库里")
        self.assertEqual(row["status"], "duplicate-ignored")
        self.assertEqual(json.loads(row["meta"])["duplicate_of"], first["update_id"],
                         "要记下它被判成了谁的重复，才能复查")

    def test_duplicates_stay_out_of_the_report(self):
        self._pair()
        tasks = db.day_tasks("t", D)
        self.assertEqual(sum(len(t["updates"]) for t in tasks), 1,
                         "日报只看 active，重复的不该出现")

    def test_exact_duplicate_still_written_once(self):
        """逐字相同的是 agent 重试，不是新进展——那种确实不必存第二份。"""
        a = store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        b = store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        self.assertEqual(b["verdict"], "duplicate")
        self.assertEqual(b["update_id"], a["update_id"])

    def test_agent_recorded_similar_entries_are_all_kept(self):
        """只有扫描来源才做近似去重。agent 主动记的相似内容可能是真实的不同
        进展（措辞碰巧像），一条都不能少。"""
        store.record_progress("t", "反向链路 catch_up 做完了，agent 开工时拉回昨日日报",
                              date=D, issue="AI-2541")
        store.record_progress("t", "catch_up 反向链路做完，agent 开工时拉回昨天的日报",
                              date=D, issue="AI-2541")
        tasks = db.day_tasks("t", D)
        self.assertEqual(sum(len(t["updates"]) for t in tasks), 2)

    def test_cli_dedupe_finds_named_transcript_producers(self):
        from argparse import Namespace
        from fecho import cli

        a = store.record_progress(
            "t", "反向链路 catch_up 做完了，agent 开工时拉回昨日日报", date=D,
            issue="AI-2541", source_agent="codex", ingestion_method="transcript-scan")
        b = store.record_progress(
            "t", "catch_up 反向链路做完，agent 开工时拉回昨天的日报", date=D,
            issue="AI-2541", source_agent="codex", ingestion_method="transcript-scan")
        with db.cursor() as conn:
            conn.execute("UPDATE updates SET status='active' WHERE update_id=?", (b["update_id"],))
        with contextlib.redirect_stdout(__import__("io").StringIO()):
            cli.cmd_dedupe(Namespace(date=D, apply=True))
        with db.cursor() as conn:
            status = conn.execute("SELECT status FROM updates WHERE update_id=?",
                                  (b["update_id"],)).fetchone()["status"]
        self.assertEqual(status, "superseded")


class TestCrossValidation(unittest.TestCase):
    """出稿前把归属重判一次。连 agent 明确填的 issue 号也要重判——那同样是模型的
    判断，记的时候手上只有当前那一条的上下文。"""

    def setUp(self):
        reset()

    def test_reassign_moves_an_update_between_issues(self):
        r = store.record_progress("t", "抓了十六个商品详情", date=D, issue="AI-2541")
        self.assertEqual(r["task"]["issue_key"], "AI-2541")
        self.assertTrue(store.reassign(r["update_id"], "t", "AI-2224"))
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertEqual(rows[r["update_id"]]["issue_key"], "AI-2224")
        self.assertEqual(rows[r["update_id"]]["match_method"], "verified")

    def test_reassign_can_send_it_back_to_freeform(self):
        r = store.record_progress("t", "帮同事看爬虫超时", date=D, issue="AI-2541")
        self.assertTrue(store.reassign(r["update_id"], "t", None))
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertIsNone(rows[r["update_id"]]["issue_key"])

    def test_reassign_is_a_noop_when_already_right(self):
        r = store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        self.assertFalse(store.reassign(r["update_id"], "t", "AI-2541"))

    def test_verify_corrects_a_wrong_explicit_issue(self):
        """agent 把闲鱼的活填成了 AI-2541，验证那步该纠回来。"""
        r = store.record_progress("t", "闲鱼抓了十六个商品详情", date=D, issue="AI-2541")
        with mock_llm("1 | AI-2224"):
            res = digest.verify_assignments("t", D)
        self.assertEqual(len(res["changed"]), 1)
        self.assertEqual(res["changed"][0]["to"], "AI-2224")
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertEqual(rows[r["update_id"]]["issue_key"], "AI-2224")

    def test_verify_ignores_issue_keys_that_do_not_exist(self):
        r = store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        with mock_llm("1 | AI-9999"):
            digest.verify_assignments("t", D)
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertIsNone(rows[r["update_id"]]["issue_key"],
                          "不存在的 issue 号该当作「都不属于」，落自由任务")

    def test_verify_leaves_entries_the_model_skipped(self):
        r = store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        with mock_llm("（模型没按格式给）"):
            res = digest.verify_assignments("t", D)
        self.assertEqual(res["changed"], [])
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertEqual(rows[r["update_id"]]["issue_key"], "AI-2541")

    def test_human_correction_updates_the_same_row_and_writes_audit_event(self):
        r = store.record_progress("t", "闲鱼抓了十六个商品详情", date=D, issue="AI-2541")
        changed = store.correct_progress(
            r["update_id"], "t", issue_key="AI-2224",
            content_md="闲鱼抓取并整理了十六个商品详情")

        self.assertTrue(changed["changed"])
        rows = db.list_updates(date=D, author="t")
        self.assertEqual(len(rows), 1, "纠错必须原地修订，不能靠重记制造重复进展")
        self.assertEqual(rows[0]["update_id"], r["update_id"])
        self.assertEqual(rows[0]["content_md"], "闲鱼抓取并整理了十六个商品详情")
        self.assertEqual(rows[0]["assignment_source"], "human")
        self.assertEqual(rows[0]["assignment_locked"], 1)
        self.assertEqual(rows[0]["revision"], 2)
        with db.cursor() as conn:
            event = conn.execute(
                "SELECT * FROM assignment_events WHERE update_id=?", (r["update_id"],)
            ).fetchone()
        self.assertEqual(event["from_issue_key"], "AI-2541")
        self.assertEqual(event["to_issue_key"], "AI-2224")

    def test_human_correction_is_not_overwritten_by_llm_verification(self):
        r = store.record_progress("t", "闲鱼抓了十六个商品详情", date=D, issue="AI-2541")
        store.correct_progress(r["update_id"], "t", issue_key="AI-2224")
        with mock_llm("1 | AI-2541"):
            res = digest.verify_assignments("t", D)
        self.assertEqual(res["checked"], 0, "人工锁定的归属不应再交给模型重判")
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertEqual(rows[r["update_id"]]["issue_key"], "AI-2224")

    def test_empty_issue_cache_skips_verification_without_erasing_assignment(self):
        r = store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        with db.cursor() as conn:
            conn.execute("DELETE FROM mobius_issues WHERE author='t'")
        with mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch("fecho.llm.chat") as chat:
            res = digest.verify_assignments("t", D)
        chat.assert_not_called()
        self.assertIn("缓存为空", res["error"])
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertEqual(rows[r["update_id"]]["issue_key"], "AI-2541")

    def test_stale_issue_cache_skips_verification(self):
        store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        with db.cursor() as conn:
            conn.execute("UPDATE mobius_issues SET synced_at='2000-01-01T00:00:00+00:00'"
                         " WHERE author='t'")
        with mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch("fecho.llm.chat") as chat:
            res = digest.verify_assignments("t", D)
        chat.assert_not_called()
        self.assertIn("已过期", res["error"])

    def test_unchanged_verification_uses_fingerprint_cache(self):
        store.record_progress("t", "配对引擎写完了", date=D, issue="AI-2541")
        with mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch("fecho.llm.chat", return_value="1 | AI-2541") as chat:
            first = digest.verify_assignments("t", D)
            second = digest.verify_assignments("t", D)
        self.assertEqual(chat.call_count, 1)
        self.assertFalse(first.get("cached", False))
        self.assertTrue(second["cached"])


class TestWebEndpoints(unittest.TestCase):
    """HTTP 层。SSE 那套握手是两条腿（GET 开流 + POST 发消息），
    ChatGPT 要的就是它，不实跑一遍没法确认。"""

    def setUp(self):
        reset()
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("没装 fastapi[server] 额外依赖")
        from fecho import web
        # TestClient 的来源不是 127.0.0.1，正好走鉴权那条路
        self._orig_token, web.TOKEN = web.TOKEN, "t3st-token"
        # 接口一律按 config.AUTHOR 取数，测试数据的作者是 "t"
        self._orig_author, config.AUTHOR = config.AUTHOR, "t"
        self.addCleanup(lambda: setattr(config, "AUTHOR", self._orig_author))
        self.web = web
        self.c = TestClient(web.build_app(),
                            headers={"Authorization": "Bearer t3st-token"})
        self.addCleanup(lambda: setattr(web, "TOKEN", self._orig_token))

    def test_non_local_access_without_token_is_refused(self):
        """隧道一开 /mcp 就在公网上了。宁可连不上，也不要默认裸奔。"""
        from fastapi.testclient import TestClient
        bare = TestClient(self.web.build_app())
        self.assertEqual(bare.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                 "method": "ping"}).status_code, 401)
        self.web.TOKEN = ""
        bare2 = TestClient(self.web.build_app())
        self.assertEqual(bare2.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                  "method": "ping"}).status_code, 403,
                         "没设 token 时外部来源该被直接拒绝")

    def test_mcp_over_http_lists_the_same_tools_as_stdio(self):
        from fecho import mcp_server
        r = self.c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 200)
        got = [t["name"] for t in r.json()["result"]["tools"]]
        self.assertEqual(got, [t["name"] for t in mcp_server.TOOLS],
                         "工具集不该按客户端分——谁连上都是同一套")

    def test_http_clients_keep_separate_agent_and_session_context(self):
        from fastapi.testclient import TestClient

        a = TestClient(self.web.build_app(), headers={"Authorization": "Bearer t3st-token"})
        b = TestClient(self.web.build_app(), headers={"Authorization": "Bearer t3st-token"})

        def initialize(client, name):
            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "initialize", "params": {
                                               "protocolVersion": "2024-11-05",
                                               "clientInfo": {"name": name}}})
            self.assertEqual(r.status_code, 200)
            return r.headers["Mcp-Session-Id"]

        def log(client, sid, content, issue=None):
            args = {"content": content, "date": D}
            if issue:
                args["issue"] = issue
            return client.post("/mcp", headers={"Mcp-Session-Id": sid}, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "log_progress", "arguments": args}})

        sid_a = initialize(a, "codex")
        sid_b = initialize(b, "claude-code")
        self.assertNotEqual(sid_a, sid_b)
        self.assertFalse(log(a, sid_a, "Fecho 接口完成", "AI-2541").json()["result"]["isError"])
        self.assertFalse(log(b, sid_b, "闲鱼调研完成", "AI-2224").json()["result"]["isError"])

        updates = db.list_updates(date=D, author="t")
        self.assertEqual({u["source_agent"] for u in updates}, {"codex", "claude-code"})
        self.assertEqual(len({u["session_id"] for u in updates}), 2)

    def test_log_progress_does_not_allow_callers_to_spoof_agent(self):
        from fecho import mcp_server
        schema = next(t for t in mcp_server.TOOLS if t["name"] == "log_progress")
        self.assertNotIn("agent", schema["inputSchema"]["properties"])

    def test_log_progress_returns_update_id_and_assignment_as_structured_data(self):
        r = self.c.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "log_progress", "arguments": {
                    "content": "Fecho MCP 结构化返回完成", "issue": "AI-2541", "date": D}}})
        result = r.json()["result"]
        self.assertFalse(result["isError"])
        self.assertRegex(result["structuredContent"]["update_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(result["structuredContent"]["issue_key"], "AI-2541")
        self.assertEqual(result["structuredContent"]["match_method"], "explicit")

    def test_correct_progress_tool_revises_in_place_and_locks_assignment(self):
        rec = store.record_progress("t", "最初归错的进展", date=D, issue="AI-2541")
        r = self.c.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "correct_progress", "arguments": {
                    "update_id": rec["update_id"], "issue": "AI-2224",
                    "content": "修正后的闲鱼进展"}}})
        result = r.json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["update_id"], rec["update_id"])
        rows = db.list_updates(date=D, author="t")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["assignment_locked"], 1)
        self.assertEqual(db.get_task(rows[0]["task_id"])["issue_key"], "AI-2224")

    def test_sse_full_roundtrip_against_a_real_server(self):
        """SSE 是两条腿：GET 开流拿到 POST 地址，响应再顺着流推回来。
        ChatGPT 要的就是这套，只验一条腿等于没验。

        起真的 uvicorn：/sse/ 是条无限流，同步的 TestClient 进去就出不来。
        """
        import json as _json
        import re
        import socket
        import threading
        import urllib.request

        import uvicorn

        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]

        self.web.TOKEN = ""                      # 本机直连，走 guard 的放行分支
        server = uvicorn.Server(uvicorn.Config(self.web.build_app(), host="127.0.0.1",
                                               port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        self.addCleanup(setattr, server, "should_exit", True)
        base = "http://127.0.0.1:%d" % port
        for _ in range(100):                     # 等它起来
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.05)

        events: "queue.Queue[str]" = queue.Queue()

        def pump():
            r = urllib.request.urlopen(base + "/sse/", timeout=20)
            buf = ""
            for chunk in r:
                buf += chunk.decode()
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    events.put(block)

        threading.Thread(target=pump, daemon=True).start()

        first = events.get(timeout=10)
        self.assertTrue(first.startswith("event: endpoint"), first)
        post_to = re.search(r"data: (\S+)", first).group(1)
        self.assertIn("/sse/messages?session_id=", post_to)

        req = urllib.request.Request(
            base + post_to, headers={"Content-Type": "application/json"},
            data=_json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
        self.assertEqual(urllib.request.urlopen(req, timeout=10).status, 202,
                         "POST 只回执，真正的响应走 SSE 流")

        while True:                              # keep-alive 之外的第一条消息
            block = events.get(timeout=15)
            if block.startswith("event: message"):
                payload = _json.loads(block.split("data: ", 1)[1])
                break
        from fecho import mcp_server
        self.assertEqual([t["name"] for t in payload["result"]["tools"]],
                         [t["name"] for t in mcp_server.TOOLS])

    def test_sse_post_to_a_dead_session_is_rejected(self):
        r = self.c.post("/sse/messages?session_id=nope",
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(r.status_code, 404)

    def test_reassign_through_the_api(self):
        rec = store.record_progress("t", "闲鱼抓了十六个商品", date=D, issue="AI-2541")
        r = self.c.post("/api/reassign",
                        json={"update_id": rec["update_id"], "issue_key": "AI-2224"})
        self.assertTrue(r.json()["ok"])
        rows = {u["update_id"]: u for u in db.day_updates("t", D)}
        self.assertEqual(rows[rec["update_id"]]["issue_key"], "AI-2224")

    def test_review_queue_includes_explicit_entries_until_a_human_confirms_them(self):
        store.record_progress("t", "明确写了 AI-2541 的进展", date=D, issue="AI-2541")
        store.record_progress("t", "没有任何线索的一条", date=D)
        from fecho import web
        items = web.review_queue("t", D)
        self.assertEqual(len(items), 2, "agent 明确填的 issue 也可能错，人工确认前必须可见")
        self.assertEqual({item["method"] for item in items}, {"explicit", "new-task"})

    def test_dashboard_payload_is_complete_and_honors_historical_date(self):
        store.record_progress("t", "AI-2541 历史进展", date=D,
                              source_agent="codex", completion_status="done")
        store.record_progress("t", "AI-2224 今天进展", date="2030-01-02")
        r = self.c.get("/api/dashboard", params={"date": D})
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(payload["date"], D)
        self.assertEqual(payload["overview"]["updates"], 1)
        self.assertTrue({"overview", "review", "tasks", "reports", "hidden",
                         "timeline", "issues", "system", "filters"} <= set(payload))
        self.assertEqual(payload["review"]["items"][0]["source_agent"], "codex")
        self.assertRegex(payload["system"]["doctor"]["version"], r"^\d+\.\d+\.\d+$")

    def test_dashboard_payload_exposes_redacted_automation_status(self):
        state = {
            "enabled": True, "daily_time": "21:00", "timezone": "Asia/Shanghai",
            "dashboard_url": "http://127.0.0.1:8900/",
            "last_result": {"status": "succeeded", "date": D},
        }
        with mock.patch("fecho.automation.status", return_value=state):
            payload = self.c.get("/api/dashboard", params={"date": D}).json()
        self.assertEqual(payload["system"]["automation"], state)
        self.assertNotIn("api_key", json.dumps(payload["system"]["automation"]))

    def test_doctor_reports_automation_readiness(self):
        from fecho import service
        state = {"enabled": True, "daily_time": "21:00", "timezone": "Asia/Shanghai",
                 "launch_agents": {"daily": True, "dashboard": True}}
        with mock.patch("fecho.automation.status", return_value=state):
            result = service.doctor()
        check = next(item for item in result["checks"] if item["name"] == "每日自动整理")
        self.assertTrue(check["ok"])
        self.assertIn("21:00", check["detail"])

    def test_mutation_returns_refreshed_dashboard_and_marks_report_dirty(self):
        rec = store.record_progress("t", "闲鱼抓了十六个商品", date=D, issue="AI-2541")
        digest.generate("t", D, force=True)
        r = self.c.post("/api/reassign", json={
            "update_id": rec["update_id"], "issue_key": "AI-2224", "date": D})
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dashboard"]["date"], D)
        self.assertTrue(payload["dashboard"]["reports"]["dirty"])

    def test_fresh_report_is_not_marked_dirty_by_dashboard_persona_loading(self):
        store.record_progress("t", "AI-2541 完成可靠性复验", date=D)
        digest.generate("t", D, force=True)
        r = self.c.get("/api/dashboard", params={"date": D})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["reports"]["dirty"])

    def test_dashboard_tasks_include_updates_for_global_filters(self):
        store.record_progress(
            "t", "AI-2541 Codex 完成测试", date=D, source_agent="codex",
            ingestion_method="transcript-scan", completion_status="done")
        task = self.c.get("/api/dashboard", params={"date": D}).json()["tasks"]["items"][0]
        self.assertEqual(task["updates"][0]["source_agent"], "codex")
        self.assertEqual(task["updates"][0]["ingestion_method"], "transcript-scan")

    def test_bad_mutation_is_a_clear_client_error_not_a_500(self):
        r = self.c.post("/api/reassign", json={
            "update_id": "missing", "issue_key": "AI-2541", "date": D})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])
        self.assertIn("不存在", r.json()["error"])

    def test_correct_endpoint_edits_content_and_returns_selected_date(self):
        rec = store.record_progress("t", "原正文", date=D, issue="AI-2541")
        r = self.c.post("/api/correct", json={
            "update_id": rec["update_id"], "issue_key": "AI-2224",
            "content": "修正后的正文", "date": D})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["dashboard"]["date"], D)
        row = db.list_updates(date=D, author="t")[0]
        self.assertEqual(row["content_md"], "修正后的正文")
        self.assertEqual(row["assignment_locked"], 1)

    def test_task_mutation_keeps_historical_dashboard_date(self):
        rec = store.record_progress("t", "AI-2541 历史进展", date=D)
        r = self.c.post("/api/tasks/%s/complete" % rec["task"]["task_id"],
                        json={"date": D})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["dashboard"]["date"], D)

    def test_healthz_does_not_leak_author(self):
        r = self.c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})


class TestOAuthRecovery(unittest.TestCase):
    def test_sync_refreshes_and_retries_once_when_server_rejects_access_token(self):
        from fecho import mobius, oauth, service

        success = {"author": "t", "count": 11}
        with mock.patch.object(oauth, "refresh_if_needed", side_effect=[None, "new-token"]) as refresh, \
             mock.patch.object(config, "load", return_value={"mobius_auth": "oauth"}), \
             mock.patch.object(config, "reload_module"), \
             mock.patch.object(mobius, "sync", side_effect=[
                 mobius.MobiusError("Mobius 401: Unauthorized"), success]) as sync:
            self.assertEqual(service.sync_issues(author="t"), success)
        self.assertEqual(refresh.call_args_list, [mock.call(), mock.call(force=True)])
        self.assertEqual(sync.call_count, 2)

    def test_sync_does_not_retry_non_auth_failures(self):
        from fecho import mobius, oauth, service

        with mock.patch.object(oauth, "refresh_if_needed") as refresh, \
             mock.patch.object(mobius, "sync",
                               side_effect=mobius.MobiusError("Mobius 请求失败: timeout")) as sync:
            with self.assertRaisesRegex(mobius.MobiusError, "timeout"):
                service.sync_issues(author="t")
        refresh.assert_called_once_with()
        self.assertEqual(sync.call_count, 1)


class TestAggregation(unittest.TestCase):
    """这一组盯的是最早那版的设计错误：把同一任务的多条进展当重复删掉。"""

    def setUp(self):
        reset()

    def test_similar_progress_on_one_task_is_kept_not_dropped(self):
        a = store.record_progress("t", "AI-2541 接口跑通了", date=D)
        b = store.record_progress("t", "AI-2541 接口又改了下，跑通了", date=D)
        self.assertEqual(a["task"]["task_id"], b["task"]["task_id"])
        ups = db.list_updates(task_id=a["task"]["task_id"])
        self.assertEqual(len(ups), 2, "相似进展必须都保留，只是归到同一个任务下")

    def test_one_task_accumulates_across_sessions_and_agents(self):
        store.record_progress("t", "AI-2541 定了边界", date=D, session_id="s1",
                              source_agent="claude-code")
        store.record_progress("t", "AI-2541 写完 MCP 层", date=D, session_id="s2",
                              source_agent="codex")
        tasks = db.day_tasks("t", D)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0]["updates"]), 2)
        self.assertEqual({u["source_agent"] for u in tasks[0]["updates"]},
                         {"claude-code", "codex"})

    def test_verbatim_resubmit_is_the_only_thing_blocked(self):
        a = store.record_progress("t", "AI-2541 接口跑通了", date=D)
        b = store.record_progress("t", "AI-2541 接口跑通了", date=D)
        self.assertEqual(b["verdict"], "duplicate")
        self.assertEqual(len(db.list_updates(task_id=a["task"]["task_id"])), 1)

    def test_scan_near_duplicates_are_blocked(self):
        """扫描是把同一段对话重新总结，措辞变了信息量没变——重跑时该挡。"""
        store.record_progress("t", "AI-2541 验收脚本扩到 16 步并全过，含真实 LLM 和 collector",
                              date=D, source_agent="scan")
        b = store.record_progress("t", "AI-2541 验收脚本扩展到 16 步全绿，加入 collector 全链路和真实 LLM",
                                  date=D, source_agent="scan")
        self.assertEqual(b["verdict"], "duplicate")

    def test_agent_written_near_duplicates_are_still_kept(self):
        """但 agent 主动记的相似内容可能是真实的不同进展，必须全留——
        这正是最早那版把真进度删掉的错误，不能借去重之名倒回去。"""
        a = store.record_progress("t", "AI-2541 接口跑通了", date=D, source_agent="claude-code")
        b = store.record_progress("t", "AI-2541 接口又改了下，跑通了", date=D,
                                  source_agent="claude-code")
        self.assertNotEqual(b["verdict"], "duplicate")
        self.assertEqual(len(db.list_updates(task_id=a["task"]["task_id"])), 2)

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            store.record_progress("t", "   ", date=D)


class TestLLMRetry(unittest.TestCase):
    def test_truncation_doubles_budget_and_retries(self):
        seen = []

        def once(messages, model, temperature, max_tokens):
            seen.append(max_tokens)
            if max_tokens < 8000:
                raise llm.LLMTruncated("被推理占满")
            return "ok"

        orig, llm._chat_once = llm._chat_once, once
        try:
            self.assertEqual(llm.chat([{"role": "user", "content": "x"}], max_tokens=2000), "ok")
        finally:
            llm._chat_once = orig
        self.assertEqual(seen, [2000, 4000, 8000])

    def test_inline_reasoning_is_stripped(self):
        self.assertEqual(llm.strip_reasoning("<think>数一数\n</think>\n\n正文"), "正文")


class TestDigest(unittest.TestCase):
    def setUp(self):
        reset()

    def test_report_groups_by_task_not_by_record(self):
        store.record_progress("t", "AI-2541 定了边界", date=D)
        store.record_progress("t", "AI-2541 写完 MCP 层", date=D)
        store.record_progress("t", "本地试了下 awesome-gpt-image-2", date=D)
        r = digest.generate("t", D)
        self.assertEqual(r["task_count"], 2)
        self.assertEqual(r["update_count"], 3)

    def test_falls_back_when_llm_unavailable_and_still_writes_files(self):
        store.record_progress("t", "AI-2541 接口跑通了", date=D)
        r = digest.generate("t", D)
        self.assertEqual(r["generator"], "fallback")
        for p in r["files"].values():
            self.assertTrue(os.path.exists(p), p)

    def test_voice_failure_does_not_downgrade_the_daily_report(self):
        store.record_progress("t", "AI-2541 接口跑通了", date=D)
        calls = {"n": 0}

        def flaky(messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return "[1] 接口 | 跑通了\n- 细节一条"
            raise llm.LLMError("模拟口播稿失败")

        orig, llm.chat = llm.chat, flaky
        try:
            r = digest.generate("t", D, force=True)
        finally:
            llm.chat = orig
        self.assertEqual(r["generator"], "llm+fallback")
        self.assertEqual(db.get_report("t", D, "daily")["generator"], "llm")
        self.assertEqual(db.get_report("t", D, "voice")["generator"], "fallback")
        self.assertGreaterEqual(r["voice_chars"], config.VOICE_MIN_CHARS)
        self.assertLessEqual(r["voice_chars"], config.VOICE_MAX_CHARS)

    def test_voice_that_is_short_twice_gets_one_structural_expansion_retry(self):
        store.record_progress("t", "AI-2541 接口跑通并完成验收", date=D,
                              completion_status="done")
        calls = {"n": 0}

        def replies(messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return "[1] done | 接口和验收都已完成\n- done | 核心链路跑通"
            if calls["n"] in (2, 3):
                return "今天把核心接口跑通了。"
            return "今天把核心接口、数据写入和验收链路都完整跑通了。" * 9

        with mock.patch("fecho.llm.chat", side_effect=replies):
            result = digest.generate("t", D, force=True)
        self.assertEqual(calls["n"], 4)
        self.assertGreaterEqual(result["voice_chars"], config.VOICE_MIN_CHARS)

    def test_regeneration_is_skipped_when_inputs_unchanged(self):
        store.record_progress("t", "AI-2541 接口跑通了", date=D)
        digest.generate("t", D)
        self.assertEqual(digest.generate("t", D)["status"], "skipped")
        self.assertEqual(digest.generate("t", D, force=True)["status"], "generated")

    def test_pto_day_without_progress_is_exempt(self):
        self.assertEqual(digest.generate("t", "2030-01-02")["status"], "pto-exempt")

    def test_pto_day_with_progress_is_downgraded_not_exempt(self):
        store.record_progress("t", "请假期间顺手修了个线上问题", date="2030-01-02")
        r = digest.generate("t", "2030-01-02", force=True)
        self.assertEqual(r["status"], "generated")
        self.assertIn("PTO", r["daily_md"])

    def test_empty_day_produces_nothing(self):
        self.assertEqual(digest.generate("t", "2030-01-03")["status"], "empty")

    def test_report_fingerprint_changes_when_an_update_is_revised_in_place(self):
        r = store.record_progress("t", "第一版正文", date=D, issue="AI-2541")
        persona = __import__("fecho.personas", fromlist=["load"]).load("default")
        before = digest.fingerprint(db.day_tasks("t", D), persona, "workday")
        store.correct_progress(r["update_id"], "t", content_md="修正后的正文")
        after = digest.fingerprint(db.day_tasks("t", D), persona, "workday")
        self.assertNotEqual(before, after)

    def test_fallback_does_not_claim_unknown_progress_is_done(self):
        store.record_progress("t", "接口完成一部分，明天继续", date=D, issue="AI-2541")
        result = digest.generate("t", D, force=True)
        self.assertIn("## Updates", result["daily_md"])
        self.assertNotIn("✅ [**", result["daily_md"])


class TestScoping(unittest.TestCase):
    def setUp(self):
        reset()

    def test_author_filter_isolates_each_person(self):
        store.record_progress("t", "我的事", date=D)
        store.record_progress("other", "别人的事", date=D)
        self.assertEqual(len(db.day_tasks("t", D)), 1)
        self.assertEqual(len(db.list_updates(date=D)), 2)


class TestTaskLifecycle(unittest.TestCase):
    def setUp(self):
        reset()

    def test_complete_hides_task_from_my_tasks_and_new_progress_reopens_it(self):
        from fecho import service
        rec = store.record_progress("t", "AI-2541 第一阶段完成", date=D)
        result = service.complete_task(rec["task"]["task_id"], author="t")
        self.assertTrue(result["changed"])
        self.assertEqual(service.open_tasks("t"), [])

        store.record_progress("t", "AI-2541 后续又有新进展", date=D)
        self.assertEqual(len(service.open_tasks("t")), 1)

    def test_reopen_completed_task(self):
        from fecho import service
        rec = store.record_progress("t", "AI-2541 完成", date=D)
        service.complete_task(rec["task"]["task_id"], author="t")
        result = service.reopen_task(rec["task"]["task_id"], author="t")
        self.assertTrue(result["changed"])
        self.assertEqual(db.get_task(rec["task"]["task_id"])["status"], "open")

    def test_merge_moves_updates_and_keeps_source_as_auditable_tombstone(self):
        from fecho import service
        source = store.record_progress("t", "临时自由任务进展", date=D, freeform=True)
        target = store.record_progress("t", "AI-2541 正式任务进展", date=D)
        result = service.merge_tasks(
            source["task"]["task_id"], target["task"]["task_id"], author="t")
        self.assertEqual(result["moved_updates"], 1)
        self.assertEqual(db.get_task(source["task"]["task_id"])["status"], "merged")
        self.assertEqual(len(db.list_updates(task_id=target["task"]["task_id"])), 2)
        with db.cursor() as conn:
            event = conn.execute("SELECT * FROM task_events WHERE event_type='merge'").fetchone()
        self.assertEqual(event["from_task_id"], source["task"]["task_id"])
        self.assertEqual(event["to_task_id"], target["task"]["task_id"])

    def test_lifecycle_tools_are_exposed_by_mcp(self):
        from fecho import mcp_server
        names = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertTrue({"complete_task", "reopen_task", "merge_tasks"} <= names)


class TestDailyFormat(unittest.TestCase):
    """日报格式。链接、短名、状态图标全部由代码渲染——模型复述精确字符串会出错：
    它把 RachelXiaolan 写成过 RachelXiaelan，也把已经改掉的项目名写回去过。"""

    def setUp(self):
        reset()
        from fecho import personas
        self.persona = personas.load("rachel")
        self.persona["task_aliases"] = {"AI-2541": "fecho"}

    def _tasks(self):
        return [
            {"title": "写一个提交工作日志的系统（给agent专用）", "issue_key": "AI-2541",
             "source": "mobius", "task_id": "t1", "updates": [{"content_md": "跑通了"}]},
            {"title": "帮同事查爬虫超时", "issue_key": None,
             "source": "freeform", "task_id": "t2", "updates": [{"content_md": "是 DNS"}]},
        ]

    def test_mobius_task_gets_a_link_freeform_does_not(self):
        items = {1: {"status": "done", "summary": "第一轮本地测试",
                     "bullets": [("done", "mcp 已接入")]},
                 2: {"status": "done", "summary": "定位到 DNS", "bullets": []}}
        md = digest._assemble_daily("2026-09-04", self._tasks(), items, [], self.persona)
        self.assertIn("[**fecho**](https://mobius.feedmob.com/issue/AI-2541)", md)
        self.assertIn("**帮同事查爬虫超时**", md)
        self.assertNotIn("[**帮同事查爬虫超时**](", md, "自由任务不该有链接")

    def test_alias_wins_over_derived_name(self):
        t = self._tasks()[0]
        self.assertEqual(digest.short_name(t, self.persona), "fecho")

    def test_short_name_derived_from_title_when_no_alias(self):
        """没配别名时从标题派生，砍掉括号后缀——不让模型起名（它会把 fecho 写成 fmjot）。"""
        t = dict(self._tasks()[0])
        p = dict(self.persona); p["task_aliases"] = {}
        self.assertEqual(digest.short_name(t, p), "写一个提交工作日志的系统")
        self.assertNotIn("（", digest.short_name(t, p))

    def test_status_icons_render_from_tokens(self):
        items = {1: {"status": "wip", "summary": "在做",
                     "bullets": [("done", "这条做完了"), ("blocked", "这条卡住了")]}}
        md = digest._assemble_daily("2026-09-04", self._tasks()[:1], items, [], self.persona)
        self.assertIn("1. ⭕️ [**fecho**]", md)
        self.assertIn("* ✅ 这条做完了", md)
        self.assertIn("* ❌ 这条卡住了", md)

    def test_missing_status_defaults_to_unknown(self):
        items, _ = digest._parse_daily("[1] 没写状态的总结\n- 也没写状态的子弹点", 1)
        self.assertEqual(items[1]["status"], "unknown")
        self.assertEqual(items[1]["bullets"][0][0], "unknown")

    def test_daily_groups_tasks_into_status_sections(self):
        items = {1: {"status": "done", "summary": "完成", "bullets": []},
                 2: {"status": "blocked", "summary": "受阻", "bullets": []}}
        md = digest._assemble_daily("2026-09-04", self._tasks(), items, [], self.persona)
        self.assertIn("## Done", md)
        self.assertIn("## Blocked", md)

    def test_header_uses_slash_date(self):
        md = digest._assemble_daily("2026-09-04", self._tasks(), {}, [], self.persona)
        self.assertIn("# 2026/09/04 工作日志", md)

    def test_todo_can_reference_a_task_and_inherit_its_link(self):
        md = digest._assemble_daily("2026-09-04", self._tasks(), {},
                                    [(1, "交给 Leo 验收"), (None, "找素材")], self.persona)
        self.assertIn("## To do", md)
        self.assertIn("[**fecho**](https://mobius.feedmob.com/issue/AI-2541)：交给 Leo 验收", md)
        self.assertIn("2. 找素材", md)

    def test_parse_handles_status_and_todo_blocks(self):
        raw = ("[1] done | 第一轮本地测试\n"
               "- done | mcp 已接入 Claude\n"
               "- wip | 准确率待优化\n"
               "[2] blocked | 卡在 path 问题\n"
               "- blocked | 还没装成功\n"
               "TODO\n"
               "- [1] 交给 Leo 验收\n"
               "- 找 yongcheng 要素材\n")
        items, todos = digest._parse_daily(raw, 2)
        self.assertEqual(items[1]["status"], "done")
        self.assertEqual(items[1]["bullets"], [("done", "mcp 已接入 Claude"),
                                               ("wip", "准确率待优化")])
        self.assertEqual(items[2]["status"], "blocked")
        self.assertEqual(todos, [(1, "交给 Leo 验收"), (None, "找 yongcheng 要素材")])

    def test_no_todo_section_when_nothing_pending(self):
        md = digest._assemble_daily("2026-09-04", self._tasks(), {}, [], self.persona)
        self.assertNotIn("## To do", md)


class TestScope(unittest.TestCase):
    """工作范围是隐私边界，不是功能开关。这一组盯的是失败方向：
    没登记过的目录必须被跳过，而不是默认扫进来。"""

    def setUp(self):
        reset()
        from fecho import scope
        scope.config.update(scope={"work_prefixes": [], "work": [], "ignore": []},
                            project_bindings={})
        scope.config.reload_module()

    def test_unregistered_directory_is_not_scanned(self):
        from fecho import scope
        verdict, _ = scope.classify("/somewhere/nobody/registered")
        self.assertEqual(verdict, "unregistered")
        self.assertFalse(scope.is_work("/somewhere/nobody/registered"))

    def test_work_prefix_covers_children(self):
        from fecho import scope
        scope.add("work_prefixes", "/home/me/work")
        scope.config.reload_module()
        self.assertTrue(scope.is_work("/home/me/work/anything/deep"))
        self.assertFalse(scope.is_work("/home/me/personal/thing"))

    def test_ignore_beats_everything(self):
        """显式忽略必须压过工作前缀——私人目录恰好在工作目录底下也要挡住。"""
        from fecho import scope
        scope.add("work_prefixes", "/home/me/work")
        scope.add("ignore", "/home/me/work/side-hustle")
        scope.config.reload_module()
        self.assertFalse(scope.is_work("/home/me/work/side-hustle/repo"))
        self.assertEqual(scope.classify("/home/me/work/side-hustle/repo")[0], "ignored")

    def test_bound_project_counts_as_work_without_prefix(self):
        from fecho import scope
        scope.config.update(project_bindings={"repos/thing": "AI-1"})
        scope.config.reload_module()
        self.assertTrue(scope.is_work("/anywhere/repos/thing"))


class TestProjectBinding(unittest.TestCase):
    """做项目本身时，说的话跟 issue 标题字面零重合，关键词配对必然失效。
    绑定是兜底，但不能压过更具体的证据。"""

    def setUp(self):
        reset()
        from fecho import config as _c
        _c.update(project_bindings={"work/scripe": "AI-2541"})
        _c.reload_module()

    def tearDown(self):
        from fecho import config as _c
        _c.update(project_bindings={})
        _c.reload_module()

    def test_binding_catches_what_keywords_miss(self):
        r = store.record_progress("t", "22 个单测全过，README 重写了", date=D,
                                  project="/home/me/work/scripe")
        self.assertEqual(r["match"]["method"], "project-bound")
        self.assertEqual(r["task"]["issue_key"], "AI-2541")

    def test_explicit_issue_in_text_beats_binding(self):
        r = store.record_progress("t", "顺手把 AI-2460 的申请单填了", date=D,
                                  project="/home/me/work/scripe")
        self.assertEqual(r["task"]["issue_key"], "AI-2460")

    def test_explicit_issue_beats_binding(self):
        """在绑定的仓库里干别的 issue 的活时，正文里写明的 issue 号该赢。"""
        r = store.record_progress("t", "顺手把 AI-2539 那个也试了", date=D,
                                  project="/home/me/work/scripe")
        self.assertEqual(r["match"]["method"], "explicit")
        self.assertEqual(r["task"]["issue_key"], "AI-2539")

    def test_unbound_project_still_records_as_freeform(self):
        """没绑 issue 的工作项目照样记，只是走自由任务——不硬塞给任何 issue。"""
        r = store.record_progress("t", "整理了一版选品汇总表", date=D,
                                  project="/home/me/work/some-research")
        self.assertEqual(r["task"]["source"], "freeform")
        self.assertIsNone(r["task"]["issue_key"])

    def test_explicit_freeform_overrides_project_binding(self):
        r = store.record_progress(
            "t", "帮同事查了一个与本项目无关的问题", date=D,
            project="/home/me/work/scripe", freeform=True)
        self.assertEqual(r["match"]["method"], "explicit-freeform")
        self.assertEqual(r["task"]["source"], "freeform")
        self.assertIsNone(r["task"]["issue_key"])


class TestScanWatermark(unittest.TestCase):
    """水位线。这一组盯的是一个数据丢失级的 bug：
    某组 LLM 失败时，如果水位线照样推过去，那段对话永远不会被重试。"""

    def setUp(self):
        reset()
        from fecho import scan
        self.scan = scan
        with db.cursor() as c:
            c.execute("DELETE FROM scan_marks")

    def test_mark_roundtrip(self):
        self.assertIsNone(self.scan.get_mark("s1"))
        self.scan.set_mark("s1", "2030-01-01T10:00:00Z", 3)
        self.assertEqual(self.scan.get_mark("s1"), "2030-01-01T10:00:00Z")

    def test_mark_advances_and_accumulates_count(self):
        self.scan.set_mark("s1", "2030-01-01T10:00:00Z", 3)
        self.scan.set_mark("s1", "2030-01-01T12:00:00Z", 2)
        self.assertEqual(self.scan.get_mark("s1"), "2030-01-01T12:00:00Z")
        with db.cursor() as c:
            row = c.execute("SELECT entries FROM scan_marks WHERE session_id=?",
                            ("s1",)).fetchone()
        self.assertEqual(row["entries"], 5)

    def test_watermark_stops_before_a_failed_group(self):
        """成功的段保持已处理，失败那段及之后的必须能被重试。"""
        stamps = ["2030-01-01T09:00:00Z", "2030-01-01T10:00:00Z",
                  "2030-01-01T11:00:00Z", "2030-01-01T12:00:00Z"]
        failed_from = "2030-01-01T11:00:00Z"
        usable = [t for t in stamps if t < failed_from]
        self.assertEqual(usable[-1], "2030-01-01T10:00:00Z",
                         "水位线该停在失败组之前那条")

    def test_whole_session_blocked_keeps_mark_untouched(self):
        stamps = ["2030-01-01T11:00:00Z", "2030-01-01T12:00:00Z"]
        failed_from = "2030-01-01T11:00:00Z"
        usable = [t for t in stamps if t < failed_from]
        self.assertEqual(usable, [], "第一组就失败时，水位线一步都不能动")

    def test_parse_survives_chinese_quotes(self):
        """中文引号会把 JSON 打断，所以输出格式是竖线分隔的一行一条。"""
        raw = 'done | 把"一条条记录"改成了"任务+进展"，日报按任务分组\npitfall | 试了 X 不行'
        got = self.scan.parse_entries(raw)
        self.assertEqual(len(got), 2)
        self.assertIn("一条条记录", got[0]["content"])
        self.assertEqual(got[1]["kind"], "pitfall")

    def test_parse_ignores_noise_lines(self):
        raw = "这是模型的开场白\n```\ndone | 真正的一条\n随便一行没有竖线\nbogus | 类型不认识"
        got = self.scan.parse_entries(raw)
        self.assertEqual([e["content"] for e in got], ["真正的一条"])

    def test_boilerplate_is_dropped_before_llm(self):
        """宿主注入的 skill 文档不是用户说的话，实测占一天内容 40%+。"""
        rec = {"message": {"content": [
            {"type": "text", "text": "<command-name>/ego-browser</command-name> 一大段文档"}]}}
        self.assertEqual(self.scan._text_of(rec), "")

    def test_tool_output_is_dropped_before_llm(self):
        rec = {"message": {"content": [
            {"type": "text", "text": "真话"},
            {"type": "tool_result", "content": "几百 KB 的工具输出"}]}}
        self.assertEqual(self.scan._text_of(rec), "真话")


class TestScanIntegrity(unittest.TestCase):
    def setUp(self):
        reset()
        from fecho import scan
        self.scan = scan
        with db.cursor() as c:
            c.execute("DELETE FROM scan_marks")

    @staticmethod
    def _group():
        at = __import__("datetime").datetime.fromisoformat("2030-01-01T10:00:00+00:00")
        rows = [{"at": at, "ts": "2030-01-01T10:00:00Z",
                 "role": "assistant", "text": "完成了实现"}]
        groups = {("codex", "codex:s1", "/work/project", D): rows}
        return groups, {}, {"codex:s1": ["2030-01-01T10:00:00Z"]}

    def test_malformed_model_output_fails_group_and_keeps_watermark(self):
        with mock.patch.object(self.scan, "collect", return_value=self._group()), \
             mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch("fecho.mobius.cached_issues", return_value=ISSUES), \
             mock.patch("fecho.llm.chat", return_value="这是解释，不是约定格式"):
            result = self.scan.scan(author="t")
        self.assertFalse(result["ok"])
        self.assertIn("codex:s1", result["retry_next_time"])
        self.assertIsNone(self.scan.get_mark("codex:s1"))
        with db.cursor() as conn:
            run = conn.execute("SELECT status FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "failed")

    def test_explicit_none_is_a_valid_zero_result_and_advances_watermark(self):
        with mock.patch.object(self.scan, "collect", return_value=self._group()), \
             mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch("fecho.mobius.cached_issues", return_value=ISSUES), \
             mock.patch("fecho.llm.chat", return_value="NONE"):
            result = self.scan.scan(author="t")
        self.assertTrue(result["ok"])
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.scan.get_mark("codex:s1"), "2030-01-01T10:00:00Z")

    def test_source_event_key_makes_scan_replay_idempotent(self):
        a = store.record_progress(
            "t", "第一种模型措辞", date=D, source_agent="codex",
            issue="AI-2541", source_event_key="codex:s1:chunk-1:item-1")
        b = store.record_progress(
            "t", "重跑后模型换了一种措辞", date=D, source_agent="codex",
            issue="AI-2541", source_event_key="codex:s1:chunk-1:item-1")
        self.assertEqual(b["verdict"], "duplicate")
        self.assertEqual(b["update_id"], a["update_id"])
        self.assertEqual(len(db.list_updates(date=D, author="t")), 1)

    def test_scan_preserves_producer_separately_from_ingestion_method(self):
        a = store.record_progress(
            "t", "Codex 扫描抽出的进展", date=D, source_agent="codex",
            ingestion_method="transcript-scan", issue="AI-2541")
        row = db.list_updates(task_id=a["task"]["task_id"])[0]
        self.assertEqual(row["source_agent"], "codex")
        self.assertEqual(row["ingestion_method"], "transcript-scan")

    def test_discovers_configured_claude_codex_and_hermes_adapters(self):
        root = Path(tempfile.mkdtemp(prefix="fecho-adapters-"))
        self.addCleanup(shutil.rmtree, root, True)
        paths = {}
        for name in ("claude-code", "codex", "hermes"):
            p = root / (name + ".jsonl")
            p.write_text("{}\n", encoding="utf-8")
            paths[name] = [str(p)]
        with mock.patch.object(config, "SCAN_SOURCES", paths):
            found = self.scan.discover_transcripts()
        self.assertEqual({x["producer_agent"] for x in found}, set(paths))


class TestCollector(unittest.TestCase):
    """team_reports 是 collector 的全部——没有 entries/tasks 表，隐私边界是结构性的。
    这里盯的是唯一真正重要的安全属性：author 只能从 token 反查，body 里传什么都不算。"""

    def setUp(self):
        import importlib

        from fastapi.testclient import TestClient

        with open(os.environ["FECHO_TOKENS"], "w") as f:
            json.dump({
                "tok-a": {"author": "alice", "display_name": "Alice"},
                "tok-b": {"author": "bob", "display_name": "Bob"},
            }, f)

        from fecho import collector as collector_mod

        importlib.reload(collector_mod)
        self.collector = collector_mod
        collector_mod._db_path().unlink(missing_ok=True)
        collector_mod.init()
        self.client = TestClient(collector_mod.app)

    def _push(self, token, **body):
        return self.client.post("/reports", json=body, headers={"Authorization": "Bearer " + token})

    def test_push_without_token_is_rejected(self):
        r = self.client.post("/reports", json={"date": D, "daily_md": "x"})
        self.assertEqual(r.status_code, 401)

    def test_push_with_bad_token_is_rejected(self):
        r = self._push("not-a-real-token", date=D, daily_md="x")
        self.assertEqual(r.status_code, 401)

    def test_author_comes_from_token_never_from_body(self):
        """body 里塞 'author': 'bob' 也不该生效——ReportPush 模型压根不接受这个字段。"""
        r = self._push("tok-a", date=D, daily_md="冒充 bob 的内容", author="bob")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["author"], "alice")
        got = self.client.get("/reports", params={"date": D, "author": "bob"},
                              headers={"Authorization": "Bearer tok-b"}).json()
        self.assertEqual(got["count"], 0, "bob 名下不该出现 alice 用 bob 的 token 都没用过就写进去的记录")

    def test_repush_overwrites_only_own_slot(self):
        self._push("tok-a", date=D, daily_md="alice 第一版")
        self._push("tok-a", date=D, daily_md="alice 第二版")
        self._push("tok-b", date=D, daily_md="bob 的日报")
        team = self.client.get("/reports", params={"date": D},
                               headers={"Authorization": "Bearer tok-a"}).json()
        by_author = {a["author"]: a for a in team["authors"]}
        self.assertEqual(team["count"], 2)
        self.assertEqual(by_author["alice"]["daily"]["content_md"], "alice 第二版")
        self.assertEqual(by_author["bob"]["daily"]["content_md"], "bob 的日报")

    def test_empty_push_is_rejected(self):
        r = self._push("tok-a", date=D)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
