"""网页上的反馈收集区：提建议、带截图、只有自己和 admin 能看、admin 标记处理状态。"""
import _env  # noqa: F401  必须在 import fecho 之前
import base64
import unittest
from pathlib import Path

from fecho import accounts, db, feedback  # noqa: E402
from test_cloud_auth import ADMIN, CloudCase, WebCase, A, B  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def clear():
    with db.cursor() as c:
        c.execute("DELETE FROM feedback_images")
        c.execute("DELETE FROM feedback")


class TestSubmit(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(A, "Alice")
        clear()

    def test_text_with_images_keeps_their_order(self):
        item = feedback.submit(A, "  日报里任务名太长  ", [
            {"mime": "image/png", "data": b64(PNG)}, {"mime": "image/jpeg", "data": b64(JPEG)}],
            page="reports")
        self.assertEqual((item["body"], item["page"], item["status"]), ("日报里任务名太长", "reports", "new"))
        self.assertEqual(len(item["images"]), 2)
        self.assertEqual(feedback.image_for(item["images"][0], A)[0], "image/png")
        self.assertEqual(feedback.image_for(item["images"][1], A)[0], "image/jpeg")

    def test_image_only_is_fine_and_webp_is_recognised(self):
        item = feedback.submit(A, "", [{"mime": "image/webp", "data": b64(WEBP)}])
        self.assertEqual(feedback.image_for(item["images"][0], A)[0], "image/webp")

    def test_type_comes_from_the_file_not_from_what_the_browser_claims(self):
        """伪装成图片的 SVG 能在浏览器里跑脚本，按文件头一律挡掉。"""
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with self.assertRaises(ValueError):
            feedback.submit(A, "看这张图", [{"mime": "image/png", "data": b64(svg)}])
        with self.assertRaises(ValueError):
            feedback.submit(A, "网页", [{"mime": "image/png", "data": b64(b"<html></html>")}])

    def test_limits(self):
        with self.assertRaises(ValueError):
            feedback.submit(A, "   ", [])
        with self.assertRaises(ValueError):
            feedback.submit(A, "太多", [{"data": b64(PNG)}] * (feedback.MAX_IMAGES + 1))
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * feedback.MAX_IMAGE_BYTES
        with self.assertRaises(ValueError):
            feedback.submit(A, "太大", [{"data": b64(big)}])
        with self.assertRaises(ValueError):
            feedback.submit(A, "坏数据", [{"data": "不是base64!!"}])
        with self.assertRaises(ValueError):
            feedback.submit(A, "字" * (feedback.MAX_BODY_CHARS + 1))
        with db.cursor() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM feedback").fetchone()["n"], 0,
                             "被挡掉的不能留下半条")

    def test_unknown_page_is_not_stored(self):
        self.assertIsNone(feedback.submit(A, "x", page="<script>")["page"])


class TestOverHttp(WebCase):
    def setUp(self):
        super().setUp()
        clear()

    def submit(self, who, body, images=()):
        r = self.post("/api/feedback", who, {"body": body, "page": "today",
                                             "images": [{"mime": "image/png", "data": b64(i)} for i in images]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["item"]

    def test_everyone_sees_only_their_own(self):
        self.submit(A, "Alice 的建议", [PNG])
        self.assertEqual([f["body"] for f in self.get("/api/feedback", A).json()["items"]], ["Alice 的建议"])
        self.assertEqual(self.get("/api/feedback", B).json()["items"], [])

    def test_images_open_only_for_the_sender_and_admins(self):
        item = self.submit(A, "带图", [PNG])
        path = "/api/feedback/images/%s" % item["images"][0]
        mine = self.get(path, A)
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.content, PNG)
        self.assertEqual(mine.headers["content-type"], "image/png")
        self.assertEqual(mine.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.get(path, B).status_code, 404, "别人拿到 ID 也打不开")
        self.assertEqual(self.get(path, ADMIN).status_code, 200)
        self.assertEqual(self.get(path).status_code, 401)

    def test_admin_sees_everyone_and_marks_status(self):
        item = self.submit(A, "保存没反应")
        self.assertEqual(self.get("/api/admin/feedback", A).status_code, 403)
        all_items = self.get("/api/admin/feedback", ADMIN).json()["items"]
        self.assertEqual([(f["body"], f["display_name"]) for f in all_items], [("保存没反应", "Alice")])
        r = self.post("/api/admin/feedback/%s" % item["feedback_id"], ADMIN, {"status": "done"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get("/api/feedback", A).json()["items"][0]["status"], "done",
                         "提的人能看到处理状态")
        self.assertEqual(self.post("/api/admin/feedback/%s" % item["feedback_id"], A,
                                   {"status": "seen"}).status_code, 403)
        self.assertEqual(self.post("/api/admin/feedback/%s" % item["feedback_id"], ADMIN,
                                   {"status": "whatever"}).status_code, 400)

    def test_rejected_image_explains_why(self):
        r = self.post("/api/feedback", A, {"body": "x", "images": [{"mime": "image/png", "data": b64(b"<svg/>")}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("PNG", r.json()["error"])


class TestPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "fecho" / "presets" /
                    "dashboard.html").read_text(encoding="utf-8")

    def test_feedback_page_accepts_images_three_ways(self):
        for needle in ('data-view="feedback" data-cloud-only hidden', 'data-page="feedback"',
                       'type="file"', "addEventListener('paste'", "addEventListener('drop'",
                       "/api/feedback", "/api/admin/feedback"):
            self.assertIn(needle, self.html)

    def test_images_are_shrunk_before_upload(self):
        """线上一次请求最多 4.5 MB：大图要在浏览器里先压缩。"""
        self.assertIn("canvas.toBlob", self.html)
        self.assertIn("FB_TOTAL_BYTES", self.html)


if __name__ == "__main__":
    unittest.main()
