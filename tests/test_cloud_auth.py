"""云端版：登录、凭证、按人隔离、admin。

最要紧的是隔离——A 登录后，从任何一个入口都不能碰到 B 的数据。
"""
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import _env  # noqa: E402,F401  必须在 import fecho 之前
TMP = _env.TMP

from fecho import accounts, config, db, store  # noqa: E402

A = "alice@feedmob.com"
B = "bob@feedmob.com"
ADMIN = "rachel.lu@feedmob.com"
SECRET = "x" * 48


def reset():
    db.require_disposable()   # 清表前确认连的是临时库
    db.init()
    with db.cursor() as c:
        for t in ("updates", "tasks", "users", "api_tokens", "web_sessions", "admin_requests",
                  "mobius_credentials", "jobs", "app_settings", "reports"):
            c.execute("DELETE FROM %s" % t)


class CloudCase(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(config, "CLOUD", True),
            mock.patch.object(config, "SECRET_KEY", SECRET),
            mock.patch.object(config, "PUBLIC_URL", "http://testserver"),
            mock.patch.object(config, "BOOTSTRAP_ADMINS", {ADMIN}),
            mock.patch.object(config, "ALLOWED_EMAIL_DOMAIN", "feedmob.com"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        reset()


# ---------- 用户与凭证 ----------

class TestUsers(CloudCase):
    def test_only_company_emails_get_in(self):
        with self.assertRaises(accounts.AccessDenied):
            accounts.upsert_user("someone@gmail.com", "路人")
        with self.assertRaises(accounts.AccessDenied):
            accounts.upsert_user("x@feedmob.com.evil.com", "伪装")
        self.assertEqual(accounts.upsert_user("Alice@FeedMob.com", "Alice")["author"], A)

    def test_bootstrap_admin(self):
        self.assertTrue(accounts.upsert_user(ADMIN, "Rachel")["is_admin"])
        self.assertFalse(accounts.upsert_user(A, "Alice")["is_admin"])

    def test_relogin_does_not_drop_admin(self):
        """admin 是审批给的，重新登录不能把它冲掉。"""
        accounts.upsert_user(A, "Alice")
        with db.cursor() as c:
            c.execute("UPDATE users SET is_admin=1 WHERE author=?", (A,))
        self.assertTrue(accounts.upsert_user(A, "Alice")["is_admin"])

    def test_daily_time_floor(self):
        """扫描要提前 15 分钟跑，出日报最早 00:15。"""
        accounts.upsert_user(A, "Alice")
        self.assertEqual(accounts.set_daily_time(A, "21:30"), "21:30")
        with self.assertRaises(ValueError):
            accounts.set_daily_time(A, "00:10")
        with self.assertRaises(ValueError):
            accounts.set_daily_time(A, "22:00")


class TestTokens(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(A, "Alice")

    def test_token_resolves_to_owner_and_is_stored_hashed(self):
        raw = accounts.issue_token(A)
        self.assertTrue(raw.startswith("fecho_"))
        self.assertEqual(accounts.resolve_token(raw), A)
        with db.cursor() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM api_tokens").fetchall()]
        self.assertNotIn(raw, str(rows), "库里只能有哈希，不能有原文")

    def test_unknown_and_revoked_tokens_fail(self):
        self.assertIsNone(accounts.resolve_token("fecho_made_up"))
        self.assertIsNone(accounts.resolve_token(""))
        raw = accounts.issue_token(A)
        accounts.revoke_tokens(A)
        self.assertIsNone(accounts.resolve_token(raw))

    def test_token_expires_after_seven_idle_days(self):
        raw = accounts.issue_token(A)
        later = accounts._now() + timedelta(days=7, minutes=1)
        with mock.patch.object(accounts, "_now", return_value=later):
            self.assertIsNone(accounts.resolve_token(raw))

    def test_token_slides_when_used(self):
        """每用一次顺延：第 6 天用了一次，第 12 天还有效。"""
        raw = accounts.issue_token(A)
        t0 = accounts._now()
        with mock.patch.object(accounts, "_now", return_value=t0 + timedelta(days=6)):
            self.assertEqual(accounts.resolve_token(raw), A)
        with mock.patch.object(accounts, "_now", return_value=t0 + timedelta(days=12)):
            self.assertEqual(accounts.resolve_token(raw), A,
                             "第 6 天用过，应该顺延到第 13 天")
        with mock.patch.object(accounts, "_now", return_value=t0 + timedelta(days=20)):
            self.assertIsNone(accounts.resolve_token(raw), "第 12 天之后 7 天没用，该失效了")

    def test_sessions_work_the_same_way(self):
        raw = accounts.create_session(A)
        self.assertEqual(accounts.resolve_session(raw), A)
        accounts.end_session(raw)
        self.assertIsNone(accounts.resolve_session(raw))


class TestSecrets(CloudCase):
    def test_encrypt_roundtrip(self):
        enc = accounts.encrypt("refresh-token-value")
        self.assertNotIn("refresh-token-value", enc)
        self.assertEqual(accounts.decrypt(enc), "refresh-token-value")

    def test_signed_state_rejects_tampering_and_expiry(self):
        signed = accounts.sign({"state": "s1"})
        self.assertEqual(accounts.unsign(signed)["state"], "s1")
        data, mac = signed.rsplit(".", 1)
        with self.assertRaises(accounts.AccessDenied):
            accounts.unsign(data + "." + "0" * len(mac))
        expired = accounts.sign({"state": "s1"}, ttl_seconds=-1)
        with self.assertRaises(accounts.AccessDenied):
            accounts.unsign(expired)

    def test_refuses_to_run_without_a_real_secret(self):
        with mock.patch.object(config, "SECRET_KEY", "short"):
            with self.assertRaises(RuntimeError):
                accounts.encrypt("x")


class TestAdminRequests(CloudCase):
    def setUp(self):
        super().setUp()
        accounts.upsert_user(ADMIN, "Rachel")
        accounts.upsert_user(A, "Alice")

    def test_request_then_approve(self):
        req = accounts.request_admin(A)
        self.assertEqual(req["status"], "pending")
        self.assertEqual(accounts.request_admin(A)["request_id"], req["request_id"],
                         "重复点申请不该建第二条")
        self.assertEqual(len(accounts.admin_requests()), 1)
        accounts.decide_admin_request(req["request_id"], ADMIN, approve=True)
        self.assertTrue(accounts.is_admin(A))
        self.assertEqual(accounts.admin_requests(), [])

    def test_deny(self):
        req = accounts.request_admin(A)
        accounts.decide_admin_request(req["request_id"], ADMIN, approve=False)
        self.assertFalse(accounts.is_admin(A))

    def test_only_admins_can_decide(self):
        accounts.upsert_user(B, "Bob")
        req = accounts.request_admin(A)
        with self.assertRaises(accounts.AccessDenied):
            accounts.decide_admin_request(req["request_id"], B, approve=True)

    def test_admin_does_not_need_to_request(self):
        self.assertEqual(accounts.request_admin(ADMIN)["status"], "already-admin")


# ---------- Mobius 登录 ----------

class TestMobiusLogin(CloudCase):
    META = {"authorization_endpoint": "https://m/authorize", "token_endpoint": "https://m/token",
            "registration_endpoint": "https://m/register", "_scopes": "mobius:read",
            "_resource": "https://m/api/mcp"}

    def _login(self, whoami):
        from fecho import mobius_login, oauth
        tok_resp = mock.Mock(status_code=200)
        tok_resp.json.return_value = {"access_token": "at", "refresh_token": "rt",
                                      "expires_in": 3600}
        with mock.patch.object(oauth, "discover", return_value=self.META), \
             mock.patch.object(oauth, "register_client", return_value={"client_id": "cid"}):
            url, signed = mobius_login.start()
            state = dict(p.split("=") for p in url.split("?")[1].split("&"))["state"]
            with mock.patch.object(mobius_login.httpx, "post", return_value=tok_resp), \
                 mock.patch.object(mobius_login, "whoami", return_value=whoami):
                return mobius_login.finish("code", state, signed)

    def test_person_with_company_email_gets_in_and_credentials_are_encrypted(self):
        from fecho import mobius_login
        author = self._login({"email": "Alice@feedmob.com", "name": "Alice",
                              "kind": "person", "id": "usr_1"})
        self.assertEqual(author, A)
        self.assertTrue(mobius_login.connected(A))
        with db.cursor() as c:
            row = dict(c.execute("SELECT * FROM mobius_credentials WHERE author=?",
                                 (A,)).fetchone())
        self.assertNotIn("rt", (row["refresh_token_enc"], row["access_token_enc"]))
        self.assertEqual(mobius_login.access_token(A), "at")

    def test_agent_tokens_cannot_log_in(self):
        """agent 的 token 也能调 whoami，不挡的话机器人就能冒充人登进来。"""
        with self.assertRaises(accounts.AccessDenied):
            self._login({"email": "bot@feedmob.com", "kind": "personal agent"})

    def test_outside_emails_cannot_log_in(self):
        with self.assertRaises(accounts.AccessDenied):
            self._login({"email": "x@gmail.com", "kind": "person"})

    def test_client_registration_is_reused(self):
        """每登录一次就去 Mobius 注册一个客户端，那边会越堆越多。"""
        from fecho import mobius_login, oauth
        with mock.patch.object(oauth, "discover", return_value=self.META), \
             mock.patch.object(oauth, "register_client",
                               return_value={"client_id": "cid"}) as reg:
            mobius_login.start()
            mobius_login.start()
        self.assertEqual(reg.call_count, 1)

    def test_state_mismatch_is_rejected(self):
        from fecho import mobius_login, oauth
        with mock.patch.object(oauth, "discover", return_value=self.META), \
             mock.patch.object(oauth, "register_client", return_value={"client_id": "cid"}):
            _url, signed = mobius_login.start()
        with self.assertRaises(accounts.AccessDenied):
            mobius_login.finish("code", "wrong-state", signed)


# ---------- 网页和 MCP：隔离 ----------

class WebCase(CloudCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("没装 fastapi")
        from fecho import web
        self.client = TestClient(web.build_app())
        for who, name in ((A, "Alice"), (B, "Bob"), (ADMIN, "Rachel")):
            accounts.upsert_user(who, name)
        self.tok = {who: accounts.issue_token(who) for who in (A, B, ADMIN)}
        self.today = store.today()
        store.record_progress(A, "Alice 的秘密进展", date=self.today, freeform=True)
        store.record_progress(B, "Bob 的秘密进展", date=self.today, freeform=True)

    def get(self, path, who=None, **kw):
        headers = {"Authorization": "Bearer " + self.tok[who]} if who else {}
        return self.client.get(path, headers=headers, **kw)

    def post(self, path, who=None, json=None):
        headers = {"Authorization": "Bearer " + self.tok[who]} if who else {}
        return self.client.post(path, headers=headers, json=json or {})


class TestIsolation(WebCase):
    def test_not_logged_in_gets_nothing(self):
        self.assertEqual(self.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                 "method": "tools/list"}).status_code, 401)
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/login"))

    def test_each_person_sees_only_their_own_dashboard(self):
        a = self.get("/api/dashboard", A).text
        b = self.get("/api/dashboard", B).text
        self.assertIn("Alice 的秘密进展", a)
        self.assertNotIn("Bob 的秘密进展", a)
        self.assertIn("Bob 的秘密进展", b)
        self.assertNotIn("Alice 的秘密进展", b)

    def test_cannot_touch_someone_elses_update_by_id(self):
        """猜到别人进展的 id 也不能改。"""
        with db.cursor() as c:
            bob_update = c.execute("SELECT update_id FROM updates WHERE author=?",
                                   (B,)).fetchone()["update_id"]
        r = self.post("/api/correct", A, {"update_id": bob_update, "content": "被 Alice 改了"})
        self.assertEqual(r.status_code, 400)
        with db.cursor() as c:
            content = c.execute("SELECT content_md FROM updates WHERE update_id=?",
                                (bob_update,)).fetchone()["content_md"]
        self.assertEqual(content, "Bob 的秘密进展")

    def test_mcp_records_under_the_token_owner(self):
        def call(who, text):
            return self.post("/mcp", who, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "log_progress",
                                                      "arguments": {"content": text,
                                                                    "freeform": True}}})
        self.assertEqual(call(A, "Alice 通过 MCP 记的").status_code, 200)
        self.assertEqual(call(B, "Bob 通过 MCP 记的").status_code, 200)
        with db.cursor() as c:
            owners = {r["content_md"]: r["author"] for r in
                      c.execute("SELECT content_md, author FROM updates").fetchall()}
        self.assertEqual(owners["Alice 通过 MCP 记的"], A)
        self.assertEqual(owners["Bob 通过 MCP 记的"], B)

    def test_reused_mcp_session_id_does_not_leak_identity(self):
        """会话号是缓存的，身份不能跟着缓存走——每次都按 token 认人。"""
        r = self.post("/mcp", A, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"clientInfo": {"name": "claude-code"}}})
        sid = r.headers["mcp-session-id"]
        self.client.post("/mcp", headers={"Authorization": "Bearer " + self.tok[B],
                                          "Mcp-Session-Id": sid},
                         json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "log_progress",
                                          "arguments": {"content": "拿着 Alice 的会话号",
                                                        "freeform": True}}})
        with db.cursor() as c:
            owner = c.execute("SELECT author FROM updates WHERE content_md=?",
                              ("拿着 Alice 的会话号",)).fetchone()["author"]
        self.assertEqual(owner, B)

    def test_unknown_mcp_session_id_never_falls_back_to_the_default_identity(self):
        """多实例部署下，请求可能落到没见过这个会话号的实例上。"""
        self.client.post("/mcp", headers={"Authorization": "Bearer " + self.tok[A],
                                          "Mcp-Session-Id": "never-seen-before"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "log_progress",
                                          "arguments": {"content": "陌生会话号",
                                                        "freeform": True}}})
        with db.cursor() as c:
            owner = c.execute("SELECT author FROM updates WHERE content_md=?",
                              ("陌生会话号",)).fetchone()["author"]
        self.assertEqual(owner, A)

    def test_sse_is_not_offered_in_cloud(self):
        self.assertEqual(self.get("/sse/", A).status_code, 404)


class TestAdminPanel(WebCase):
    def test_non_admin_is_refused(self):
        for path in ("/api/admin/users", "/api/admin/requests",
                     "/api/admin/dashboard?author=" + B):
            self.assertEqual(self.get(path, A).status_code, 403, path)

    def test_admin_can_read_anyones_log(self):
        r = self.get("/api/admin/dashboard?author=" + B, ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bob 的秘密进展", r.text)
        users = {u["author"] for u in self.get("/api/admin/users", ADMIN).json()["items"]}
        self.assertEqual(users, {A, B, ADMIN})

    def test_request_and_approval_through_the_api(self):
        self.assertFalse(self.get("/api/me", A).json()["is_admin"])
        req = self.post("/api/admin/request", A).json()
        self.assertEqual(self.get("/api/me", A).json()["admin_request"]["status"], "pending")
        pending = self.get("/api/admin/requests", ADMIN).json()["items"]
        self.assertEqual([p["author"] for p in pending], [A])
        self.post("/api/admin/requests/" + req["request_id"], ADMIN, {"approve": True})
        self.assertTrue(self.get("/api/me", A).json()["is_admin"])
        self.assertEqual(self.get("/api/admin/users", A).status_code, 200)


class TestWebLoginFlow(WebCase):
    def test_tokens_can_only_be_minted_from_a_browser_session(self):
        """拿着一个 agent token 不该能再生出更多 token。"""
        self.assertEqual(self.post("/api/tokens", A).status_code, 401)
        self.client.cookies.set("fecho_session", accounts.create_session(A))
        r = self.client.post("/api/tokens", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(accounts.resolve_token(r.json()["token"]), A)

    def test_settings_saves_daily_time(self):
        r = self.post("/api/settings", A, {"daily_time": "20:30"})
        self.assertEqual(r.json()["daily_time"], "20:30")
        self.assertEqual(accounts.get_user(A)["daily_time"], "20:30")

    def test_regenerate_is_queued_not_run_inline(self):
        """出日报要几分钟，云端版的网页请求等不了，只排队。"""
        from fecho import service
        with mock.patch.object(service, "end_of_day") as eod:
            r = self.post("/api/regenerate", A, {"date": self.today})
        eod.assert_not_called()
        self.assertTrue(r.json()["queued"])
        with db.cursor() as c:
            job = dict(c.execute("SELECT * FROM jobs WHERE author=?", (A,)).fetchone())
        self.assertEqual((job["kind"], job["status"]), ("regenerate", "queued"))

    def test_login_and_onboard_pages(self):
        self.assertIn("用 Mobius 登录", self.client.get("/login").text)
        r = self.client.get("/onboard", follow_redirects=False)
        self.assertEqual(r.status_code, 302, "没登录的人进 onboarding 页要被送去登录")
        self.client.cookies.set("fecho_session", accounts.create_session(A))
        self.assertIn("把 token 交给 agent", self.client.get("/onboard").text)


if __name__ == "__main__":
    unittest.main()
