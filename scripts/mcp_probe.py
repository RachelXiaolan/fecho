#!/usr/bin/env python3
"""最小 MCP client：把 fecho-mcp 当子进程拉起来，走真实 stdio JSON-RPC。

开发时用来验证工具链路，不依赖任何 MCP 宿主。
  python3 scripts/mcp_probe.py list
  python3 scripts/mcp_probe.py call log_progress '{"content":"..."}'
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MCPSession:
    def __init__(self, client_name="mcp-probe", session_id=None, cmd=None):
        env = dict(os.environ)
        if session_id:
            env["FECHO_SESSION_ID"] = session_id
        self.p = subprocess.Popen(
            cmd or [sys.executable, "-u", "-m", "fecho.mcp_server"],
            cwd=ROOT, env=env, text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": client_name, "version": "0.0.1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def _rpc(self, method, params=None):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("server closed: " + self.p.stderr.read())
        return json.loads(line)

    def list_tools(self):
        return self._rpc("tools/list")["result"]["tools"]

    def call(self, name, args):
        r = self._rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r.get("isError", False)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="mcp-probe")
    ap.add_argument("--session", default=None)
    ap.add_argument("cmd", choices=["list", "call"])
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args()
    s = MCPSession(a.client, a.session)
    try:
        if a.cmd == "list":
            for t in s.list_tools():
                print("- %s: %s" % (t["name"], t["description"].splitlines()[0][:70]))
        else:
            text, err = s.call(a.rest[0], json.loads(a.rest[1]) if len(a.rest) > 1 else {})
            print(("[ERROR] " if err else "") + text)
    finally:
        s.close()


if __name__ == "__main__":
    main()
