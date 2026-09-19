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

    def test_table_comes_from_db_init_not_from_the_request(self):
        """建表是部署时的一次性动作，不在请求路径里做。

        0.8.1 上线时漏跑建表，保存日报当场 500——教训是「缺表要响，不要偷偷补」：
        请求路径里跑 DDL，并发冷启动会打架，而且线上少了哪张表没人知道。
        这张表在 SCHEMA_CLOUD 里，`db.init()` 会建。
        """
        self.assertIn("quick_api_keys", db.table_names())
        import inspect
        source = inspect.getsource(quick_api)
        self.assertNotIn("CREATE TABLE", source, "建表不该写在 quick_api 里")
        self.assertNotIn("ROW LEVEL SECURITY", source,
                         "开 RLS 归 db.init() 统一做（它给每张表都开，用来挡 Supabase "
                         "那个能直接读库的 Data API），应用代码里不要逐表临时补")

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


    # ---- 交原料：走完整的归属和整理，和随手记是同一条路 ----

    def test_updates_go_into_the_pipeline_not_straight_to_the_report(self):
        """交原料只是往库里加进展，不会动这一天的日报。"""
        key = self.issue().json()["api_key"]
        r = self.client.post("/api/quick-api/updates",
                             headers=self.headers(key, "application/json"),
                             json={"date": "2030-01-20", "entries": [
                                 {"content": "Hermes 接好了，默认模型设成 auto"},
                                 {"content": "擂台赛复盘页发布了", "completion_status": "done"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["saved"], 2)
        self.assertIsNone(db.get_report(A, "2030-01-20", "daily"), "交原料不该写日报")
        with db.cursor() as conn:
            rows = conn.execute("SELECT ingestion_method, source_agent FROM updates"
                                " WHERE author=? AND date=?", (A, "2030-01-20")).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["ingestion_method"] for r in rows}, {"quick-api"},
                         "「今天」页要看得出这条是从快速 API 交的")

    def test_a_single_entry_can_be_posted_without_wrapping_it(self):
        key = self.issue().json()["api_key"]
        r = self.client.post("/api/quick-api/updates",
                             headers=self.headers(key, "application/json"),
                             json={"content": "随手交一条", "date": "2030-01-21"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["saved"], 1)

    def test_retrying_the_same_entry_does_not_double_log_it(self):
        """模型调 HTTP 会超时重试。带上 event_key，重复交只记一次。"""
        key = self.issue().json()["api_key"]
        body = {"date": "2030-01-22",
                "entries": [{"content": "同一件事", "event_key": "grok:abc123"}]}
        first = self.client.post("/api/quick-api/updates",
                                 headers=self.headers(key, "application/json"), json=body)
        again = self.client.post("/api/quick-api/updates",
                                 headers=self.headers(key, "application/json"), json=body)
        self.assertEqual(first.json()["saved"], 1)
        self.assertEqual(again.json()["saved"], 0)
        self.assertEqual(again.json()["skipped_duplicates"], 1)
        with db.cursor() as conn:
            n = conn.execute("SELECT COUNT(*) n FROM updates WHERE author=? AND date=?",
                             (A, "2030-01-22")).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_an_unknown_issue_key_falls_back_to_a_freeform_task(self):
        """归错了当天日报会跟着错，归不上只是多一个自由任务——所以认不出的 issue 不报错。"""
        key = self.issue().json()["api_key"]
        r = self.client.post("/api/quick-api/updates",
                             headers=self.headers(key, "application/json"),
                             json={"content": "写了点东西", "issue": "AI-9999",
                                   "date": "2030-01-23"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["updates"][0]["issue"])

    def test_updates_are_scoped_to_the_key_owner(self):
        a_key = self.issue_for(A).json()["api_key"]
        b_key = self.issue_for(B).json()["api_key"]
        for key, who in ((a_key, "A 交的"), (b_key, "B 交的")):
            self.client.post("/api/quick-api/updates",
                             headers=self.headers(key, "application/json"),
                             json={"content": who, "date": "2030-01-24"})
        with db.cursor() as conn:
            got = {r["author"]: r["content_md"] for r in conn.execute(
                "SELECT author, content_md FROM updates WHERE date=?", ("2030-01-24",)).fetchall()}
        self.assertEqual(got, {A: "A 交的", B: "B 交的"})

    def test_empty_or_oversized_entries_are_refused(self):
        key = self.issue().json()["api_key"]
        for bad in ({"entries": []}, {"content": "   "}, {"content": "x" * 4001}):
            r = self.client.post("/api/quick-api/updates",
                                 headers=self.headers(key, "application/json"), json=bad)
            self.assertEqual(r.status_code, 400, "%s -> %s" % (bad, r.text))

    def test_updates_need_the_key_too(self):
        r = self.client.post("/api/quick-api/updates",
                             headers={"Content-Type": "application/json"},
                             json={"content": "没带钥匙"})
        self.assertEqual(r.status_code, 401)

    def test_openapi_tells_the_model_which_road_to_take(self):
        """两条路的语义差别必须写在文档里，否则模型不知道该用哪个。"""
        document = self.client.get("/api/quick-api/openapi.json").json()
        self.assertIn("/api/quick-api/updates", document["paths"])
        blurb = document["info"]["description"]
        self.assertIn("追加", blurb)
        self.assertIn("覆盖", blurb)
        self.assertIn("钥匙是谁的", blurb)


if __name__ == "__main__":
    unittest.main()
