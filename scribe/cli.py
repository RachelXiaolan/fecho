"""scribe 命令行：装完之后人（或 agent）用来配置和排查的入口。"""
import argparse
import json
import sys
from typing import Any, Dict

from . import __version__, config, db, service, store


def _p(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj)


def cmd_doctor(args) -> int:
    d = service.doctor()
    print("Scribe %s · 作者=%s · 数据在 %s\n" % (
        __version__, d["config"]["author"], d["config"]["home"]))
    for c in d["checks"]:
        print("%s %s — %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
        if c.get("fix"):
            print("    → %s" % c["fix"])
    print("\n记进展 %s｜自动配 Mobius %s｜出日报 %s" % (
        "✓", "✓" if d["ready_to_match"] else "✗", "✓" if d["ready_to_report"] else "✗"))
    return 0


def cmd_login(args) -> int:
    from . import oauth

    url = args.url or config.MOBIUS_URL or "https://mobius.feedmob.com/api/mcp"
    updates: Dict[str, Any] = {"mobius_url": url}
    if args.assignee:
        updates["mobius_assignee"] = args.assignee
    config.update(**updates)
    config.reload_module()

    if args.token:
        config.update(mobius_token=args.token, mobius_auth="token")
        config.reload_module()
        print("已保存 token（%s，权限 0600）" % config.CONFIG_FILE)
    else:
        print("正在打开浏览器完成 Mobius 授权…")
        started = oauth.login(url, open_browser=not args.no_browser, timeout=args.timeout)
        if args.no_browser:
            print("请在浏览器打开：\n%s\n" % started["authorize_url"])
        try:
            res = oauth.complete(started["ctx"])
        except oauth.OAuthError as exc:
            print("授权失败：%s" % exc, file=sys.stderr)
            return 1
        print("已连接（授权有效期至 %s）" % res["expires_at"])

    if not config.MOBIUS_ASSIGNEE:
        print("提示：还不知道要拉谁的 issue，补一句 scribe login --assignee you@company.com")
        return 0
    try:
        s = service.sync_issues()
        print("已同步 %d 个在办 issue（%s）" % (s["count"], s["assignee"]))
    except Exception as exc:
        print("同步 issue 失败：%s" % exc, file=sys.stderr)
        return 1
    return 0


def cmd_setup(args) -> int:
    kw = {k: v for k, v in {
        "author": args.author, "display_name": args.display_name, "persona": args.persona,
        "llm_base_url": args.llm_url, "llm_api_key": args.llm_key, "llm_model": args.llm_model,
        "llm_reasoning_effort": args.llm_reasoning_effort,
        "mobius_assignee": args.mobius_assignee,
    }.items() if v}
    if not kw:
        print("没有要改的。可设：--author --display-name --persona "
              "--llm-url --llm-key --llm-model --llm-reasoning-effort --mobius-assignee")
        return 1
    config.update(**kw)
    config.reload_module()
    print("已写入 %s（0600）：%s" % (config.CONFIG_FILE, "、".join(sorted(kw))))
    return cmd_doctor(args)


def cmd_sync(args) -> int:
    _p(service.sync_issues(args.assignee))
    return 0


def cmd_digest(args) -> int:
    from . import digest

    db.init()
    r = digest.generate(config.AUTHOR, args.date or store.today(), force=args.force)
    line = "[%s] %s %s · %d 个任务 / %d 条进展" % (
        r["status"], r["date"], r["author"], r["task_count"], r["update_count"])
    if r.get("generator"):
        line += " · %s" % r["generator"]
    if r.get("voice_chars"):
        line += " · 口播 %d 字/%ds" % (r["voice_chars"], r["voice_seconds_est"])
    print(line)
    for w in r.get("warnings", []):
        print("    ! %s" % w)
    for p in (r.get("files") or {}).values():
        print("    -> %s" % p)
    return 0


MCP_SNIPPET = {
    "mcpServers": {
        "scribe": {"command": "scribe-mcp", "args": [], "env": {}}
    }
}


def cmd_install(args) -> int:
    print("把下面这段合进你的 MCP 配置：\n")
    print(json.dumps(MCP_SNIPPET, ensure_ascii=False, indent=2))
    print("\n位置：")
    print("  claude code : ~/.claude.json 的 mcpServers（或 claude mcp add scribe -- scribe-mcp）")
    print("  codex       : ~/.codex/config.toml 的 [mcp_servers.scribe]")
    print("\n装好之后在 agent 会话里调 scribe_doctor，它会告诉你还缺什么。")
    return 0


def cmd_serve(args) -> int:
    """团队共享部署时才需要：起 REST 服务，多人共用一个库。"""
    import uvicorn

    uvicorn.run("scribe.api:app", host=args.host, port=args.port)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="scribe", description="Agent 优先的工作日志系统")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="自检：什么配好了、什么还缺").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("login", help="连接 Mobius（默认走浏览器授权）")
    p.add_argument("--token", help="不走浏览器，直接贴一个 token")
    p.add_argument("--assignee", help="你在 Mobius 上的邮箱，决定拉谁的 issue")
    p.add_argument("--url", help="Mobius MCP 端点，默认 https://mobius.feedmob.com/api/mcp")
    p.add_argument("--no-browser", action="store_true", help="不自动开浏览器，只打印链接")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("setup", help="写配置（身份 / LLM / Mobius）")
    for flag in ("--author", "--display-name", "--persona", "--llm-url", "--llm-key",
                 "--llm-model", "--llm-reasoning-effort", "--mobius-assignee"):
        p.add_argument(flag)
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("sync", help="刷新 Mobius issue 缓存")
    p.add_argument("--assignee")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("digest", help="日终整理（cron 入口）")
    p.add_argument("--date")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_digest)

    sub.add_parser("install", help="打印 MCP 配置片段").set_defaults(fn=cmd_install)

    p = sub.add_parser("serve", help="起 REST 服务（团队共享部署时才需要）")
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--port", type=int, default=config.PORT)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args()
    db.init()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
