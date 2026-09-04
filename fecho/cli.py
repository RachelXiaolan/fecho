"""fecho 命令行：装完之后人（或 agent）用来配置和排查的入口。"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

from . import __version__, config, db, service, store


def _p(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj)


def cmd_doctor(args) -> int:
    d = service.doctor()
    print("Fecho %s · 作者=%s · 数据在 %s\n" % (
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
        print("提示：还不知道要拉谁的 issue，补一句 fecho login --assignee you@company.com")
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
        "collector_url": args.collector_url, "collector_token": args.collector_token,
    }.items() if v}
    if not kw:
        print("没有要改的。可设：--author --display-name --persona "
              "--llm-url --llm-key --llm-model --llm-reasoning-effort --mobius-assignee "
              "--collector-url --collector-token")
        return 1
    config.update(**kw)
    config.reload_module()
    print("已写入 %s（0600）：%s" % (config.CONFIG_FILE, "、".join(sorted(kw))))
    return cmd_doctor(args)


def cmd_sync(args) -> int:
    _p(service.sync_issues(args.assignee))
    return 0


def cmd_bind(args) -> int:
    """把当前目录（或指定路径片段）绑到一个 Mobius issue 上。

    解决的是：做这个项目本身时，说的话跟 issue 标题字面上一个词都不重合，
    关键词配对必然失效。工作目录是个免费的强信号，绑一次就够。
    """
    bindings = dict(config.PROJECT_BINDINGS)

    if args.list or (not args.issue and not args.remove):
        if not bindings:
            print("还没有任何项目绑定。\n  在项目目录里跑：fecho bind AI-2541")
            return 0
        print("项目绑定（路径含左边片段 → 默认归右边的 issue）：\n")
        for frag, key in sorted(bindings.items()):
            print("  %-40s → %s" % (frag, key))
        return 0

    if args.remove:
        if args.remove not in bindings:
            print("没有这条绑定：%s" % args.remove, file=sys.stderr)
            return 1
        bindings.pop(args.remove)
        config.update(project_bindings=bindings)
        print("已解除：%s" % args.remove)
        return 0

    frag = args.path or os.getcwd()
    if not args.path:
        # 默认用「最后两级目录」而不是绝对路径：换台机器、仓库挪了位置都还能匹配上
        parts = Path(frag).parts
        frag = os.path.join(*parts[-2:]) if len(parts) >= 2 else frag
    bindings[frag] = args.issue
    config.update(project_bindings=bindings)
    print("已绑定：路径含 %s 的项目 → %s" % (frag, args.issue))
    print("之后在这个项目里记的进展，配不到别的 issue 时默认归它。")
    return 0


def cmd_scope(args) -> int:
    """管工作范围。默认不扫任何目录——没登记的一律跳过。"""
    from . import scope

    if args.work_prefix:
        scope.add("work_prefixes", os.path.abspath(os.path.expanduser(args.work_prefix)))
        print("已登记工作前缀：%s（这底下的项目默认都算工作）"
              % os.path.abspath(os.path.expanduser(args.work_prefix)))
    elif args.work:
        val = args.work if args.work != "." else os.getcwd()
        scope.add("work", val)
        print("已登记为工作项目：%s（没有对应 issue 也会记，走自由任务）" % val)
    elif args.ignore:
        val = args.ignore if args.ignore != "." else os.getcwd()
        scope.add("ignore", val)
        print("已加入忽略：%s（这里的活动永远不会进工作日志，也不会发给 LLM）" % val)
    elif args.remove:
        scope.remove(args.remove)
        print("已移除：%s" % args.remove)

    r = scope.rules()
    print("\n当前工作范围：")
    for label, key in (("工作前缀", "work_prefixes"), ("工作项目", "work"), ("忽略", "ignore")):
        vals = r[key] or ["（无）"]
        for v in vals:
            print("  %-8s %s" % (label, v))
    for frag, key in sorted(config.PROJECT_BINDINGS.items()):
        print("  %-8s %s → %s" % ("绑定", frag, key))

    verdict, why = scope.classify(os.getcwd())
    print("\n当前目录 %s\n  → %s（%s）" % (os.getcwd(), verdict, why))
    if verdict == "unregistered":
        print("  未登记的目录不会被扫描。要算工作就跑 fecho scope --work .")
    return 0


def cmd_digest(args) -> int:
    db.init()
    r = service.end_of_day(args.date, force=args.force)
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
    tp = r.get("team_push")
    if tp:
        print(("    团队: 已推送" if tp.get("pushed") else "    团队: 未推送（%s）" % tp.get("reason")))
    return 0


def cmd_team(args) -> int:
    from . import push

    try:
        d = service.team_digest(args.date or store.today(), args.author)
    except push.PushError as exc:
        print("连不上团队 collector：%s" % exc, file=sys.stderr)
        return 1
    if not d["count"]:
        print("%s 团队还没有人推送过报告" % d["date"])
        return 0
    print("%s 团队日报（%d 人）：\n" % (d["date"], d["count"]))
    for a in d["authors"]:
        print("=== %s ===" % a["author"])
        if a.get("daily"):
            print(a["daily"]["content_md"])
        if a.get("voice"):
            print("\n--- 口播稿 ---\n%s" % a["voice"]["content_md"])
        print()
    return 0


MCP_SNIPPET = {
    "mcpServers": {
        "fecho": {"command": "fecho-mcp", "args": [], "env": {}}
    }
}


def cmd_install(args) -> int:
    print("把下面这段合进你的 MCP 配置：\n")
    print(json.dumps(MCP_SNIPPET, ensure_ascii=False, indent=2))
    print("\n位置：")
    print("  claude code : ~/.claude.json 的 mcpServers（或 claude mcp add fecho -- fecho-mcp）")
    print("  codex       : ~/.codex/config.toml 的 [mcp_servers.fecho]")
    print("\n装好之后在 agent 会话里调 fecho_doctor，它会告诉你还缺什么。")
    return 0


def cmd_serve(args) -> int:
    """起团队 collector：只收日报/口播稿成品，不是共享的原始数据库。

    部署在一台团队都能访问到的内部机器上。认证用 config.TOKENS_FILE
    （examples/tokens.example.json 是格式参考），一人一个 token。
    """
    import uvicorn

    print("Fecho collector 启动在 %s:%d" % (args.host, args.port))
    print("token 文件：%s（不存在就先建一个，格式见 examples/tokens.example.json）"
          % config.TOKENS_FILE)
    uvicorn.run("fecho.collector:app", host=args.host, port=args.port)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="fecho", description="Agent 优先的工作日志系统")
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

    p = sub.add_parser("setup", help="写配置（身份 / LLM / Mobius / 团队 collector）")
    for flag in ("--author", "--display-name", "--persona", "--llm-url", "--llm-key",
                 "--llm-model", "--llm-reasoning-effort", "--mobius-assignee",
                 "--collector-url", "--collector-token"):
        p.add_argument(flag)
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("sync", help="刷新 Mobius issue 缓存")
    p.add_argument("--assignee")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("scope", help="管工作范围：哪些目录能进工作日志（默认不扫）")
    p.add_argument("--work-prefix", help="这个前缀底下的项目默认都算工作，如放工作仓库的那个父目录")
    p.add_argument("--work", help="登记为工作项目（. = 当前目录），没有 issue 也会记")
    p.add_argument("--ignore", help="永不进工作日志、永不发给 LLM（. = 当前目录）")
    p.add_argument("--remove", help="移除某条规则")
    p.set_defaults(fn=cmd_scope)

    p = sub.add_parser("bind", help="把当前项目绑到一个 Mobius issue（解决关键词配不上的问题）")
    p.add_argument("issue", nargs="?", help="如 AI-2541")
    p.add_argument("--path", help="要绑的路径片段，缺省=当前目录的最后两级")
    p.add_argument("--list", action="store_true", help="看现有绑定")
    p.add_argument("--remove", help="解除某条绑定")
    p.set_defaults(fn=cmd_bind)

    p = sub.add_parser("digest", help="日终整理（cron 入口，配了 collector 会顺带推送）")
    p.add_argument("--date")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("team", help="看团队某天的日报（需要配好 collector）")
    p.add_argument("--date")
    p.add_argument("--author", help="只看某人")
    p.set_defaults(fn=cmd_team)

    sub.add_parser("install", help="打印 MCP 配置片段").set_defaults(fn=cmd_install)

    p = sub.add_parser("serve", help="起团队 collector（只收成品，不是共享数据库）")
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--port", type=int, default=config.PORT)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args()
    db.init()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
