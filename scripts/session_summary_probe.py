#!/usr/bin/env python3
"""路线 B 的只读原型：从 agent 会话记录里抽出「今天做成了什么」。

只读——不写库、不改配置、不动 Mobius。跑完只打印，让人先判断质量值不值得做。

用法：
  python3 scripts/session_summary_probe.py --date 2026-09-03
  python3 scripts/session_summary_probe.py --date 2026-09-04 --match

要点：
  · 按**本地时间**切片。transcript 时间戳是 UTC，直接按 UTC 日期切会把一天
    从早上 9 点截断（KST 场景），这是那种上线三天后才发现日报少半天的 bug。
  · 过滤掉 tool_use / tool_result。它们占了文件 90% 的体积，对「我们做了什么」
    几乎没有信息量，喂进去纯烧钱。
  · 扫 ~/.claude/projects/ 下**所有**项目，不只当前仓库——你今天在别的仓库
    干的活也该进工作日志。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def local_tz() -> timezone:
    return datetime.now().astimezone().tzinfo


def iter_records(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


# 宿主注入的样板：slash command 的展开、skill 的说明文档、系统提醒。
# 这些以 user 身份出现在记录里，但根本不是用户说的话，喂进去纯烧钱还带偏总结。
_BOILERPLATE = re.compile(
    r"<command-(message|name|args)>|<system-reminder>|<local-command-|"
    r"Base directory for this skill:|^#+ Quick start|<user-prompt-submit-hook>",
    re.MULTILINE,
)


def text_of(rec: Dict[str, Any]) -> str:
    """只取人话，丢掉 tool_use / tool_result / thinking / 宿主注入的样板。"""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        body = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return ""
    if _BOILERPLATE.search(body):
        return ""
    return body


def collect(day: str, tz, only: List[str] = None, skip: List[str] = None) -> List[Dict[str, Any]]:
    """把某个本地日期当天的对话按时间排好。

    默认扫所有项目——你今天在别的仓库干的活也该进工作日志。但私人目录该不该
    进「工作」日志、该不该送到公司的 LLM 端点，是个需要人来定的边界，所以
    留了 only / skip 两个口子。
    """
    from fecho import scope as _scope

    out, seen, skipped = [], {}, {}
    for path in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        project = path.parent.name
        if only and not any(k in project for k in only):
            continue
        if skip and any(k in project for k in skip):
            continue
        for rec in iter_records(path):
            ts = rec.get("timestamp")
            if not ts or rec.get("type") not in ("user", "assistant"):
                continue
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
            if when.strftime("%Y-%m-%d") != day:
                continue
            body = text_of(rec).strip()
            if not body:
                continue
            cwd = rec.get("cwd") or project
            verdict = seen.get(cwd)
            if verdict is None:
                verdict = _scope.classify(cwd)[0]
                seen[cwd] = verdict
            if verdict != "work":
                # 未登记/已忽略的目录：连内容都不取，更不会进 LLM 请求。
                skipped.setdefault(verdict, set()).add(cwd)
                continue
            out.append({
                "at": when,
                "role": rec["type"],
                "text": body,
                "session": rec.get("sessionId") or path.stem,
                # 目录名是有损编码（work/8-6-x 会编成一串连字符，反解不回来），
                # 记录里的 cwd 才是真实绝对路径。
                "project": rec.get("cwd") or project,
            })
    out.sort(key=lambda r: r["at"])
    return out, skipped


def render(rows: List[Dict[str, Any]], cap: int = 2000) -> str:
    lines = []
    for r in rows:
        body = r["text"]
        if len(body) > cap:                      # 超长回复截断，保留头尾
            body = body[: cap // 2] + "\n…(略)…\n" + body[-cap // 2:]
        who = "用户" if r["role"] == "user" else "agent"
        lines.append("[%s %s] %s" % (r["at"].strftime("%H:%M"), who, body))
    return "\n\n".join(lines)


PROMPT = """你在读一段「人和 coding agent 一起干活」的对话记录，任务是抽出这一天**做成了什么**。

规则：
- 抽的是**成果和进展**，不是对话内容，也不是用户说过的话。
- 一件事一条。同一件事在对话里反复出现，合并成一条。
- **踩的坑、得出的负面结论也算进展**——"试了 X 但发现不行，原因是 Y" 是有价值的记录。
- 只写对话里确实发生的事，不许推断、不许补充没做的事。
- 忽略纯粹的来回确认、纯提问、没有结论的讨论。
- 每条一到两句话，要让人三个月后还能看懂。涉及 issue 号（形如 AI-1234）的带上。

- **不许把"讨论中/倾向于"写成"已决定"**。只有明确拍板的才用"定为/改成/确定"这类词，
  还在比较、还在征求意见的，要写成"在评估 X 和 Y"。

输出格式：一行一条，`类型 | 内容`，类型是 done / pitfall / decision 三者之一。
不要 JSON、不要代码块、不要编号、不要任何解释。内容里随便用什么标点都行。

示例：
done | 把配对引擎写完了，拿真实 issue 测下来 11/12 命中
pitfall | 让模型自己数中文字数会把推理预算烧穿，改成给结构性目标才出得来
decision | 团队方案选了联邦汇总，只推成品不推原始进展

对话记录如下：

"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--match", action="store_true", help="顺便看看每条会配到哪个 Mobius issue")
    ap.add_argument("--dump", action="store_true", help="只打印切片结果，不调 LLM")
    ap.add_argument("--only", action="append", help="只看名字含该关键词的项目，可多次")
    ap.add_argument("--skip", action="append", help="排除名字含该关键词的项目，可多次")
    a = ap.parse_args()

    tz = local_tz()
    rows, skipped = collect(a.date, tz, only=a.only, skip=a.skip)
    if not rows:
        print("%s 当天没有会话记录。" % a.date)
        return 1

    convo = render(rows)
    sessions = {r["session"][:8] for r in rows}
    projects = {r["project"] for r in rows}
    span = (rows[-1]["at"] - rows[0]["at"])

    print("═" * 66)
    print("日期      : %s（本地时区 %s）" % (a.date, datetime.now().astimezone().tzname()))
    print("消息      : %d 条" % len(rows))
    print("会话      : %d 个 · %s" % (len(sessions), ", ".join(sorted(sessions))))
    print("项目      : %s" % ", ".join(sorted(projects)))
    print("时间跨度  : %s → %s（%s）" % (
        rows[0]["at"].strftime("%H:%M"), rows[-1]["at"].strftime("%H:%M"),
        str(span).split(".")[0]))
    print("喂给模型  : %.1f KB ≈ %d tokens" % (len(convo) / 1024, len(convo) / 1.5))
    print("═" * 66)

    if skipped:
        print()
        for verdict, paths in sorted(skipped.items()):
            label = {"unregistered": "未登记，已跳过（只知道路径，没读内容）",
                     "ignored": "已设为忽略"}.get(verdict, verdict)
            print("%s：" % label)
            for pth in sorted(paths):
                print("  %s" % pth)
        if "unregistered" in skipped:
            print("  → 要算工作就在那个目录里跑 fecho scope --work .")
        print()

    if a.dump:
        print(convo[:3000])
        return 0

    from fecho import config, llm, match, mobius

    # 按项目分组，一个项目一次总结。这样每条进展属于哪个项目是**确定的**，
    # 不用让模型猜——它也猜不准（上一版让它标 project，基本没标对）。
    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_project.setdefault(r["project"], []).append(r)

    issues = mobius.cached_issues(config.AUTHOR) if a.match else []
    all_entries: List[Dict[str, Any]] = []

    for project, prows in by_project.items():
        chunk = render(prows)
        bound = match.project_binding(project)
        print("\n%s" % ("─" * 66))
        print("项目 %s%s" % (project, "  [绑定 %s]" % bound if bound else ""))
        print("%d 条消息 · %.1f KB ≈ %d tokens · 调 %s"
              % (len(prows), len(chunk)/1024, len(chunk)/1.5, config.LLM_MODEL))
        print("─" * 66)
        raw = llm.chat([{"role": "user", "content": PROMPT + chunk}],
                       temperature=0.3, max_tokens=4000)
        raw = re.sub(r"^```\w*\s*|\s*```$", "", raw.strip())
        # 不解析 JSON：中文内容里的引号会把 JSON 打断（上一版两个项目全炸在这）。
        # 一行一条、竖线分隔，标点随便用都不影响。
        entries = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-*0123456789. ")
            if "|" not in line:
                continue
            kind, _, content = line.partition("|")
            kind, content = kind.strip().lower(), content.strip()
            if kind in ("done", "pitfall", "decision") and content:
                entries.append({"kind": kind, "content": content})
        if not entries:
            print("没解析出条目，原样打印：\n%s" % raw[:500])
            continue
        icon = {"done": "✓", "pitfall": "⚠", "decision": "◆"}
        for i, e in enumerate(entries, 1):
            e["project"] = project
            all_entries.append(e)
            print("%2d. %s %s" % (i, icon.get(e.get("kind"), "·"), e.get("content", "")))

    entries = all_entries
    if a.match:
        print("\n" + "═" * 66)
        print("如果这些条目走现有的配对引擎（缓存里有 %d 个在办 issue）：\n" % len(issues))
        for e in entries:
            c = e.get("content", "")
            keys = match.extract_issue_keys(c)
            if keys:
                print("  %-9s ← 正文点名   | %s" % (keys[0], c[:46]))
                continue
            best, score = match.best_issue(c, issues)
            if best and score >= config.MATCH_THRESHOLD:
                print("  %-9s ← 自动配 %.2f | %s" % (best["issue_key"], score, c[:46]))
                continue
            bound = match.project_binding(e.get("project") or "")
            if bound:
                print("  %-9s ← 项目绑定    | %s" % (bound, c[:46]))
            else:
                print("  %-9s ← 最高才 %.2f | %s" % ("自由任务", score, c[:46]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
