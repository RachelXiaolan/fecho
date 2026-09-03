"""核心逻辑单测：配对、归并、整理、降级。

不依赖 pytest：python3 tests/test_core.py
不碰生产库、不联网：全部指向临时目录，Mobius issue 用假缓存。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

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
from fecho import db, digest, llm, match, store  # noqa: E402

D = "2030-01-01"
ISSUES = [
    {"issue_key": "AI-2541", "title": "写一个提交工作日志的系统（给agent专用）"},
    {"issue_key": "AI-2539", "title": "本地试用 awesome-gpt-image-2 并评估复用价值"},
    {"issue_key": "AI-2460", "title": "申请新lu3服务器 8G 部署产品级的QM"},
]


def seed_issues(author="t"):
    with db.cursor() as c:
        c.execute("DELETE FROM mobius_issues WHERE author=?", (author,))
        for i in ISSUES:
            c.execute("INSERT INTO mobius_issues (issue_key,author,title,state,url,"
                      "updated_at,synced_at,raw) VALUES (?,?,?,?,?,?,?,?)",
                      (i["issue_key"], author, i["title"], "In Progress", "", "", "now", "{}"))


def reset():
    db.init()
    with db.cursor() as c:
        c.execute("DELETE FROM updates")
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM reports")
    seed_issues()


class TestMatching(unittest.TestCase):
    def setUp(self):
        reset()

    def test_explicit_issue_key_in_text_wins(self):
        r = store.record_progress("t", "顺手把 AI-2541 的配对引擎写了", date=D)
        self.assertEqual(r["match"]["method"], "explicit")
        self.assertEqual(r["task"]["issue_key"], "AI-2541")

    def test_content_is_matched_to_a_mobius_issue_without_naming_it(self):
        r = store.record_progress("t", "本地试了下 awesome-gpt-image-2，出图质量一般", date=D)
        self.assertEqual(r["match"]["method"], "mobius-auto")
        self.assertEqual(r["task"]["issue_key"], "AI-2539")

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

    def test_strong_signal_beats_session_inertia(self):
        """会话惯性只是默认值：正文里有明确证据时必须让位。"""
        s = "sess-2"
        store.record_progress("t", "开始做 AI-2541 的工作日志系统", date=D, session_id=s)
        b = store.record_progress("t", "本地试了下 awesome-gpt-image-2，出图一般",
                                  date=D, session_id=s)
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
                return "# 日报\n\n- AI-2541 接口跑通了"
            raise llm.LLMError("模拟口播稿失败")

        orig, llm.chat = llm.chat, flaky
        try:
            r = digest.generate("t", D, force=True)
        finally:
            llm.chat = orig
        self.assertEqual(r["generator"], "llm+fallback")
        self.assertEqual(db.get_report("t", D, "daily")["generator"], "llm")
        self.assertEqual(db.get_report("t", D, "voice")["generator"], "fallback")

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


class TestScoping(unittest.TestCase):
    def setUp(self):
        reset()

    def test_author_filter_isolates_each_person(self):
        store.record_progress("t", "我的事", date=D)
        store.record_progress("other", "别人的事", date=D)
        self.assertEqual(len(db.day_tasks("t", D)), 1)
        self.assertEqual(len(db.list_updates(date=D)), 2)


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
        unittest.main(verbosity=2, exit=False)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
