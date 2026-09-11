"""Fecho MCP server（stdio）—— agent 的入口。

单进程：直接读写本地 SQLite，不需要先起任何服务。
自己实现最小 JSON-RPC，不引第三方 MCP SDK。
stdout 只允许出现 JSON-RPC，日志一律走 stderr。

会话 id：每个 agent 会话各自拉起一个本进程，所以进程实例天然等于一次会话。
它不参与分组（任务才是主干），只做审计和配对时的上下文先验。
"""
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import __version__, config, db, service

PROTOCOL = "2024-11-05"


@dataclass
class MCPContext:
    """Connection-local identity used by every MCP transport."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str = "unknown-agent"
    # 这条连接是谁的。本机版留空，业务函数会退回配置里那个名字；
    # 云端版由 HTTP 层按 token 填上——**每个请求都按 token 重填**，不信任缓存的上下文。
    author: Optional[str] = None


DEFAULT_CONTEXT = MCPContext(session_id=os.getenv("FECHO_SESSION_ID") or str(uuid.uuid4()))


@dataclass
class ToolReply:
    text: str
    structured: Dict[str, Any]


def log(msg: str) -> None:
    sys.stderr.write("[fecho] %s\n" % msg)
    sys.stderr.flush()


TOOLS = [
    {
        "name": "log_progress",
        "description": (
            "记一条工作进展。**推进完一件事就调一次**，不要攒到下班再回忆。\n"
            "记的是「我们做成了什么、进展到哪」，不是对话内容，也不是用户说过的话——"
            "一句能让人看懂结果的话就够。\n"
            "**归属你来判，别让系统猜。** 你手上有这段对话的全部上下文，知道自己在推进"
            "哪件事；系统只能靠字面相似度猜，而干活时说的话（「配对引擎写完了」）"
            "和 issue 标题（「写一个提交工作日志的系统」）常常一个词都不重合。\n"
            "所以：知道是哪个 issue 就填 issue 参数（开工时 catch_up / my_tasks 给过你列表）；"
            "确实不属于任何 issue 就留空，系统归到自由任务。**不确定时留空，别硬填**——"
            "归错了当天日报会跟着错，归不上只是多一个自由任务。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "content": {"type": "string", "description": "做成了什么、进展到哪"},
            "issue": {"type": "string", "description": "可选。确定是哪个 issue 就直接写，如 AI-2541"},
            "task_id": {"type": "string", "description": "可选。强制挂到某个已有任务"},
            "freeform": {"type": "boolean", "description": "明确不属于任何 issue；可覆盖项目目录绑定"},
            "completion_status": {"type": "string", "enum": ["done", "wip", "blocked", "unknown"],
                                  "description": "这项工作的完成状态；不确定就用 unknown"},
            "kind": {"type": "string", "enum": ["progress", "pitfall", "decision"],
                     "description": "内容类型，默认 progress"},
            "date": {"type": "string", "description": "可选，YYYY-MM-DD，补记往日时用"}},
            "required": ["content"]},
    },
    {
        "name": "correct_progress",
        "description": (
            "纠正一条已记录进展的正文或任务归属。使用 log_progress 返回的 update_id；"
            "原地修订，不会制造重复记录。人工确认后的归属会锁定，不再被日报模型覆盖。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "update_id": {"type": "string", "description": "要纠正的进展 ID"},
            "content": {"type": "string", "description": "可选，修正后的正文"},
            "issue": {"type": "string", "description": "可选，修正到已同步的 issue"},
            "task_id": {"type": "string", "description": "可选，修正到已有任务"},
            "freeform": {"type": "boolean", "description": "修正为自由任务"}},
            "required": ["update_id"]},
    },
    {
        "name": "catch_up",
        "description": ("开工时调一次：拉回昨天的日报、口播稿，和现在还挂着的任务。"
                        "适合放在会话开场自动执行。"),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "看哪天的稿，默认昨天"}}},
    },
    {
        "name": "my_tasks",
        "description": "我现在有哪些任务在推进，各自最近一条进展是什么。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "complete_task",
        "description": "把一个本地任务标为已完成；不会回写 Mobius。后续有新进展时会自动重开。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "reopen_task",
        "description": "重新打开一个已完成的本地任务。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "merge_tasks",
        "description": "把误拆出来的源任务合并进目标任务；进展原地移动并保留审计记录。",
        "inputSchema": {"type": "object", "properties": {
            "source_task_id": {"type": "string"},
            "target_task_id": {"type": "string"}},
            "required": ["source_task_id", "target_task_id"]},
    },
    {
        "name": "get_my_log",
        "description": "我今天推进了哪些任务、推到哪了，以及已生成的日报/口播稿（若有）。",
        "inputSchema": {"type": "object", "properties": {"date": {"type": "string"}}},
    },
    {
        "name": "end_of_day",
        "description": ("收工：把今天的进展按任务整理成日报和口播稿（调 LLM）。"
                        "输入没变时不会重复生成。"),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string"}, "force": {"type": "boolean"}}},
    },
    {
        "name": "mobius_login",
        "description": ("连接 Mobius。会打开浏览器让用户授权（OAuth），授权完自动接上，"
                        "不需要用户手动贴任何 key。装好 fecho 后第一件事就调它。"),
        "inputSchema": {"type": "object", "properties": {
            "assignee": {"type": "string",
                         "description": "用户在 Mobius 上的邮箱，用来确定拉谁的 issue"}}},
    },
    {
        "name": "sync_issues",
        "description": "从 Mobius 拉一次在办 issue，刷新配对用的本地缓存。",
        "inputSchema": {"type": "object", "properties": {"assignee": {"type": "string"}}},
    },
    {
        "name": "fecho_doctor",
        "description": ("自检：什么配好了、什么还缺、缺的怎么补。装完先调这个，"
                        "然后按它给的 fix 一步步做完。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "team_digest",
        "description": (
            "看团队某天各自推送过来的日报（需要团队配好共享 collector）。"
            "只有整理好的成品，看不到任何人的原始进展——collector 那边物理上就不存这个。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
            "author": {"type": "string", "description": "可选，只看某人"}}},
    },
]

# 云端版才有的工具。本机版的 tools/list 里看不到它们。
CLOUD_TOOLS = [
    {
        "name": "submit_scan",
        "description": (
            "【夜间扫描专用】把本机扫描聊天记录提取出的候选进展批量交上来。"
            "每条都会走和 log_progress 一样的归属、去重规则；同一个 source_event_key 重复交只记一次；"
            "不在工作文件夹白名单里的会被服务器丢掉。最后一批带 finished=true，服务器记下「这天已扫」。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "这一轮扫的是哪天，YYYY-MM-DD（由「该扫了吗」给出）"},
            "entries": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string", "description": "做成了什么，一两句话"},
                "kind": {"type": "string", "enum": ["done", "pitfall", "decision"]},
                "issue": {"type": "string", "description": "判断属于哪个 issue，不确定就不填"},
                "date": {"type": "string", "description": "这条发生在哪天（北京时间）"},
                "project": {"type": "string", "description": "这段对话所在文件夹的绝对路径"},
                "agent": {"type": "string", "description": "哪个 agent 的聊天记录：claude-code / codex / hermes"},
                "session_id": {"type": "string"},
                "source_event_key": {"type": "string", "description": "稳定的去重键，同一条每次扫都一样"}},
                "required": ["content", "project"]}},
            "finished": {"type": "boolean", "description": "这是最后一批"},
            "error": {"type": "string", "description": "扫描失败时写原因，服务器会半小时后让你再试"}},
            "required": ["entries"]},
    },
    {
        "name": "report_work_folders",
        "description": (
            "【安装时用】上报「用户用 agent 干过活的文件夹」清单，只传绝对路径，不传内容。"
            "从 Claude Code / Codex 的聊天记录里读每段对话所在的目录即可。"
            "上报后用户在网页上勾选哪些算工作（白名单）；新上报的默认不勾。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "folders": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "last_used": {"type": "string"}},
                "required": ["path"]}}},
            "required": ["folders"]},
    },
    {
        "name": "set_work_folders",
        "description": "把工作文件夹白名单设成这一组绝对路径（用户说「把某个目录加进/移出白名单」时用）。"
                       "会整组替换，所以要带上想保留的全部路径。",
        "inputSchema": {"type": "object", "properties": {
            "folders": {"type": "array", "items": {"type": "string"}}},
            "required": ["folders"]},
    },
    {
        "name": "set_daily_time",
        "description": "改每天出日报的时间（北京时间 HH:MM，00:15–21:45）。本机扫描会自动提前 15 分钟。",
        "inputSchema": {"type": "object", "properties": {
            "time": {"type": "string", "description": "如 20:30"}},
            "required": ["time"]},
    },
]

_METHOD_LABEL = {
    "explicit": "你点名了 issue",
    "explicit-freeform": "你明确指定了自由任务",
    "mobius-auto": "自动配到 Mobius issue",
    "task-continue": "接着已有任务",
    "new-task": "新立了自由任务",
}


def _task_line(t: Dict[str, Any]) -> str:
    return "%s %s" % (t["issue_key"], t["title"]) if t.get("issue_key") else t["title"]


def _fmt_day(data: Dict[str, Any], who: str) -> str:
    out = ["%s %s：%d 个任务 / %d 条进展" % (data["date"], who, data["task_count"],
                                            data["update_count"])]
    for t in data["tasks"]:
        out.append("\n## %s" % _task_line(t))
        for u in t["updates"]:
            out.append("- [%s] %s" % (u["source_agent"], u["content_md"].splitlines()[0]))
    return "\n".join(out)


def _fmt_report(r: Dict[str, Any]) -> str:
    out = ["已整理 %s：%d 个任务 / %d 条进展（%s%s）" % (
        r["date"], r["task_count"], r["update_count"], r["generator"],
        "/" + r["model"] if r.get("model") else "")]
    out.append("口播稿 %d 字，约 %d 秒" % (r["voice_chars"], r["voice_seconds_est"]))
    for w in r.get("warnings", []):
        out.append("! " + w)
    out += ["", "=== 日报 ===", r["daily_md"], "", "=== 口播稿 ===", r["voice_md"],
            "", "文件：", "\n".join("  " + p for p in r["files"].values())]
    tp = r.get("team_push")
    if tp:
        out.append("团队：已推送到 collector" if tp.get("pushed") else "团队：未推送（%s）" % tp.get("reason"))
    return "\n".join(out)



def _cloud_call(name: str, args: Dict[str, Any], me: str, context: MCPContext) -> Optional[str]:
    """云端版里做法不同的工具。返回 None 表示照本机版的做法处理。"""
    from . import accounts, cloudscan, jobs, mobius, mobius_login, store

    url = config.PUBLIC_URL
    if name == "submit_scan":
        producer = cloudscan.agent_id(context.client_name) or "scan"
        r = cloudscan.submit(me, args.get("entries") or [], date=args.get("date"),
                             finished=bool(args.get("finished")), error=args.get("error"),
                             producer=producer)
        lines = ["收到：新记 %d 条，重复 %d 条" % (r["recorded"], r["duplicate"])]
        if r["out_of_scope"]:
            lines.append("⚠️ 有 %d 条不在工作文件夹白名单里，已丢弃" % r["out_of_scope"])
        if r["rejected"]:
            lines.append("⚠️ 有 %d 条没记上：%s" % (len(r["rejected"]), r["rejected"][0]["error"]))
        if r["regenerate_queued"]:
            lines.append("这些日期已经出过日报，已排队重新生成：%s" % "、".join(r["regenerate_queued"]))
        if args.get("finished"):
            lines.append("这一轮扫描已标记为%s。" % ("失败，半小时后会再让你试" if args.get("error")
                                                  else "完成"))
        return "\n".join(lines)

    if name == "report_work_folders":
        r = cloudscan.report_folders(me, args.get("folders") or [])
        return ("收到 %d 个文件夹。请让用户打开 %s/onboard 勾选哪些算工作——"
                "没勾的不会被扫描。" % (r["reported"], url))

    if name == "set_work_folders":
        chosen = cloudscan.set_selected(me, args.get("folders") or [])
        return ("工作文件夹白名单现在是：\n" + "\n".join("- " + p for p in chosen)
                if chosen else "白名单已清空——本机扫描不会读任何聊天记录。")

    if name == "set_daily_time":
        t = accounts.set_daily_time(me, args["time"])
        h, m = map(int, t.split(":"))
        scan = h * 60 + m - accounts.SCAN_LEAD_MINUTES
        return "已改：每天 %s 出日报，本机 %02d:%02d 扫描。最多 15 分钟内生效。" % (t, *divmod(scan, 60))

    if name == "end_of_day":
        # 出日报要好几分钟，放在请求里做会超时——排队交给后台程序
        date = args.get("date") or store.today()
        job = jobs.enqueue(me, "regenerate", date)
        return "已排队生成 %s 的日报（%s），几分钟后在 %s 能看到。" % (date, job["status"], url)

    if name == "mobius_login":
        return "云端版在网页上连 Mobius：打开 %s/login 用 Mobius 登录即可。" % url

    if name == "sync_issues":
        token = mobius_login.access_token(me)
        s = mobius.sync(me, assignee=me, token=token)
        return "已同步 %d 个在办 issue。" % s["count"]

    if name == "fecho_doctor":
        user = accounts.get_user(me) or {}
        due = cloudscan.due(me)
        on = [a["label"] for a in cloudscan.agents(me) if a["scan_enabled"]]
        lines = [
            "Fecho %s · 云端版 · %s" % (__version__, me),
            "%s Mobius：%s" % (("✓", "已连接") if mobius_login.connected(me)
                               else ("✗", "未连接，去 %s/login 登录" % url)),
            "✓ 每天 %s 出日报，本机 %s 扫描" % (user.get("daily_time", "21:00"), due["scan_at"]),
            "%s 工作文件夹：%s" % (("✓", "%d 个" % len(due["folders"])) if due["folders"]
                                  else ("✗", "还没选，去 %s/onboard 勾选" % url)),
            "%s 扫描的 agent：%s" % ("✓" if on else "✗", "、".join(on) or "一个都没开"),
            "· 扫描状态：%s" % due["reason"],
        ]
        lines += ["⚠️ " + n for n in due["notices"]]
        return "\n".join(lines)

    if name == "team_digest":
        if not accounts.is_admin(me):
            return "团队汇总只有 admin 能看。需要的话在网页 %s 的 Admin 面板里申请。" % url
        date = args.get("date") or store.today()
        out = ["# %s 团队日报" % date]
        for u in accounts.list_users():
            rep = db.get_report(u["author"], date, "daily")
            out.append("\n## %s\n\n%s" % (u["display_name"],
                                            rep["content_md"] if rep else "（当天没有日报）"))
        return "\n".join(out)

    return None

def call_tool(name: str, args: Dict[str, Any], context: Optional[MCPContext] = None) -> Any:
    context = context or DEFAULT_CONTEXT
    me = context.author or service.whoami()
    cloud_only = {t["name"] for t in CLOUD_TOOLS}
    if config.CLOUD:
        reply = _cloud_call(name, args, me, context)
        if reply is not None:
            return reply
    elif name in cloud_only:
        raise RuntimeError("%s 只有云端版才有" % name)
    if name == "log_progress":
        res = service.record(
            args["content"], author=me, date=args.get("date"),
            source_agent=context.client_name, session_id=context.session_id,
            issue=args.get("issue"), task_id=args.get("task_id"),
            freeform=bool(args.get("freeform")),
            completion_status=args.get("completion_status", "unknown"),
            content_kind=args.get("kind", "progress"))
        t, m = res["task"], res["match"]
        head = ("重复，未写入 → %s" if res["verdict"] == "duplicate" else "已记录 → **%s**") \
            % _task_line(t)
        if m.get("via") == "same-session":
            why = "没有明确线索，按同一对话里刚才那个任务归的"
        elif m.get("score") and m["method"] not in ("explicit", "new-task"):
            why = "%s（相似度 %.2f）" % (_METHOD_LABEL.get(m["method"], m["method"]), m["score"])
        else:
            why = _METHOD_LABEL.get(m["method"], m["method"])
        lines = [head, "配对方式：%s" % why]
        if m.get("confidence") == "low":
            lines.append("⚠️ 这条是猜的。不是同一件事的话，请用 correct_progress 原地纠正。")
        lines.append("进展 ID：%s" % res["update_id"])
        lines.append("今天已推进 %d 个任务。" % res["today_task_count"])
        return ToolReply("\n".join(lines), {
            "update_id": res["update_id"],
            "task_id": t["task_id"],
            "issue_key": t.get("issue_key"),
            "match_method": m["method"],
            "confidence": m.get("confidence", "high"),
            "verdict": res["verdict"],
            "date": res["date"],
        })

    if name == "correct_progress":
        from . import store

        kw: Dict[str, Any] = {}
        if "issue" in args:
            kw["issue_key"] = args["issue"]
        if "task_id" in args:
            kw["task_id"] = args["task_id"]
        if "content" in args:
            kw["content_md"] = args["content"]
        if args.get("freeform"):
            kw["freeform"] = True
        result = store.correct_progress(args["update_id"], me, **kw)
        task = result["task"]
        text = ("已原地修订" if result["changed"] else "无需修订") + " → **%s**" % _task_line(task)
        return ToolReply(text, {
            "changed": result["changed"],
            "update_id": result["update_id"],
            "task_id": task["task_id"],
            "issue_key": task.get("issue_key"),
            "assignment_locked": True,
        })

    if name == "catch_up":
        c = service.catch_up(args.get("date"), author=me)
        out = []
        rep = c["report"]
        if rep.get("daily"):
            out += ["=== %s 的日报 ===" % c["date"], rep["daily"]["content_md"]]
            if rep.get("voice"):
                out += ["", "=== 口播稿 ===", rep["voice"]["content_md"]]
        else:
            out.append("（%s 没有日报）" % c["date"])
        out += ["", "=== 还挂着的任务（%d 个）===" % len(c["open_tasks"])]
        for t in c["open_tasks"]:
            out.append("- %s" % _task_line(t))
            if t.get("last_progress"):
                out.append("    最近：%s" % t["last_progress"].splitlines()[0])
        return "\n".join(out)

    if name == "my_tasks":
        tasks = service.open_tasks(author=me)
        if not tasks:
            return "当前没有在推进的任务。"
        out = ["在推进的任务（%d 个）：" % len(tasks)]
        for t in tasks:
            out.append("- %s（%d 条进展）" % (_task_line(t), t["update_count"]))
            if t.get("last_progress"):
                out.append("    最近：%s" % t["last_progress"].splitlines()[0])
        return "\n".join(out)

    if name in ("complete_task", "reopen_task"):
        result = (service.complete_task(args["task_id"], author=me)
                  if name == "complete_task" else service.reopen_task(args["task_id"], author=me))
        task = result["task"]
        label = "已完成" if name == "complete_task" else "已重新打开"
        return ToolReply("%s → **%s**" % (label, _task_line(task)), {
            "changed": result["changed"], "task_id": task["task_id"],
            "status": task["status"], "issue_key": task.get("issue_key")})

    if name == "merge_tasks":
        result = service.merge_tasks(args["source_task_id"], args["target_task_id"],
                                    author=me)
        task = result["task"]
        return ToolReply("已合并 %d 条进展 → **%s**" % (
            result["moved_updates"], _task_line(task)), {
                "source_task_id": result["source_task_id"],
                "target_task_id": result["target_task_id"],
                "moved_updates": result["moved_updates"]})

    if name == "get_my_log":
        data = service.day(me, args.get("date"))
        out = [_fmt_day(data, "我")]
        rep = service.report(data["date"], author=me)
        if rep.get("daily"):
            out += ["", "--- 已生成日报 ---", rep["daily"]["content_md"]]
            if rep.get("voice"):
                out += ["", "--- 口播稿 ---", rep["voice"]["content_md"]]
        else:
            out += ["", "（尚未生成日报，可调用 end_of_day）"]
        return "\n".join(out)

    if name == "end_of_day":
        r = service.end_of_day(args.get("date"), author=me, force=bool(args.get("force")))
        if r["status"] != "generated":
            return "状态：%s（%s）" % (r["status"], r.get("reason", ""))
        return _fmt_report(r)

    if name == "mobius_login":
        from . import oauth

        url = config.MOBIUS_URL or "https://mobius.feedmob.com/api/mcp"
        if args.get("assignee"):
            config.update(mobius_assignee=args["assignee"])
        config.update(mobius_url=url)
        config.reload_module()
        started = oauth.login(url, open_browser=True, timeout=180)
        out = ["已在浏览器里打开 Mobius 授权页，请让用户点「同意」。",
               "如果没自动打开，把这个链接给用户：", started["authorize_url"], "", "等待授权…"]
        try:
            res = oauth.complete(started["ctx"])
        except oauth.OAuthError as exc:
            return "\n".join(out + ["", "❌ 授权失败：%s" % exc,
                                    "可以重试，或改用 fecho login --token <token>"])
        try:
            s = service.sync_issues()
            out.append("✅ 已连接 Mobius，并同步了 %d 个在办 issue（%s）。" % (s["count"], s["assignee"]))
        except Exception as exc:
            out.append("✅ 已连接 Mobius，但同步 issue 失败：%s" % exc)
            out.append("（如果是不知道拉谁的 issue，再调一次 mobius_login 并带上 assignee 邮箱）")
        out.append("授权有效期约 %d 分钟，过期会自动续。" %
                   max(1, (res.get("expires_at", 0) - int(__import__("time").time())) // 60))
        return "\n".join(out)

    if name == "sync_issues":
        s = service.sync_issues(args.get("assignee"))
        return "已从 Mobius 同步 %d 个在办 issue（%s），配对缓存已刷新。" % (s["count"], s["assignee"])

    if name == "fecho_doctor":
        d = service.doctor()
        out = ["Fecho %s · 作者=%s · 数据在 %s" % (
            __version__, d["config"]["author"], d["config"]["home"])]
        for c in d["checks"]:
            out.append("%s %s — %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
            if c.get("fix"):
                out.append("    → %s" % c["fix"])
        out += ["", "现在能做：记进展 ✓｜自动配 Mobius %s｜出日报 %s｜团队协作 %s" % (
            "✓" if d["ready_to_match"] else "✗", "✓" if d["ready_to_report"] else "✗",
            "✓" if d["ready_for_team"] else "✗（可选）")]
        return "\n".join(out)

    if name == "team_digest":
        from . import push

        try:
            d = service.team_digest(args.get("date"), args.get("author"))
        except push.PushError as exc:
            return "连不上团队 collector：%s" % exc
        if not d["count"]:
            return "%s 团队还没有人推送过报告。" % d["date"]
        out = ["%s 团队日报（%d 人）：" % (d["date"], d["count"])]
        for a in d["authors"]:
            out.append("\n## %s" % a["author"])
            if a.get("daily"):
                out.append(a["daily"]["content_md"])
        return "\n".join(out)

    raise RuntimeError("未知工具: %s" % name)


def handle(msg: Dict[str, Any], context: Optional[MCPContext] = None) -> Optional[Dict[str, Any]]:
    context = context or DEFAULT_CONTEXT
    method, mid = msg.get("method"), msg.get("id")

    if method == "initialize":
        info = (msg.get("params") or {}).get("clientInfo") or {}
        context.client_name = info.get("name") or context.client_name
        if config.CLOUD and context.author:
            from . import cloudscan
            cloudscan.touch_agent(context.author, context.client_name)   # 网页上的「已连接」靠这个
        log("client=%s session=%s home=%s" % (
            context.client_name, context.session_id[:8], config.HOME))
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fecho", "version": __version__}}}

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        tools = TOOLS + CLOUD_TOOLS if config.CLOUD else TOOLS
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            reply = call_tool(params.get("name"), params.get("arguments") or {}, context)
            text = reply.text if isinstance(reply, ToolReply) else reply
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if isinstance(reply, ToolReply):
                result["structuredContent"] = reply.structured
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:
            log("tool error: %s" % exc)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "调用失败：%s" % exc}], "isError": True}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def main() -> None:
    db.init()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    context = MCPContext(session_id=os.getenv("FECHO_SESSION_ID") or str(uuid.uuid4()))
    log("started pid=%d session=%s" % (os.getpid(), context.session_id[:8]))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg, context)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
