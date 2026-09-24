"""日报分批写：任务多的日子一次调用写不完。

真实事故：9/23 Rachel 11 个任务 / 155 条进展，一次调用写整天，推理模型 + 非流式，
180 秒超时，整篇掉成兜底稿。现在按任务切几批分别写，某一批失败只让那一批降级。
"""
import re
import unittest
from unittest import mock

import _env  # noqa: F401  必须在 import fecho 之前
from test_core import D, reset

from fecho import config, db, digest, llm, store

# 9/23 那天各任务的进展条数
REAL_DAY = [54, 26, 3, 51, 8, 5, 3, 2, 1, 1, 1]


def fake_tasks(counts):
    return [{"task_id": str(i), "updates": [{}] * n} for i, n in enumerate(counts)]


class FakeModel:
    """按提示词里的任务数回话；正文含 fail_on 的那一批抛超时。记下每次调用。"""

    def __init__(self, fail_on=()):
        self.fail_on, self.calls = fail_on, []

    def __call__(self, messages, **kw):
        user = messages[-1]["content"]
        self.calls.append(user)
        if any(marker in user for marker in self.fail_on):
            raise llm.LLMError("LLM 请求失败: timed out")
        m = re.search(r"今天推进了 (\d+) 个任务", user)
        if not m:                                     # 口播稿：给一段长度合适的话
            return "今天主要推进了几件事，进展都写在日报里了。" * 12
        n = int(m.group(1))
        firsts = re.findall(r"\[(\d+)\]\n  - \[[^\]]*\] (\S+)", user)
        lines = []
        for k, first in firsts:
            lines += ["[%s] done | 整理过的：%s" % (k, first), "- done | 细节"]
        lines += ["TODO", "- [1] 跟进：%s" % firsts[0][1], "- 与任务无关的待办"]
        assert len(firsts) == n
        return "\n".join(lines)


class TestBatches(unittest.TestCase):
    def test_the_real_heavy_day_is_split_small_enough(self):
        batches = digest._batches(fake_tasks(REAL_DAY))
        self.assertEqual(sum(len(b) for b in batches), len(REAL_DAY), "一个任务都不能丢")
        self.assertEqual([i for b in batches for i in b], list(range(1, len(REAL_DAY) + 1)), "顺序不变")
        for b in batches:
            self.assertLessEqual(len(b), digest.BATCH_TASKS)
            n = sum(REAL_DAY[i - 1] for i in b)
            self.assertTrue(n <= digest.BATCH_UPDATES or len(b) == 1,
                            "超过进展上限的只能是单个大任务自己一批")

    def test_a_light_day_is_still_one_call(self):
        self.assertEqual(digest._batches(fake_tasks([3, 2, 1])), [[1, 2, 3]])


class TestBatchedGenerate(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)
        # 7 个任务，每个 12 条进展：按 40 条一批切成 3、3、1
        for k in range(7):
            first = store.record_progress("t", "任务%d 的第0条进展" % k, date=D, freeform=True)
            for j in range(1, 12):
                store.record_progress("t", "任务%d 的第%d条进展" % (k, j), date=D,
                                      task_id=first["task"]["task_id"])
        self.tasks = db.day_tasks("t", D)
        self.assertEqual(len(self.tasks), 7)
        self._orig = config.llm_configured
        config.llm_configured = lambda: True
        self.addCleanup(lambda: setattr(config, "llm_configured", self._orig))

    def _generate(self, model):
        with mock.patch.object(llm, "chat", side_effect=model), \
                mock.patch.object(digest, "verify_assignments", return_value={"changed": [], "error": None}), \
                mock.patch.object(digest, "task_aliases", return_value={}):
            return digest.generate("t", D, force=True)

    def test_every_task_is_written_once_in_order_across_batches(self):
        model = FakeModel()
        res = self._generate(model)
        daily_calls = [c for c in model.calls if "今天推进了" in c]
        self.assertEqual(len(daily_calls), 3, "7 个任务 × 12 条 → 3 批")
        self.assertEqual(res["generator"].split("+")[0], "llm")
        md = res["daily_md"]
        positions = [md.index("整理过的：任务%d" % k) for k in range(7)]
        self.assertEqual(positions, sorted(positions), "合并后保持原来的任务顺序")
        for k in range(7):
            self.assertEqual(md.count("整理过的：任务%d" % k), 1)

    def test_todos_link_back_to_the_right_task(self):
        res = self._generate(FakeModel())
        todo = res["daily_md"].split("## To do")[1]
        # 每批的 [1] 是那一批第一个任务：第 1、4、7 个
        for k in (0, 3, 6):
            self.assertIn("跟进：任务%d" % k, todo)
        loose = [ln for ln in todo.splitlines() if re.fullmatch(r"\d+\. 与任务无关的待办", ln.strip())]
        self.assertEqual(len(loose), 1, "各批重复的无主待办只留一条（只有一个任务的那批会归给它）")

    def test_one_failed_batch_degrades_only_its_own_tasks(self):
        res = self._generate(FakeModel(fail_on=("任务3 的",)))       # 第 2 批（任务 4–6）超时
        md = res["daily_md"]
        self.assertEqual(res["generator"].split("+")[0], "llm", "不是整篇兜底")
        for k in (0, 1, 2, 6):
            self.assertIn("整理过的：任务%d" % k, md)
        for k in (3, 4, 5):
            self.assertIn("任务%d 的第0条进展" % k, md, "失败那批用进展原文顶上")
        self.assertIn("有 1/3 批模型没写成", md)
        self.assertTrue(any("第 2/3 批" in w and "timed out" in w for w in res["warnings"]))

    def test_all_batches_failing_falls_back_like_before(self):
        res = self._generate(FakeModel(fail_on=("今天推进了",)))
        self.assertTrue(res["generator"].startswith("fallback"))
        self.assertIn("本篇为兜底稿", res["daily_md"])


class TestTodoCleanup(unittest.TestCase):
    def test_model_written_task_names_are_stripped(self):
        """名字和链接是系统拼的，模型再写一遍就成了双重标题。"""
        self.assertEqual(digest._clean_todo("**曝光权限与 IPM 验证**：把 FFalcon 推给 Yongcheng"),
                         "把 FFalcon 推给 Yongcheng")
        self.assertEqual(digest._clean_todo("**Bug Hunter**: **收尾**：核验密钥"), "核验密钥")
        self.assertEqual(digest._clean_todo("核验 **CMD_API_KEY** 的注入"), "核验 **CMD_API_KEY** 的注入",
                         "句中的加粗不动")

    def test_unnumbered_todo_in_a_single_task_batch_belongs_to_that_task(self):
        reset()
        self.addCleanup(reset)
        rec = store.record_progress("t", "AI-2541 做了一半", date=D)
        tasks = db.day_tasks("t", D)
        reply = "[1] wip | 做了一半\nTODO\n- **Fecho**：把另一半做完"
        with mock.patch.object(llm, "chat", return_value=reply):
            _, todos, _ = digest._write_batch("t", D, tasks, [1], digest.persona_for("t"), "")
        self.assertEqual(todos, [(1, "把另一半做完")])
        self.assertEqual(tasks[0]["task_id"], rec["task"]["task_id"])


class TestSectionNumbering(unittest.TestCase):
    def test_each_section_counts_from_one(self):
        """以前用全天的序号：Done 里 1、2、4、5，In Progress 里 3、7，看着像漏了几条。"""
        tasks = [{"task_id": str(k), "issue_key": None, "title": "任务%d" % k, "updates": []} for k in range(5)]
        items = {1: {"status": "done", "summary": "a", "bullets": []},
                 2: {"status": "wip", "summary": "b", "bullets": []},
                 3: {"status": "done", "summary": "c", "bullets": []},
                 4: {"status": "wip", "summary": "d", "bullets": []},
                 5: {"status": "done", "summary": "e", "bullets": []}}
        md = digest._assemble_daily(D, tasks, items, [], digest.persona_for("t"))
        done = md.split("## Done")[1].split("## In Progress")[0]
        wip = md.split("## In Progress")[1]
        self.assertEqual(re.findall(r"^(\d+)\.", done, re.M), ["1", "2", "3"])
        self.assertEqual(re.findall(r"^(\d+)\.", wip, re.M), ["1", "2"])


class TestTestsStayQuiet(unittest.TestCase):
    def test_simulated_scenarios_do_not_print_to_the_screen(self):
        """测试里虚构的 Alice、2030 年、网关超时打到屏幕上，会被 agent 的对话记下来，
        再被 Fecho 扫描当成真事记进日报（9/23 真出过）。"""
        import os
        import sys
        if os.environ.get("FECHO_TEST_VERBOSE"):
            self.skipTest("手动要求看输出")
        self.assertTrue(getattr(sys.stdout, "_fecho_quiet", False))
