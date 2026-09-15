"""还没生成就手写日报、人写的日报旁边自动版只进历史版本、日报里贴图。"""
import _env  # noqa: F401  必须在 import fecho 之前
import base64
import unittest
from unittest import mock

from fecho import accounts, config, db, digest, report_images, store, style, web  # noqa: E402
from test_cloud_auth import ADMIN, CloudCase, WebCase, A, B  # noqa: E402

DAY = "2030-01-11"
WRITTEN = "# 2030-01-11 工作日志\n\n## Done\n\n- [x] 把本机采集写完了\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


class WriteCase(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(A, "Alice")
        with db.cursor() as c:
            for table in ("reports", "report_history", "jobs", "style_profiles"):
                c.execute("DELETE FROM %s" % table)
        store.record_progress(A, "把本机采集写完了", date=DAY, freeform=True)

    def daily(self):
        return db.get_report(A, DAY, "daily")

    def history(self):
        return [h for h in db.report_history(A, DAY) if h["kind"] == "daily"]

    def generate(self, **kw):
        with mock.patch.object(config, "llm_configured", return_value=False):
            return digest.generate(A, DAY, **kw)


class TestWritingBeforeGeneration(WriteCase):
    def test_can_write_before_any_report_exists(self):
        r = digest.save_human_edit(A, DAY, WRITTEN)
        self.assertEqual((r["status"], r.get("new")), ("saved", True))
        self.assertEqual(self.daily()["generator"], "human")
        self.assertEqual(self.history(), [])
        self.assertFalse(web.report_payload(A, DAY)["dirty"], "刚写完、没新进展，不该提示重新生成")

    def test_writing_on_an_empty_day_is_fine(self):
        r = digest.save_human_edit(A, "2030-01-12", "# 休息日也写两句\n")
        self.assertEqual(r["status"], "saved")

    def test_nothing_to_learn_from_a_report_written_from_scratch(self):
        digest.save_human_edit(A, DAY, WRITTEN)
        self.assertEqual(style.learn(A, DAY)["status"], "skipped")


class TestAutomaticVersionGoesToHistory(WriteCase):
    def test_scheduled_run_keeps_the_written_report_and_archives_its_own(self):
        digest.save_human_edit(A, DAY, WRITTEN)
        r = self.generate()                               # 到点出日报走的就是这个
        self.assertEqual(r["status"], "kept-human")
        self.assertEqual(self.daily()["content_md"].strip(), WRITTEN.strip())
        archived = self.history()
        self.assertEqual(len(archived), 1)
        self.assertNotEqual(archived[0]["generator"], "human")
        self.assertIn("本机采集", archived[0]["content_md"])
        self.assertIsNone(db.get_report(A, DAY, "voice"), "口播稿按人写的出，自动版不碰它")

    def test_no_new_facts_no_new_archived_version(self):
        digest.save_human_edit(A, DAY, WRITTEN)
        self.generate()
        self.generate(force=True)
        self.assertEqual(len(self.history()), 1)

    def test_new_progress_archives_another_version(self):
        digest.save_human_edit(A, DAY, WRITTEN)
        self.generate()
        store.record_progress(A, "又补了一条进展", date=DAY, freeform=True)
        self.generate()
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(self.daily()["content_md"].strip(), WRITTEN.strip())
        self.assertTrue(web.report_payload(A, DAY)["dirty"], "有新进展要提示人")

    def test_editing_a_generated_report_does_not_archive_the_same_facts_again(self):
        """改的是模型那版：改之前的版本已经在历史里，事实没变就不再出一版。"""
        self.generate()
        digest.save_human_edit(A, DAY, WRITTEN)
        self.generate()
        self.assertEqual(len(self.history()), 1)

    def test_explicit_regenerate_still_replaces_it(self):
        digest.save_human_edit(A, DAY, WRITTEN)
        r = self.generate(force=True, keep_human=False)
        self.assertEqual(r["status"], "generated")
        self.assertNotEqual(self.daily()["generator"], "human")
        self.assertIn(WRITTEN.strip(), [h["content_md"].strip() for h in self.history()])


class TestReportImages(CloudCase):
    def setUp(self):
        super().setUp()
        for who, name in ((A, "Alice"), (B, "Bob"), (ADMIN, "Rachel")):
            accounts.upsert_user(who, name)
        with db.cursor() as c:
            c.execute("DELETE FROM report_images")

    def test_owner_and_admin_can_open_others_cannot(self):
        saved = report_images.save(A, b64(PNG))
        self.assertTrue(saved["url"].endswith(saved["image_id"]))
        self.assertEqual(report_images.image_for(saved["image_id"], A)[0], "image/png")
        self.assertIsNone(report_images.image_for(saved["image_id"], B))
        if accounts.is_admin(ADMIN):
            self.assertIsNotNone(report_images.image_for(saved["image_id"], ADMIN))

    def test_only_real_images_within_the_limit(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        for bad in (b64(svg), "", "不是 base64",
                    b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * report_images.MAX_IMAGE_BYTES)):
            with self.assertRaises(ValueError):
                report_images.save(A, bad)


class TestReportImagesOverHttp(WebCase):
    def test_upload_then_only_the_owner_can_fetch(self):
        r = self.post("/api/reports/images", A, json={"data": b64(PNG)})
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["url"]
        got = self.get(url, A)
        self.assertEqual((got.status_code, got.headers["content-type"]), (200, "image/png"))
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.get(url, B).status_code, 404)
        self.assertEqual(self.get(url).status_code, 401)

    def test_write_before_generation_over_http(self):
        with db.cursor() as c:
            c.execute("DELETE FROM reports WHERE author=? AND date=?", (A, self.today))
        r = self.post("/api/reports/daily", A, json={"date": self.today, "content_md": WRITTEN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["saved"]["new"])
        self.assertEqual(db.get_report(A, self.today, "daily")["generator"], "human")


if __name__ == "__main__":
    unittest.main()
