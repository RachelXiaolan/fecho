"""任务别拆碎：扫描按对话归到一起、重判不再积空壳、存量能收拾。

真实事故：Rachel 名下 241 个任务，237 个是自由任务，其中 142 个一条进展都不挂。
- 一场 Codex 对话被每晚扫描拆成 21 个任务（扫描关掉了同会话接续，没写 issue 号的每条都新建）
- 出日报前的重判会来回翻（这条算不算 AI-2541？），每次挪回自由任务都新建一个，原来那个留成空壳
"""
import _env  # noqa: F401  必须在 import fecho 之前
import unittest
from argparse import Namespace

from fecho import cli, db, store, web  # noqa: E402

D = "2030-01-01"
AUTHOR = "t"
ISSUES = [
    {"issue_key": "AI-2541", "title": "写一个提交工作日志的系统（给agent专用）"},
    {"issue_key": "AI-2224", "title": "与小Lu商量，有关Linux.do积分的事情"},
]


def reset():
    db.require_disposable()   # 清表前确认连的是临时库
    db.init()
    with db.cursor() as c:
        for table in ("task_events", "assignment_events", "updates", "tasks", "reports",
                      "scan_runs", "assignment_verifications", "task_aliases"):
            c.execute("DELETE FROM %s" % table)
        c.execute("DELETE FROM mobius_issues WHERE author=?", (AUTHOR,))
        for i in ISSUES:
            c.execute("INSERT INTO mobius_issues (issue_key,author,title,state,url,"
                      "updated_at,synced_at,raw) VALUES (?,?,?,?,?,?,?,?)",
                      (i["issue_key"], AUTHOR, i["title"], "In Progress", "", "",
                       store.now_iso(), "{}"))


def scan(content, session="codex:a", date=D, **kw):
    return store.record_progress(AUTHOR, content, date=date, session_id=session,
                                 source_agent="codex", ingestion_method="transcript-scan", **kw)


def freeform_tasks(status=None):
    sql = "SELECT * FROM tasks WHERE author=? AND issue_key IS NULL"
    params = [AUTHOR]
    if status:
        sql += " AND status=?"
        params.append(status)
    with db.cursor() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


class TestScanGroupsByConversation(unittest.TestCase):
    def setUp(self):
        reset()

    def test_same_conversation_without_issue_lands_in_one_task(self):
        """一场对话里没写 issue 号的几条，归进同一个自由任务，不再每条新建。"""
        a = scan("确认 Hermes 以 Docker 运行")
        b = scan("建了 freellmapi-net 网络，两个容器接进去")
        c = scan("Hermes 接入 FreeLLMAPI，默认模型设为 auto")
        self.assertEqual(a["match"]["method"], "new-task")
        self.assertEqual(b["match"]["method"], "session-group")
        self.assertEqual({a["task"]["task_id"], b["task"]["task_id"], c["task"]["task_id"]},
                         {a["task"]["task_id"]})
        self.assertEqual(len(freeform_tasks()), 1)

    def test_different_conversations_stay_apart(self):
        a = scan("Hermes 接好了", session="codex:a")
        b = scan("擂台赛复盘页发布了", session="codex:b")
        self.assertNotEqual(a["task"]["task_id"], b["task"]["task_id"])

    def test_same_conversation_on_another_day_stays_apart(self):
        """按「同一天 + 同一场对话」归：隔天在同一个会话里做的，算另一件事。"""
        a = scan("Hermes 接好了", date=D)
        b = scan("Hermes 搜索还不能用", date="2030-01-02")
        self.assertNotEqual(a["task"]["task_id"], b["task"]["task_id"])

    def test_explicit_issue_still_wins_over_the_conversation(self):
        """只并自由任务，从不往 issue 上塞；写了 issue 号的照样进 issue。"""
        a = scan("Hermes 接好了")
        b = scan("AI-2541 的日报重试额度修好了")
        self.assertEqual(b["task"]["issue_key"], "AI-2541")
        self.assertNotEqual(a["task"]["task_id"], b["task"]["task_id"])

    def test_interactive_logging_is_unchanged(self):
        """随手记不是扫描，照旧走会话接续。"""
        a = store.record_progress(AUTHOR, "开始做 AI-2541", date=D, session_id="s1",
                                  source_agent="claude-code")
        b = store.record_progress(AUTHOR, "顺手弄了一下", date=D, session_id="s1",
                                  source_agent="claude-code")
        self.assertEqual(b["match"]["method"], "task-continue")
        self.assertEqual(a["task"]["task_id"], b["task"]["task_id"])


class TestReassignDoesNotLeaveShells(unittest.TestCase):
    def setUp(self):
        reset()

    def test_flip_flop_returns_to_the_same_freeform_task(self):
        """重判来回翻：挪去 issue 再挪回来，回到原来那个自由任务，不新建空壳。"""
        r = scan("Grok 适配要不要算进 Fecho")
        original = r["task"]["task_id"]

        self.assertTrue(store.reassign(r["update_id"], AUTHOR, "AI-2541"))
        self.assertEqual(db.get_task(original)["status"], "empty", "挪空的自由任务要收起来")

        self.assertTrue(store.reassign(r["update_id"], AUTHOR, None))
        with db.cursor() as c:
            now = c.execute("SELECT task_id FROM updates WHERE update_id=?",
                            (r["update_id"],)).fetchone()["task_id"]
        self.assertEqual(now, original)
        self.assertEqual(db.get_task(original)["status"], "open", "挪回来要重新打开")
        self.assertEqual(len(freeform_tasks()), 1, "来回翻一轮不该多出任何任务")

    def test_back_to_freeform_joins_its_conversation(self):
        """同一场对话里还有别的条目时，挪回来直接并进那个任务。"""
        a = scan("Hermes 接好了")
        b = scan("Hermes 搜索后端设成了 ddgs")
        store.reassign(b["update_id"], AUTHOR, "AI-2541")
        store.reassign(b["update_id"], AUTHOR, None)
        with db.cursor() as c:
            now = c.execute("SELECT task_id FROM updates WHERE update_id=?",
                            (b["update_id"],)).fetchone()["task_id"]
        self.assertEqual(now, a["task"]["task_id"])

    def test_emptied_task_is_hidden_from_the_task_list(self):
        # 正文里不能带 issue 号，否则一开始就直接进 issue 任务，根本不经过自由任务
        r = scan("这条后来被判去 Fecho")
        self.assertIsNone(r["task"]["issue_key"])
        store.reassign(r["update_id"], AUTHOR, "AI-2541")
        listed = {t["task_id"] for t in web.task_list(AUTHOR)}
        self.assertNotIn(r["task"]["task_id"], listed)

    def test_issue_tasks_are_never_collapsed(self):
        """Mobius 任务对应真实的 issue，空着也该在列表里。"""
        r = scan("AI-2541 先记一条")
        store.reassign(r["update_id"], AUTHOR, None)
        issue_task = db.get_task(r["task"]["task_id"])
        self.assertEqual(issue_task["status"], "open")


class TestSystemMergeIsNotLocked(unittest.TestCase):
    def setUp(self):
        reset()

    def test_system_merge_keeps_updates_rejudgeable(self):
        a = store.record_progress(AUTHOR, "碎片一", date=D, freeform=True)
        b = store.record_progress(AUTHOR, "碎片二", date=D, freeform=True)
        store.merge_tasks(b["task"]["task_id"], a["task"]["task_id"], AUTHOR,
                          actor="system", lock=False)
        with db.cursor() as c:
            row = dict(c.execute("SELECT match_method, assignment_locked FROM updates"
                                 " WHERE update_id=?", (b["update_id"],)).fetchone())
        self.assertEqual(row, {"match_method": "system-merged", "assignment_locked": 0})

    def test_human_merge_still_locks(self):
        a = store.record_progress(AUTHOR, "碎片一", date=D, freeform=True)
        b = store.record_progress(AUTHOR, "碎片二", date=D, freeform=True)
        store.merge_tasks(b["task"]["task_id"], a["task"]["task_id"], AUTHOR)
        with db.cursor() as c:
            locked = c.execute("SELECT assignment_locked FROM updates WHERE update_id=?",
                               (b["update_id"],)).fetchone()["assignment_locked"]
        self.assertEqual(locked, 1)


class TestTidyTasks(unittest.TestCase):
    """收拾存量：先演练，确认了再 --apply。"""

    def setUp(self):
        reset()
        # 按老办法造出碎片：同一天同一场对话，每条一个自由任务
        self.frags = [store.record_progress(AUTHOR, "碎片 %d" % i, date=D, session_id="codex:old",
                                            source_agent="codex", ingestion_method="transcript-scan",
                                            freeform=True)
                      for i in range(3)]
        self.shell = store._create_freeform_task(AUTHOR, "一条进展都没有的空壳")
        with db.cursor() as c:
            for f in self.frags:
                c.execute("INSERT INTO task_aliases (author, task_key, alias, created_at)"
                          " VALUES (?,?,?,?)",
                          (AUTHOR, "task:%s" % f["task"]["task_id"], "旧短名", store.now_iso()))

    def test_dry_run_changes_nothing(self):
        cli.cmd_tidy_tasks(Namespace(author=AUTHOR, apply=False))
        self.assertEqual(len(freeform_tasks("open")), 4)

    def test_apply_merges_fragments_and_collapses_shells(self):
        cli.cmd_tidy_tasks(Namespace(author=AUTHOR, apply=True))
        open_tasks = freeform_tasks("open")
        self.assertEqual(len(open_tasks), 1, "三个碎片合成一个，空壳收起")
        kept = open_tasks[0]["task_id"]
        with db.cursor() as c:
            n = c.execute("SELECT COUNT(*) n FROM updates WHERE task_id=? AND status='active'",
                          (kept,)).fetchone()["n"]
            locked = c.execute("SELECT MAX(assignment_locked) m FROM updates WHERE task_id=?",
                               (kept,)).fetchone()["m"]
            alias = c.execute("SELECT alias FROM task_aliases WHERE author=? AND task_key=?",
                              (AUTHOR, "task:%s" % kept)).fetchone()
        self.assertEqual(n, 3)
        self.assertEqual(locked, 0, "系统合并不上锁，重判还能纠正")
        self.assertIsNone(alias, "合并后清掉旧短名，下次出日报时按整组重新起")
        self.assertEqual(db.get_task(self.shell["task_id"])["status"], "empty")
        self.assertEqual(len(freeform_tasks("merged")), 2)


class TestAliasesShowOnTheWeb(unittest.TestCase):
    def setUp(self):
        reset()

    def test_task_list_carries_the_short_name(self):
        r = scan("已将 SSH 远程机器连接到 ChatGPT")
        with db.cursor() as c:
            c.execute("INSERT INTO task_aliases (author, task_key, alias, created_at)"
                      " VALUES (?,?,?,?)",
                      (AUTHOR, "task:%s" % r["task"]["task_id"], "Hermes 调试", store.now_iso()))
        item = next(t for t in web.task_list(AUTHOR) if t["task_id"] == r["task"]["task_id"])
        self.assertEqual(item["alias"], "Hermes 调试")


if __name__ == "__main__":
    unittest.main()
