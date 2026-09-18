"""临时快速 API：轮换、OpenAPI、日报和图片上传。"""
import _env  # noqa: F401 必须在 import fecho 之前
import unittest

from fecho import db, quick_api  # noqa: E402
from test_cloud_auth import A, B, WebCase  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class TestQuickApi(WebCase):
    def issue_for(self, who):
        return self.client.post(
            "/api/quick-api/credentials",
            headers={"Authorization": "Bearer " + self.tok[who]},
        )

    def issue(self):
        return self.issue_for(A)

    def headers(self, key, content_type=None):
        out = {"Authorization": "Bearer " + key}
        if content_type:
            out["Content-Type"] = content_type
        return out

    def test_missing_table_is_created_on_first_use(self):
        with db.cursor() as conn:
            conn.execute("DROP TABLE quick_api_keys")
        quick_api._table_ready = False
        response = self.issue()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(quick_api.resolve(response.json()["api_key"]), A)

    def test_keys_are_scoped_to_the_google_account(self):
        a_key = self.issue_for(A).json()["api_key"]
        b_key = self.issue_for(B).json()["api_key"]
        self.assertEqual(quick_api.resolve(a_key), A)
        self.assertEqual(quick_api.resolve(b_key), B)
        self.assertNotEqual(a_key, b_key)
        a_upload = self.client.post(
            "/api/quick-api/reports?date=2030-01-13",
            headers=self.headers(a_key, "text/markdown"),
            content="# A 的日报",
        )
        b_upload = self.client.post(
            "/api/quick-api/reports?date=2030-01-13",
            headers=self.headers(b_key, "text/markdown"),
            content="# B 的日报",
        )
        self.assertEqual((a_upload.status_code, b_upload.status_code), (200, 200))
        self.assertEqual(db.get_report(A, "2030-01-13", "daily")["content_md"].strip(), "# A 的日报")
        self.assertEqual(db.get_report(B, "2030-01-13", "daily")["content_md"].strip(), "# B 的日报")

    def test_rotation_only_keeps_new_key_and_stores_hash(self):
        first = self.issue()
        second = self.issue()
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        old_key, new_key = first.json()["api_key"], second.json()["api_key"]
        self.assertNotEqual(old_key, new_key)
        self.assertIsNone(quick_api.resolve(old_key))
        self.assertEqual(quick_api.resolve(new_key), A)
        with db.cursor() as conn:
            row = dict(conn.execute("SELECT * FROM quick_api_keys WHERE author=?", (A,)).fetchone())
        self.assertNotIn(new_key, str(row))
        self.assertNotIn(old_key, str(row))

    def test_openapi_document_is_importable_without_exposing_data(self):
        response = self.client.get("/api/quick-api/open.json")
        self.assertEqual(response.status_code, 200)
        document = response.json()
        self.assertEqual(document["openapi"], "3.0.3")
        self.assertIn("/api/quick-api/reports", document["paths"])
        self.assertIn("/api/quick-api/resources", document["paths"])
        self.assertEqual(document["servers"][0]["url"], "http://testserver")

    def test_report_and_image_upload_use_the_rotated_key(self):
        key = self.issue().json()["api_key"]
        report = self.client.post(
            "/api/quick-api/reports?date=2030-01-11",
            headers=self.headers(key, "text/markdown"),
            content="# 2030-01-11 工作日志\n\n## Done\n",
        )
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(db.get_report(A, "2030-01-11", "daily")["generator"], "human")

        image = self.client.post(
            "/api/quick-api/resources?alt=截图&width=640",
            headers=self.headers(key, "image/png"),
            content=PNG,
        )
        self.assertEqual(image.status_code, 200, image.text)
        payload = image.json()
        self.assertIn("![截图|640]", payload["markdown"])
        fetched = self.client.get(payload["url"], headers=self.headers(key))
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.headers["content-type"], "image/png")

    def test_old_key_cannot_upload_or_read_after_rotation(self):
        old_key = self.issue().json()["api_key"]
        image = self.client.post(
            "/api/quick-api/resources",
            headers=self.headers(old_key, "image/png"),
            content=PNG,
        )
        self.assertEqual(image.status_code, 200)
        url = image.json()["url"]
        new_key = self.issue().json()["api_key"]
        self.assertEqual(
            self.client.post("/api/quick-api/resources", headers=self.headers(old_key, "image/png"), content=PNG).status_code,
            401,
        )
        self.assertEqual(self.client.get(url, headers=self.headers(old_key)).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.headers(new_key)).status_code, 200)

    def test_x_api_key_header_is_supported(self):
        key = self.issue().json()["api_key"]
        response = self.client.post(
            "/api/quick-api/reports",
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            json={"date": "2030-01-12", "content_md": "# JSON 日报"},
        )
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
