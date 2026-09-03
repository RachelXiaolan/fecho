"""Mobius 浏览器登录（OAuth 2.1 + PKCE + 动态客户端注册）。

Mobius 的 MCP 端点自带完整的 OAuth 元数据，且允许公开客户端动态注册，
所以整个流程可以零配置：不需要人事先去后台申请 client_id，也不需要贴 token。

  发现元数据 → 动态注册 → 起本地回调 → 开浏览器 → 换 token → 存盘（0600）

access_token 过期会用 refresh_token 自动续，续不上才要求重新登录。
只依赖标准库 + httpx，不引 OAuth 库。
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, Optional, Tuple

import httpx

from . import config

DEFAULT_SCOPES = "mobius:read"
_TIMEOUT = 30


class OAuthError(RuntimeError):
    pass


# ---------- 元数据发现 ----------

def discover(resource_url: str) -> Dict[str, Any]:
    """从受保护资源反查授权服务器元数据。"""
    origin = "%s://%s" % urllib.parse.urlparse(resource_url)[:2]
    try:
        pr = httpx.get(origin + "/.well-known/oauth-protected-resource",
                       timeout=_TIMEOUT).json()
        issuer = (pr.get("authorization_servers") or [origin])[0]
        scopes = " ".join(pr.get("scopes_supported") or [DEFAULT_SCOPES])
        asm = httpx.get(issuer.rstrip("/") + "/.well-known/oauth-authorization-server",
                        timeout=_TIMEOUT).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OAuthError("读取 OAuth 元数据失败: %s" % exc) from exc
    missing = [k for k in ("authorization_endpoint", "token_endpoint") if not asm.get(k)]
    if missing:
        raise OAuthError("授权服务器缺少必要端点: %s" % ",".join(missing))
    asm["_scopes"] = scopes
    asm["_resource"] = pr.get("resource", resource_url)
    return asm


# ---------- 动态注册 ----------

def register_client(meta: Dict[str, Any], redirect_uri: str) -> Dict[str, Any]:
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise OAuthError("该授权服务器不支持动态注册，请改用 --token 手动贴凭证")
    body = {
        "client_name": "Fecho 工作日志",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": meta["_scopes"],
    }
    r = httpx.post(endpoint, json=body, timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise OAuthError("动态注册失败 %d: %s" % (r.status_code, r.text[:300]))
    return r.json()


# ---------- 本地回调 ----------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<style>body{font:16px/1.6 system-ui;margin:15vh auto;max-width:28em;text-align:center}"
    "b{font-size:1.4em}</style><b>%s</b><p>%s</p><p style='color:#888'>可以关掉这个页面了。</p>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    result: Dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Handler.result = {k: v[0] for k, v in q.items()}
        ok = "code" in _Handler.result
        page = _PAGE % (("✅ Mobius 已连接", "回到你的 agent 会话继续。") if ok
                        else ("❌ 授权失败", _Handler.result.get("error_description",
                                              _Handler.result.get("error", "未知错误"))))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 不要往 stdout 写东西——MCP 的 stdout 是协议通道
        pass


def _await_callback(port: int, timeout: int) -> Dict[str, str]:
    _Handler.result = {}
    srv = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    srv.timeout = 1
    t = threading.Thread(target=lambda: None)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not _Handler.result:
            srv.handle_request()
    finally:
        srv.server_close()
        del t
    if not _Handler.result:
        raise OAuthError("等待浏览器回调超时（%d 秒）" % timeout)
    if "code" not in _Handler.result:
        raise OAuthError("授权被拒绝: %s" % _Handler.result.get(
            "error_description", _Handler.result.get("error", "unknown")))
    return _Handler.result


# ---------- 主流程 ----------

def build_authorize_url(resource_url: str, timeout: int = 300) -> Tuple[str, Dict[str, Any]]:
    """准备好授权 URL 和后续换 token 需要的上下文（不开浏览器）。"""
    meta = discover(resource_url)
    port = _free_port()
    redirect_uri = "http://127.0.0.1:%d/callback" % port
    client = register_client(meta, redirect_uri)

    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "scope": meta["_scopes"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": meta["_resource"],
    }
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
    ctx = {"meta": meta, "client": client, "verifier": verifier, "state": state,
           "redirect_uri": redirect_uri, "port": port, "timeout": timeout}
    return url, ctx


def complete(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """等回调、换 token、存盘。"""
    cb = _await_callback(ctx["port"], ctx["timeout"])
    if cb.get("state") != ctx["state"]:
        raise OAuthError("state 不匹配，可能是 CSRF，已中止")

    data = {
        "grant_type": "authorization_code",
        "code": cb["code"],
        "redirect_uri": ctx["redirect_uri"],
        "client_id": ctx["client"]["client_id"],
        "code_verifier": ctx["verifier"],
        "resource": ctx["meta"]["_resource"],
    }
    r = httpx.post(ctx["meta"]["token_endpoint"], data=data, timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise OAuthError("换取 token 失败 %d: %s" % (r.status_code, r.text[:300]))
    tok = r.json()
    return _persist(tok, ctx["meta"], ctx["client"])


def _persist(tok: Dict[str, Any], meta: Dict[str, Any],
             client: Dict[str, Any]) -> Dict[str, Any]:
    expires_at = int(time.time()) + int(tok.get("expires_in") or 3600) - 60
    config.update(
        mobius_token=tok["access_token"],
        mobius_auth="oauth",
        mobius_oauth=json.loads(json.dumps({
            "refresh_token": tok.get("refresh_token"),
            "expires_at": expires_at,
            "token_endpoint": meta["token_endpoint"],
            "client_id": client["client_id"],
            "resource": meta["_resource"],
            "scope": meta["_scopes"],
        })),
    )
    config.reload_module()
    return {"ok": True, "expires_at": expires_at, "scope": meta["_scopes"]}


def login(resource_url: str, open_browser: bool = True,
          timeout: int = 300) -> Dict[str, Any]:
    url, ctx = build_authorize_url(resource_url, timeout=timeout)
    if open_browser:
        webbrowser.open(url)
    return {"authorize_url": url, "ctx": ctx}


def refresh_if_needed() -> Optional[str]:
    """快过期就续期。返回新 token；不需要续或续不上则返回 None。"""
    st = config.load().get("mobius_oauth") or {}
    if not st.get("refresh_token"):
        return None
    if time.time() < (st.get("expires_at") or 0):
        return None
    r = httpx.post(st["token_endpoint"], timeout=_TIMEOUT, data={
        "grant_type": "refresh_token",
        "refresh_token": st["refresh_token"],
        "client_id": st["client_id"],
        "resource": st.get("resource"),
    })
    if r.status_code >= 400:
        return None
    tok = r.json()
    st = dict(st)
    st["expires_at"] = int(time.time()) + int(tok.get("expires_in") or 3600) - 60
    if tok.get("refresh_token"):
        st["refresh_token"] = tok["refresh_token"]
    config.update(mobius_token=tok["access_token"], mobius_oauth=st)
    config.reload_module()
    return tok["access_token"]


def status() -> Dict[str, Any]:
    st = config.load().get("mobius_oauth") or {}
    if not config.MOBIUS_TOKEN:
        return {"connected": False, "auth": "none"}
    out = {"connected": True, "auth": config.load().get("mobius_auth", "token")}
    if st.get("expires_at"):
        left = int(st["expires_at"] - time.time())
        out["expires_in_seconds"] = left
        out["refreshable"] = bool(st.get("refresh_token"))
    return out
