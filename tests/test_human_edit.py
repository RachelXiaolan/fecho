"""人亲手改日报：旧版进历史、不被自动覆盖、按改动学写作偏好、按新日报重出口播稿。"""
import _env  # noqa: F401  必须在 import fecho 之前
import unittest
from unittest import mock

from fecho import accounts, config, db, digest, jobs, llm, pto, store, style, web, worker  # noqa: E402
from test_cloud_auth import CloudCase, WebCase, A, B  # noqa: E402

DAY = "2030-01-10"
MODEL_DAILY = ("# 2030/01/10 工作日志\n\n## Done\n\n"
               "1. ✅ [**agent 原生 time-off：Fecho**](https://mobius.feedmob.com/issue/AI-2541)"
               "：顺手把本机采集写完了，整体进展非常顺利\n")
EDITED_DAILY = ("# 2030/01/10 工作日志\n\n## Done\n\n"
                "1. ✅ [**Fecho**](https://mobius.feedmob.com/issue/AI-2541)：本机采集写完了\n")


def clear(*tables):
    with db.cursor() as c:
        for t in tables:
            c.execute("DELETE FROM %s" % t)


class EditCase(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(A, "Alice")
        accounts.upsert_user(B, "Bob")
        clear("task_aliases", "style_profiles", "report_history", "jobs")
        store.record_progress(A, "把本机采集写完了", date=DAY, freeform=True)
        fp = digest.fingerprint(db.day_tasks(A, DAY), digest.persona_for(A), pto.status(A, DAY))
        digest._persist(A, DAY, "daily", MODEL_DAILY, fp, "llm", "m", 1)
        digest._persist(A, DAY, "voice", "今天把本机采集写完了。", fp, "llm", "m", 1)

    def daily(self):
        return db.get_report(A, DAY, "daily")

    def history(self, kind="daily"):
        return [h for h in db.report_history(A, DAY) if h["kind"] == kind]


class TestSavingAnEdit(EditCase):
    def test_old_version_goes_to_history_new_one_is_marked_human(self):
        r = digest.save_human_edit(A, DAY, EDITED_DAILY)
        self.assertEqual(r["status"], "saved")
        self.assertEqual(self.daily()["generator"], "human")
        self.assertIn("本机采集写完了", self.daily()["content_md"])
        self.assertEqual([h["content_md"] for h in self.history()], [MODEL_DAILY])

    def test_a_fresh_edit_is_not_marked_for_regeneration(self):
        digest.save_human_edit(A, DAY, EDITED_DAILY)
        self.assertFalse(web.report_payload(A, DAY)["dirty"])

    def test_unchanged_and_empty(self):
        self.assertEqual(digest.save_human_edit(A, DAY, MODEL_DAILY)["status"], "unchanged")
        self.assertEqual(self.history(), [], "没改就不该多一条历史")
        with self.assertRaises(ValueError):
            digest.save_human_edit(A, DAY, "   ")

    def test_renaming_a_task_updates_its_short_name(self):
        """改任务名是确定的事，直接存成短名，不用等模型学。"""
        r = digest.save_human_edit(A, DAY, EDITED_DAILY)
        self.assertEqual(r["renamed"], {"AI-2541": "Fecho"})
        with db.cursor() as c:
            row = c.execute("SELECT alias FROM task_aliases WHERE author=? AND task_key='AI-2541'",
                            (A,)).fetchone()
        self.assertEqual(row["alias"], "Fecho")


class TestHumanEditIsNotOverwritten(EditCase):
    def test_new_progress_does_not_overwrite_an_edit(self):
        digest.save_human_edit(A, DAY, EDITED_DAILY)
        store.record_progress(A, "又补了一条进展", date=DAY, freeform=True)
        with mock.patch.object(config, "llm_configured", return_value=False):
            r = digest.generate(A, DAY, force=True)          # 补扫后自动重出走的就是这个
        self.assertEqual(r["status"], "kept-human")
        self.assertEqual(self.daily()["content_md"].strip(), EDITED_DAILY.strip())
        self.assertTrue(web.report_payload(A, DAY)["dirty"], "有新进展要提示人")

    def test_explicit_regenerate_overwrites_and_the_edit_stays_in_history(self):
        digest.save_human_edit(A, DAY, EDITED_DAILY)
        with mock.patch.object(config, "llm_configured", return_value=False):
            r = digest.generate(A, DAY, force=True, keep_human=False)
        self.assertEqual(r["status"], "generated")
        self.assertNotEqual(self.daily()["generator"], "human")
        self.assertIn("human", [h["generator"] for h in self.history()])


class TestStyleProfile(EditCase):
    def learn(self, reply):
        with mock.patch.object(llm, "chat", return_value=reply) as chat:
            return style.learn(A, DAY), chat

    def test_an_edit_becomes_rules(self):
        digest.save_human_edit(A, DAY, EDITED_DAILY)
        r, chat = self.learn("- 每个任务的总结写短，一句话说完\n- 不用「顺手」「非常顺利」这种口语")
        self.assertEqual((r["status"], r["rules"]), ("learned", 2))
        self.assertIn("不用「顺手」", style.get(A)["content_md"])
        prompt = chat.call_args[0][0][1]["content"]
        self.assertIn(MODEL_DAILY.strip(), prompt)
        self.assertIn("本机采集写完了", prompt)

    def test_nothing_to_learn_keeps_what_was_there(self):
        style.save(A, "- 已有的一条")
        digest.save_human_edit(A, DAY, EDITED_DAILY)
        r, _ = self.learn("NONE")
        self.assertEqual(r["status"], "unchanged")
        self.assertEqual(style.get(A)["content_md"], "- 已有的一条")

    def test_does_not_learn_from_a_report_nobody_edited(self):
        r, chat = self.learn("- 不该出现")
        self.assertEqual(r["status"], "skipped")
        chat.assert_not_called()

    def test_profile_goes_into_the_prompt_but_not_the_fingerprint(self):
        before = web.report_payload(A, DAY)["dirty"]
        style.save(A, "- 不用口语")
        tasks = db.day_tasks(A, DAY)
        system = digest._daily_prompt(A, DAY, tasks, digest.persona_for(A), style.for_prompt(A))[0]
        self.assertIn("不用口语", system["content"])
        self.assertEqual(web.report_payload(A, DAY)["dirty"], before,
                         "偏好一更新就把日报标成要重出，那所有人的日报都会亮黄条")

    def test_generate_hands_the_profile_to_the_model(self):
        style.save(A, "- 总结不超过二十个字")
        seen = []

        def fake_chat(messages, **kw):
            system = messages[0]["content"]
            seen.append(system)
            if "起短名" in system:
                return "[1] Fecho"
            if "口播稿" in system:
                return "今天主要把本机采集脚本写完了。" * 20
            return "[1] done | 写完了本机采集\n- done | 脚本写完了"

        with mock.patch.object(config, "llm_configured", return_value=True), \
             mock.patch.object(llm, "chat", side_effect=fake_chat):
            digest.generate(A, DAY, force=True, keep_human=False)
        daily_prompts = [s for s in seen if "日志助手" in s]
        self.assertTrue(daily_prompts)
        self.assertIn("总结不超过二十个字", daily_prompts[0])

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            style.save(A, "- " + "字" * (style.MAX_CHARS + 1))

    def test_clearing(self):
        style.save(A, "- 一条")
        self.assertEqual(style.save(A, "")["content_md"], "")


class TestVoiceFollowsTheEdit(EditCase):
    def test_voice_is_rewritten_from_the_edited_report(self):
        digest.save_human_edit(A, DAY, EDITED_DAILY)
        seen = []

        def fake_chat(messages, **kw):
            seen.append(messages[1]["content"])
            return "今天把本机采集写完了，接下来准备做验收。" * 12

        with mock.patch.object(llm, "chat", side_effect=fake_chat):
            r = digest.regenerate_voice(A, DAY)
        self.assertEqual(r["status"], "generated")
        self.assertIn("[**Fecho**]", seen[0], "口播稿要按改好的日报写")
        self.assertEqual(self.daily()["generator"], "human", "重出口播稿不能动日报")
        self.assertIn("接下来准备做验收", db.get_report(A, DAY, "voice")["content_md"])


class TestWorkerKinds(EditCase):
    def run_kind(self, kind):
        clear("jobs")
        jobs.enqueue(A, kind, DAY)
        job = worker.claim()[0]
        worker.run_job(job)
        with db.cursor() as c:
            return c.execute("SELECT status, error FROM jobs").fetchone()

    def test_voice_and_learn_jobs(self):
        with mock.patch.object(digest, "regenerate_voice", return_value={"status": "generated"}) as v, \
             mock.patch.object(style, "learn", return_value={"status": "learned", "rules": 2}) as l, \
             mock.patch.object(worker, "sync_issues") as sync:
            self.assertEqual(self.run_kind("voice")["status"], "succeeded")
            self.assertEqual(self.run_kind("learn")["status"], "succeeded")
        v.assert_called_once_with(A, DAY)
        l.assert_called_once_with(A, DAY)
        sync.assert_not_called()

    def test_only_an_explicit_regenerate_overwrites_an_edit(self):
        from fecho import service
        calls = {}
        with mock.patch.object(worker, "sync_issues"), \
             mock.patch.object(service, "end_of_day",
                               side_effect=lambda d, **kw: calls.setdefault(kw_kind[0], kw) or {}):
            for kind in ("daily", "refresh", "regenerate"):
                kw_kind = [kind]
                self.run_kind(kind)
        self.assertEqual((calls["daily"]["force"], calls["daily"]["keep_human"]), (False, True))
        self.assertEqual((calls["refresh"]["force"], calls["refresh"]["keep_human"]), (True, True))
        self.assertEqual((calls["regenerate"]["force"], calls["regenerate"]["keep_human"]), (True, False))

    def test_two_edits_in_a_row_queue_one_voice_job(self):
        clear("jobs")
        jobs.enqueue(A, "voice", DAY)
        jobs.enqueue(A, "voice", DAY)
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM jobs WHERE kind='voice'").fetchone()["n"], 1)


class TestOverHttp(WebCase):
    def setUp(self):
        super().setUp()
        clear("style_profiles", "report_history", "jobs")
        fp = digest.fingerprint(db.day_tasks(A, self.today), digest.persona_for(A),
                                pto.status(A, self.today))
        digest._persist(A, self.today, "daily", MODEL_DAILY, fp, "llm", "m", 1)

    def test_editing_queues_voice_and_learning(self):
        r = self.post("/api/reports/daily", A, {"date": self.today, "content_md": EDITED_DAILY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["dashboard"]["reports"]["generator"], "human")
        with db.cursor() as c:
            kinds = sorted(x["kind"] for x in c.execute(
                "SELECT kind FROM jobs WHERE author=?", (A,)).fetchall())
        self.assertEqual(kinds, ["learn", "voice"])

    def test_you_can_only_edit_your_own_report(self):
        """B 没有这天的日报：带着 B 的 token 改，改的只能是 B 自己的。"""
        r = self.post("/api/reports/daily", B, {"date": self.today, "content_md": "改别人的"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(db.get_report(A, self.today, "daily")["content_md"], MODEL_DAILY)

    def test_style_profiles_are_private(self):
        self.assertEqual(self.post("/api/style", A, {"content_md": "- Alice 的偏好"}).status_code, 200)
        self.assertEqual(self.get("/api/style", A).json()["content_md"], "- Alice 的偏好")
        self.assertEqual(self.get("/api/style", B).json()["content_md"], "")


class TestPageContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.html = (Path(__file__).resolve().parents[1] / "fecho" / "presets" /
                    "dashboard.html").read_text(encoding="utf-8")

    def test_edit_and_style_controls_exist(self):
        for needle in ('id="edit-report"', "/api/reports/daily", 'id="style-editor"', "/api/style"):
            self.assertIn(needle, self.html)

    def test_auto_refresh_does_not_wipe_what_you_are_typing(self):
        body = self.html[self.html.index("function renderReports"):]
        body = body[:body.index("function renderSystem")]
        self.assertIn("state.editing", body)
        self.assertLess(body.index("$('#report-editor')) return;"), body.index("innerHTML=html"))


if __name__ == "__main__":
    unittest.main()
