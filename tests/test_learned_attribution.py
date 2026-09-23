"""归属越用越准：从人的纠正里学（C），同一场对话跟着已确认的 issue 走（D）。

真实案例：Bug Hunter 的进展其实都属于 AI-2660，但它的标题是「9.18 RSI 带练项目
自进化检查节点」，一个字没提 Bug Hunter。同一场对话前半截被交叉验证判到了 AI-2660，
后半截却一条条落成了自由任务。
"""
import json
import unittest
from unittest import mock

import _env  # noqa: F401  必须在 import fecho 之前
from test_core import D, mock_llm, reset

from fecho import db, digest, mobius, service, store


def scan(content, session="codex:s1"):
    return store.record_progress("t", content, date=D, session_id=session,
                                 source_agent="codex", ingestion_method="transcript-scan")


class TestConversationFollowsConfirmedIssue(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)   # 合并会留下 task_events，别的测试文件清表时不认它

    def _confirm(self, rec, issue_key):
        """模拟出日报前的交叉验证把这条判到某个 issue。"""
        self.assertTrue(store.reassign(rec["update_id"], "t", issue_key))

    def test_later_items_follow_an_issue_verified_earlier_in_the_conversation(self):
        first = scan("Bug Hunter 第一阶段框架和页面已准备好")
        self._confirm(first, "AI-2460")
        later = scan("Bug Hunter Worker 部署到 Cloudflare")
        self.assertEqual(later["match"]["method"], "session-issue")
        self.assertEqual(later["task"]["issue_key"], "AI-2460")

    def test_human_confirmed_attribution_is_followed_too(self):
        first = scan("Bug Hunter 第一阶段框架")
        service.attach_to_issue(first["task"]["task_id"], "AI-2460", author="t")
        later = scan("Bug Hunter 第二阶段")
        self.assertEqual(later["task"]["issue_key"], "AI-2460")

    def test_a_mere_mention_of_an_issue_is_not_followed(self):
        """正文顺嘴提到 issue 号不算确认：同场后面不相干的不能整片跟进去。"""
        scan("开始做 AI-2541 的工作日志系统")
        later = scan("闲鱼那边抓了十六个商品的详情")
        self.assertIsNone(later["task"].get("issue_key"))

    def test_other_conversations_and_other_days_are_not_affected(self):
        first = scan("Bug Hunter 第一阶段")
        self._confirm(first, "AI-2460")
        other = scan("完全另一场对话里的事", session="codex:s2")
        self.assertIsNone(other["task"].get("issue_key"))
        next_day = store.record_progress("t", "第二天同一场对话", date="2030-01-02",
                                         session_id="codex:s1", source_agent="codex",
                                         ingestion_method="transcript-scan")
        self.assertIsNone(next_day["task"].get("issue_key"))

    def test_followed_items_are_still_rechecked_at_report_time(self):
        first = scan("Bug Hunter 第一阶段")
        self._confirm(first, "AI-2460")
        later = scan("顺手回了一封无关的邮件")
        self.assertEqual(later["task"]["issue_key"], "AI-2460")
        row = [r for r in db.day_updates("t", D) if r["update_id"] == later["update_id"]][0]
        self.assertEqual(row["assignment_locked"], 0, "跟过去的不锁，出日报前的重判能改")


class TestLearnedHints(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)   # 合并会留下 task_events，别的测试文件清表时不认它

    def test_human_merge_teaches_the_issue_a_new_name(self):
        free = store.record_progress("t", "项目方向定为 Bug Hunter 自进化捉虫器", date=D, freeform=True)
        service.attach_to_issue(free["task"]["task_id"], "AI-2460", author="t")
        hints = digest.issue_hints("t", ["AI-2460", "AI-2541"])
        self.assertIn("项目方向定为 Bug Hunter 自进化捉虫器", hints["AI-2460"]["names"])
        self.assertTrue(hints["AI-2460"]["examples"], "人确认过的进展要当例子")
        self.assertEqual(hints["AI-2541"], {"names": [], "examples": []})

    def test_verification_prompt_carries_names_description_and_examples(self):
        free = store.record_progress("t", "Bug Hunter 框架部署好了", date=D, freeform=True)
        service.attach_to_issue(free["task"]["task_id"], "AI-2460", author="t")
        with db.cursor() as c:
            c.execute("UPDATE mobius_issues SET raw=? WHERE author='t' AND issue_key='AI-2460'",
                      (json.dumps({"_desc": "每周检查一次 RSI 自进化项目", "_bucket": "started"},
                                  ensure_ascii=False),))
        store.record_progress("t", "今天又修了捉虫器的回归测试", date=D, freeform=True)
        seen = {}

        def capture(messages, **kw):
            seen["prompt"] = messages[0]["content"]
            return "1 | -"

        with mock_llm(""), mock.patch("fecho.llm.chat", side_effect=capture):
            digest.verify_assignments("t", D)
        prompt = seen["prompt"]
        self.assertIn("也叫：Bug Hunter 框架部署好了", prompt)
        self.assertIn("描述：每周检查一次 RSI 自进化项目", prompt)
        self.assertIn("人确认过属于它的进展：「Bug Hunter 框架部署好了」", prompt)


class TestDescriptionSnippet(unittest.TestCase):
    def test_snippet_keeps_the_prose_and_drops_markup(self):
        desc = ("<!-- ai-context:start -->\n## AI 背景补全\n\n### 背景\n"
                "周一 AI 会议 Ken 在讨论 **Orca** 时提出：要学习这家 4 人团队。\n"
                "<!-- ai-context:end -->")
        self.assertEqual(mobius.snippet(desc), "周一 AI 会议 Ken 在讨论 Orca 时提出：要学习这家 4 人团队。")
        self.assertLessEqual(len(mobius.snippet("字" * 500)), mobius.DESC_CHARS)
        self.assertEqual(mobius.snippet(None), "")

    def test_sync_reuses_a_description_when_the_issue_did_not_change(self):
        reset()
        self.addCleanup(reset)
        calls = []

        def fake(method, params=None, token=None):
            name, args = params["name"], params["arguments"]
            calls.append(name)
            if name == "list_issues":
                items = ([{"identifier": "AI-2", "title": "票", "state": "In Progress",
                           "updatedAt": "2030-01-01T00:00:00Z"}]
                         if args["stateType"] == "started" else [])
                return {"content": [{"text": json.dumps({"issues": items, "hasMore": False})}]}
            return {"content": [{"text": json.dumps(
                {"issue": {"identifier": "AI-2", "description": "票的描述"}}, ensure_ascii=False)}]}

        with mock.patch.object(mobius, "_rpc", side_effect=fake):
            mobius.sync("t", assignee="t@x.com", token="tok")
            first = calls.count("get_issue")
            mobius.sync("t", assignee="t@x.com", token="tok")
        self.assertEqual(first, 1)
        self.assertEqual(calls.count("get_issue"), 1, "issue 没改过，第二轮不再查描述")
        self.assertEqual(mobius.cached_issues("t")[0]["desc"], "票的描述")
