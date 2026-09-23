"""同步「参与中」的 issue，带上优先级；日报按优先级排。

以前只同步「指派给我、进行中或待开始」的：backlog 里的、别人名下我在帮着做的、
没人认领我先做了的、刚关掉还在收尾的，一概不认识，进展归不上去。
"""
import json
import unittest
from unittest import mock

import _env  # noqa: F401  必须在 import fecho 之前
from test_core import D, mock_llm, reset

from fecho import db, digest, mobius, store


def issue(key, title, state, priority=0, updated="2030-01-01T00:00:00Z"):
    return {"identifier": key, "title": title, "state": state, "priority": priority,
            "updatedAt": updated, "url": "/issue/" + key}


class FakeMobius:
    """按参数回 list_issues / get_issue，记下每次调用。"""

    def __init__(self, lists, singles):
        self.lists, self.singles, self.calls = lists, singles, []

    def __call__(self, method, params=None, token=None):
        name, args = params["name"], params["arguments"]
        self.calls.append((name, args))
        if name == "list_issues":
            items = self.lists.get(args["stateType"], [])
            body = {"issues": items, "hasMore": False}
        else:
            if args["identifier"] not in self.singles:
                raise mobius.MobiusError("not found")
            body = {"issue": self.singles[args["identifier"]]}
        return {"content": [{"text": json.dumps(body, ensure_ascii=False)}]}


class TestSyncScope(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)   # 合并会留下 task_events，别的测试文件清表时不认它

    def _sync(self, fake):
        with mock.patch.object(mobius, "_rpc", side_effect=fake):
            return mobius.sync("t", assignee="t@x.com", token="tok")

    def test_backlog_and_recently_closed_are_synced_with_priority(self):
        fake = FakeMobius({
            "backlog": [issue("AI-1", "排进 backlog 的票", "Backlog", 3)],
            "started": [issue("AI-2", "进行中的票", "In Progress", 1)],
            "completed": [issue("AI-3", "前天刚做完", "Done", 2)],
        }, {})
        self._sync(fake)
        got = {i["issue_key"]: i for i in mobius.cached_issues("t")}
        self.assertEqual(set(got), {"AI-1", "AI-2", "AI-3"})
        self.assertEqual(got["AI-2"]["priority"], 1)
        self.assertTrue(got["AI-3"]["closed"])
        closed_query = [a for n, a in fake.calls if n == "list_issues" and a["stateType"] == "completed"]
        self.assertIn("updatedAfter", closed_query[0], "关掉的只要最近 7 天的")
        self.assertEqual([i["issue_key"] for i in mobius.cached_issues("t")], ["AI-2", "AI-1", "AI-3"],
                         "开着的在前、优先级高的在前，关掉的最后")

    def test_issues_i_worked_on_are_synced_even_if_not_assigned_to_me(self):
        """别人名下我在帮着做的、没人认领我先做了的：Fecho 里归过进展就算参与中。"""
        for key in ("AI-2558", "AI-2677", "AI-100"):   # 人手动归过、或扫描认出来过
            store.task_for_issue("t", key, key)
        with db.cursor() as c:           # 让这几个任务算「最近动过」
            c.execute("UPDATE tasks SET last_update=? WHERE author='t'", (store.now_iso(),))
        fake = FakeMobius({"started": [issue("AI-2", "我的票", "In Progress", 2)]}, {
            "AI-2558": issue("AI-2558", "Leo 的票", "In Progress", 2),
            "AI-2677": issue("AI-2677", "没人认领", "Todo", 0),
            "AI-100": issue("AI-100", "早就关了", "Done", 0, updated="2000-01-01T00:00:00Z"),
        })
        result = self._sync(fake)
        keys = {i["issue_key"]: i for i in mobius.cached_issues("t")}
        self.assertIn("AI-2558", keys)
        self.assertIn("AI-2677", keys)
        self.assertEqual(keys["AI-2677"]["via"], "touched")
        self.assertNotIn("AI-100", keys, "关了超过 7 天的不要")
        self.assertEqual(result["touched"], 2)

    def test_an_issue_mobius_cannot_return_does_not_break_the_sync(self):
        store.task_for_issue("t", "AI-404", "已删除的票")
        with db.cursor() as c:
            c.execute("UPDATE tasks SET last_update=? WHERE author='t'", (store.now_iso(),))
        fake = FakeMobius({"started": [issue("AI-2", "我的票", "In Progress")]}, {})
        self._sync(fake)
        self.assertEqual([i["issue_key"] for i in mobius.cached_issues("t")], ["AI-2"])


class TestDailyOrder(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)   # 合并会留下 task_events，别的测试文件清表时不认它
        with db.cursor() as c:
            for key, prio in (("AI-2541", 4), ("AI-2460", 1), ("AI-2539", 0)):
                c.execute("UPDATE mobius_issues SET raw=? WHERE author='t' AND issue_key=?",
                          (json.dumps({"priority": prio, "_bucket": "started"}), key))

    def test_tasks_are_ordered_by_priority_then_freeform_last(self):
        store.record_progress("t", "自由任务一条", date=D, freeform=True)
        store.record_progress("t", "AI-2541 低优先级的活", date=D)
        store.record_progress("t", "AI-2539 没设优先级", date=D)
        store.record_progress("t", "AI-2460 紧急的活", date=D)
        ordered = digest.by_priority("t", db.day_tasks("t", D))
        self.assertEqual([t.get("issue_key") for t in ordered], ["AI-2460", "AI-2541", "AI-2539", None])

    def test_reordering_does_not_mark_existing_reports_stale(self):
        """排序只改呈现：指纹照旧按原顺序算，旧日报不会全被判成「需要重新生成」。"""
        from fecho import web
        store.record_progress("t", "AI-2541 低优先级的活", date=D)
        store.record_progress("t", "AI-2460 紧急的活", date=D)
        reply = "[1] done | 一\n[2] done | 二\n"
        with mock_llm(reply), mock.patch.object(digest, "verify_assignments",
                                                return_value={"changed": [], "error": None}):
            digest.generate("t", D, force=True)
        self.assertFalse(web.report_payload("t", D)["dirty"])
