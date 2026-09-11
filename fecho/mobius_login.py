"""云端版的 Mobius 登录：服务器替用户走一遍 OAuth。

一次登录办成三件事：
1. **证明是 feedmob 员工** —— 登录后调 Mobius 的 whoami 拿邮箱，必须是 @feedmob.com
2. **证明是真人** —— whoami 会标 kind，agent / 团队机器人的 token 一律挡掉
3. **打通 Mobius** —— 顺便存下授权，之后服务器替他同步名下的 issue

和本机版（oauth.py）的区别只在回调地址：本机版在你电脑上临时开个端口接，
这里回调到服务器的网址。发现授权服务器、动态注册、PKCE 都复用 oauth.py。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import httpx

from . import accounts, config, db, mobius, oauth

CALLBACK_PATH = "/auth/callback"
_TIMEOUT = 30


def redirect_uri() -> str:
    if not config.PUBLIC_URL:
        raise RuntimeError("FECHO_PUBLIC_URL 没设，Mobius 不知道登录完跳回哪里")
    return config.PUBLIC_URL + CALLBACK_PATH


def _client_id(meta: Dict[str, Any]) -> str:
    """在 Mobius 注册一次客户端，之后一直复用。

    按回调地址缓存：换了域名（比如从 vercel.app 换到 techmob.net）会自动重新注册。
    """
    key = "mobius_client:" + redirect_uri()
    cached = accounts.get_setting(key)
    if cached:
        return json.loads(cached)["client_id"]
    client = oauth.register_client(meta, redirect_uri())
    accounts.set_setting(key, json.dumps({"client_id": client["client_id"]}))
    return client["client_id"]


def start() -> Tuple[str, str]:
    """返回 (跳去 Mobius 的地址, 要写进 cookie 的签名串)。"""
    meta = oauth.discover(mobius.endpoint())
    client_id = _client_id(meta)
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "scope": meta["_scopes"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": meta["_resource"],
    })
    return url, accounts.sign({"state": state, "verifier": verifier, "client_id": client_id})


def finish(code: str, state: str, signed: str) -> str:
    """Mobius 跳回来之后：校验、换授权、查身份、建号。返回这个人的 author。"""
    ctx = accounts.unsign(signed)
    if not state or not hmac.compare_digest(ctx.get("state", ""), state):
        raise accounts.AccessDenied("登录状态对不上，可能是过期的链接，请重新登录")

    meta = oauth.discover(mobius.endpoint())
    r = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": ctx["client_id"],
        "code_verifier": ctx["verifier"],
        "resource": meta["_resource"],
    }, timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise accounts.AccessDenied("Mobius 授权失败（%d），请重新登录" % r.status_code)
    tok = r.json()

    me = whoami(tok["access_token"])
    check_identity(me)
    user = accounts.upsert_user(me["email"], me.get("name") or "", me.get("id"))
    save_credentials(user["author"], tok, meta, ctx["client_id"])
    return user["author"]


def whoami(access_token: str) -> Dict[str, Any]:
    res = mobius._rpc("tools/call", {"name": "whoami", "arguments": {}}, token=access_token)
    content = res.get("content") or []
    if not content:
        raise accounts.AccessDenied("Mobius 没有返回身份信息")
    return json.loads(content[0]["text"])


def check_identity(me: Dict[str, Any]) -> None:
    """只放本人的 feedmob 账号进来。

    kind 的取值有 person / personal agent / team agent / workspace agent。
    agent 的 token 也能调 whoami，不挡的话，一个机器人账号就能冒充人登进来。
    """
    if me.get("kind") != "person":
        raise accounts.AccessDenied("请用你本人的 Mobius 账号登录（不能用 agent 账号）")
    if not accounts.email_allowed(me.get("email", "")):
        raise accounts.AccessDenied("只允许 @%s 的邮箱登录" % config.ALLOWED_EMAIL_DOMAIN)


# ---------- 存授权、续期 ----------

def save_credentials(author: str, tok: Dict[str, Any], meta: Dict[str, Any],
                     client_id: str) -> None:
    expires_at = int(time.time()) + int(tok.get("expires_in") or 3600) - 60
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO mobius_credentials (author, access_token_enc, access_expires_at,"
            " refresh_token_enc, client_id, token_endpoint, resource, scope, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (author) DO UPDATE SET access_token_enc=excluded.access_token_enc,"
            " access_expires_at=excluded.access_expires_at,"
            " refresh_token_enc=COALESCE(excluded.refresh_token_enc,"
            "   mobius_credentials.refresh_token_enc),"
            " client_id=excluded.client_id, token_endpoint=excluded.token_endpoint,"
            " resource=excluded.resource, scope=excluded.scope, updated_at=excluded.updated_at",
            (author, accounts.encrypt(tok["access_token"]), expires_at,
             accounts.encrypt(tok.get("refresh_token")), client_id,
             meta["token_endpoint"], meta["_resource"], meta.get("_scopes"),
             accounts._iso(accounts._now())))


def access_token(author: str) -> str:
    """拿这个人当前可用的 Mobius 授权，快过期了就先续。

    续不上（refresh token 也过期了）就只能让他重新登录。Mobius 的 refresh token
    能活多久由 Mobius 决定，先跟随系统，试用一段时间再看。
    """
    with db.cursor() as conn:
        row = conn.execute("SELECT * FROM mobius_credentials WHERE author=?",
                           (author,)).fetchone()
    if not row:
        raise mobius.MobiusError("还没连接 Mobius，请先在网页上登录")
    if row["access_expires_at"] and int(row["access_expires_at"]) > int(time.time()):
        return accounts.decrypt(row["access_token_enc"])

    refresh = accounts.decrypt(row["refresh_token_enc"])
    if not refresh:
        raise mobius.MobiusError("Mobius 授权已过期，请重新登录")
    r = httpx.post(row["token_endpoint"], data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": row["client_id"],
        "resource": row["resource"],
    }, timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise mobius.MobiusError("Mobius 授权已过期，请重新登录")
    tok = r.json()
    save_credentials(author, tok, {"token_endpoint": row["token_endpoint"],
                                   "_resource": row["resource"], "_scopes": row["scope"]},
                     row["client_id"])
    return tok["access_token"]


def connected(author: str) -> bool:
    with db.cursor() as conn:
        return conn.execute("SELECT 1 FROM mobius_credentials WHERE author=?",
                            (author,)).fetchone() is not None
