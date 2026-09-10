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
        aliases = config.load().get("task_aliases") or {}
        for frag, key in sorted(bindings.items()):
            alias = "  [日报里显示为 %s]" % aliases[key] if key in aliases else ""
            print("  %-36s → %s%s" % (frag, key, alias))
        return 0

    if args.remove:
        if args.remove not in bindings:
            print("没有这条绑定：%s" % args.remove, file=sys.stderr)
            return 1
        bindings.pop(args.remove)
        config.update(project_bindings=bindings)
        print("已解除：%s" % args.remove)
        return 0

    if args.alias:
        aliases = dict(config.load().get("task_aliases") or {})
        aliases[args.issue] = args.alias
        config.update(task_aliases=aliases)
        config.reload_module()
        print("日报里 %s 会显示成「%s」" % (args.issue, args.alias))

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


def cmd_scan(args) -> int:
    """读会话记录，把「做成了什么」自动记成进展。cron 的第一步。"""
    from . import scan as scanner

    r = scanner.scan(days=args.days, dry_run=args.dry_run)
    if not r["ok"]:
        print(r["error"], file=sys.stderr)
        return 1

    for verdict, paths in sorted(r["skipped"].items()):
        label = {"unregistered": "未登记，已跳过（只知道路径，没读内容）",
                 "ignored": "已设为忽略"}.get(verdict, verdict)
        print("%s：" % label)
        for pth in paths:
            print("  %s" % pth)
        if verdict == "unregistered":
            print("  → 要算工作就在那个目录里跑 fecho scope --work .")
        print()

    if not r["groups"]:
        print(r.get("note", "没有新内容"))
        return 0

    for g in r["groups"]:
        head = "%s · %s · %d 条消息 ≈ %d tokens" % (
            g["date"], g["project"], g["messages"], g["tokens_in"])
        if g.get("chunks", 1) > 1:
            head += " · 切 %d 块" % g["chunks"]
        if g["bound"]:
            head += " · 绑定 %s" % g["bound"]
        print(head)
        if g.get("error"):
            print("  ! LLM 失败：%s" % g["error"])
            continue
        icon = {"done": "✓", "pitfall": "⚠", "decision": "◆"}
        for e in g["entries"]:
            dup = "（重复，未写入）" if e["verdict"] == "duplicate" else ""
            print("  %s %s" % (icon.get(e["kind"], "·"), e["content"]))
            print("      → %s%s" % (e["issue"] or e["task"], dup))
        print()

    if r["dry_run"]:
        print("dry-run：没有写入，水位线也没推进。共 %d tokens 的输入待处理。"
              % r["tokens_in"])
    else:
        print("写入 %d 条进展，消耗输入约 %d tokens。" % (r["recorded"], r["tokens_in"]))
        if r.get("retry_next_time"):
            print("有 %d 个会话的部分内容这次没处理成，水位线已卡在失败处，下次扫描会重来。"
                  % len(r["retry_next_time"]))
    return 0


def cmd_dedupe(args) -> int:
    """清理扫描重跑留下的复述条目。默认只看不删，加 --apply 才动手。

    只处理 ingestion_method='transcript-scan' 的条目——agent 主动记的内容里，
    措辞相似可能是真实的不同进展，不在这个命令的射程内。
    """
    from . import match

    db.init()
    sql = ("SELECT update_id, task_id, date, content_md, created_at FROM updates"
           " WHERE ingestion_method='transcript-scan' AND status='active'")
    params = []
    if args.date:
        sql += " AND date=?"
        params.append(args.date)
    sql += " ORDER BY task_id, date, created_at"
    with db.cursor() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    kept, drop = {}, []
    for r in rows:
        key = (r["task_id"], r["date"])
        for k in kept.setdefault(key, []):
            if match.similarity(r["content_md"], k["content_md"]) >= config.SCAN_DEDUPE_SIMILARITY:
                drop.append((r, k))
                break
        else:
            kept[key].append(r)

    if not drop:
        print("没有需要清理的复述条目（共检查 %d 条扫描进展）。" % len(rows))
        return 0

    print("发现 %d 条复述（保留先写入的那条）：\n" % len(drop))
    for r, k in drop[:20]:
        print("  删 %s" % r["content_md"][:58])
        print("  留 %s\n" % k["content_md"][:58])
    if len(drop) > 20:
        print("  …另有 %d 条\n" % (len(drop) - 20))

    if not args.apply:
        print("这是预览。确认没问题再加 --apply 真删。")
        return 0

    with db.cursor() as conn:
        conn.executemany("UPDATE updates SET status='superseded' WHERE update_id=?",
                         [(r["update_id"],) for r, _ in drop])
    print("已把 %d 条标记为 superseded（没有物理删除，随时可查）。" % len(drop))
    return 0


def cmd_hidden(args) -> int:
    """看被去重挡掉的进展，必要时捞回来。

    去重是纯字符串判断，没有后续环节能纠错。所以判为重复的照样存库、只是不进
    日报——但存了没人看得见等于没存，这个命令就是那个入口。
    """
    db.init()
    sql = ("SELECT u.update_id, u.date, u.content_md, u.status, u.meta,"
           " COALESCE(t.issue_key,'自由任务') AS belongs FROM updates u"
           " JOIN tasks t ON t.task_id=u.task_id"
           " WHERE u.status IN ('duplicate-ignored','superseded')")
    params = []
    if args.date:
        sql += " AND u.date=?"
        params.append(args.date)
    sql += " ORDER BY u.date, u.created_at"
    with db.cursor() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if args.restore:
        with db.cursor() as conn:
            n = conn.execute("UPDATE updates SET status='active' WHERE update_id=?",
                             (args.restore,)).rowcount
        print("已恢复 %d 条，下次出日报会带上它。" % n if n else "没找到这条：%s" % args.restore)
        return 0 if n else 1

    if not rows:
        print("没有被挡掉的进展。")
        return 0

    print("被挡掉的进展（存着，但不进日报）：\n")
    for r in rows:
        why = "近似重复" if r["status"] == "duplicate-ignored" else "dedupe 清理"
        print("  %s  %s  [%s]" % (r["date"], r["belongs"], why))
        print("    %s" % r["content_md"][:76].replace("\n", " "))
        print("    id=%s\n" % r["update_id"])
    print("判错了就捞回来：fecho hidden --restore <id>")
    return 0


def cmd_web(args) -> int:
    """起本机 HTTP 服务：dashboard + MCP over HTTP。

    两样东西一个进程托着——都需要「有个 HTTP 服务能读到这份 SQLite」，而数据在
    本机、扫描要读本机的对话记录，所以服务也得在本机。
    """
    from . import web

    scheme = "http://%s:%d" % (args.host, args.port)
    print("dashboard  %s/" % scheme)
    print("MCP 端点   %s/mcp" % scheme)
    if args.host in ("127.0.0.1", "localhost"):
        print("\n只监听本机。要从 ChatGPT 连，先设 FECHO_WEB_TOKEN，再用隧道把它暴露出去：")
        print("  cloudflared tunnel --url %s" % scheme)
    elif not web.TOKEN:
        print("\n⚠️  监听了非本机地址却没设 FECHO_WEB_TOKEN —— 拒绝启动。")
    web.serve(host=args.host, port=args.port, open_browser=not args.no_browser)
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


def cmd_export_shared_config(args) -> int:
    """摘出团队共享的 LLM 配置，给同事 onboarding 用。

    只导 LLM 那几个字段——Mobius token 是一人一份的身份凭证，绝不能跟着走。
    """
    from . import onboarding

    values = {k: config.load().get(k) for k in onboarding.LLM_FIELDS}
    values = {k: v for k, v in values.items() if v not in (None, "")}
    missing = onboarding.REQUIRED_LLM_FIELDS - set(values)
    if missing:
        print("本机 LLM 还没配全，缺：%s" % "、".join(sorted(missing)), file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(out, 0o600)                      # 里面有 key，别让同组可读

    print("已导出 %s（权限 600）" % out)
    print("包含：%s" % "、".join(sorted(values)))
    print("\n⚠️  这个文件里有 API key，而代码仓库是公开的。")
    print("    私聊发给同事或放内网盘，不要发群、不要进 Git、不要传公开网盘。")
    return 0


def cmd_schedule(args) -> int:
    from . import automation

    if args.action == "install":
        _p(automation.install_schedule(args.time))
    elif args.action == "status":
        _p(automation.status())
    elif args.action == "run-now":
        _p(automation.run_now())
    elif args.action == "tick":
        _p(automation.tick())
    elif args.action == "uninstall":
        _p(automation.uninstall_schedule())
    return 0


def cmd_onboard(args) -> int:
    from . import onboarding

    result = onboarding.onboard(
        author=args.author or config.AUTHOR,
        display_name=args.display_name,
        work_prefixes=args.work_prefix or (),
        ignores=args.ignore or (),
        mobius_assignee=args.mobius_assignee,
        daily_time=args.time,
        shared_file=args.shared_config,
        shared_url=args.shared_config_url,
        skip_mobius=args.skip_mobius,
        skip_schedule=args.skip_schedule,
        dry_run=args.dry_run,
    )
    _p(result)
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
    p.add_argument("--alias", help="日报里这个 issue 显示成什么短名，如 fecho")
    p.add_argument("--list", action="store_true", help="看现有绑定")
    p.add_argument("--remove", help="解除某条绑定")
    p.set_defaults(fn=cmd_bind)

    p = sub.add_parser("scan", help="读会话记录自动记进展（cron 第一步）")
    p.add_argument("--days", type=int, default=1, help="最多往回看几天，默认 1")
    p.add_argument("--dry-run", action="store_true", help="只看会记出什么，不写库不推水位线")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("dedupe", help="清理扫描重跑留下的复述条目（默认只看不删）")
    p.add_argument("--date", help="只处理某天")
    p.add_argument("--apply", action="store_true", help="真的执行，不加就只预览")
    p.set_defaults(fn=cmd_dedupe)

    p = sub.add_parser("web", help="起 dashboard + MCP over HTTP（本机）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8900)
    p.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("hidden", help="看被去重挡掉的进展（判错了可以捞回来）")
    p.add_argument("--date", help="只看某天")
    p.add_argument("--restore", metavar="ID", help="把某条恢复成正常进展")
    p.set_defaults(fn=cmd_hidden)

    p = sub.add_parser("digest", help="日终整理（cron 入口，配了 collector 会顺带推送）")
    p.add_argument("--date")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("team", help="看团队某天的日报（需要配好 collector）")
    p.add_argument("--date")
    p.add_argument("--author", help="只看某人")
    p.set_defaults(fn=cmd_team)

    sub.add_parser("install", help="打印 MCP 配置片段").set_defaults(fn=cmd_install)

    p = sub.add_parser("onboard", help="一次完成共享配置、白名单、Agent、Mobius 和自动任务")
    p.add_argument("--author", help="稳定英文标识；默认使用当前 Fecho 身份")
    p.add_argument("--display-name", help="Dashboard 和日报展示名")
    p.add_argument("--work-prefix", action="append", help="允许扫描的工作目录前缀，可重复")
    p.add_argument("--ignore", action="append", help="明确禁止扫描的目录，可重复")
    p.add_argument("--mobius-assignee", help="Mobius 邮箱")
    p.add_argument("--time", default="21:00", help="每日北京时间 HH:MM，必须早于 22:00")
    p.add_argument("--shared-config", help="管理员提供的共享 LLM JSON 文件")
    p.add_argument("--shared-config-url", help="管理员提供的共享 LLM HTTPS 地址")
    p.add_argument("--skip-mobius", action="store_true", help="暂不进行浏览器 OAuth")
    p.add_argument("--skip-schedule", action="store_true", help="暂不安装自动任务")
    p.add_argument("--dry-run", action="store_true", help="只校验并展示计划，不修改配置")
    p.set_defaults(fn=cmd_onboard)

    p = sub.add_parser("export-shared-config",
                       help="导出团队共享的 LLM 配置，给同事 onboarding 用（含密钥）")
    p.add_argument("--out", default="~/.fecho/shared-llm-config.json",
                   help="输出路径，默认 ~/.fecho/shared-llm-config.json")
    p.set_defaults(fn=cmd_export_shared_config)

    p = sub.add_parser("schedule", help="管理北京时间日终任务和本地 Dashboard 后台服务")
    schedule_sub = p.add_subparsers(dest="action", required=True)
    q = schedule_sub.add_parser("install", help="安装自动任务和 Dashboard 服务")
    q.add_argument("--time", default="21:00", help="北京时间 HH:MM，必须早于 22:00")
    for action, help_text in (
        ("status", "查看调度与后台服务状态"),
        ("run-now", "立即执行一次完整日终流水线"),
        ("tick", "由系统每分钟调用的到点检查"),
        ("uninstall", "卸载自动任务和 Dashboard 服务，不删除日志"),
    ):
        schedule_sub.add_parser(action, help=help_text)
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("serve", help="起团队 collector（只收成品，不是共享数据库）")
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--port", type=int, default=config.PORT)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args()
    db.init()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
