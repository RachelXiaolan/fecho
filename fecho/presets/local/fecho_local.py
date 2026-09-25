#!/usr/bin/env python3
"""Fecho 本机采集（云端版）。

同事电脑上只跑这一个文件：不用 pip，只用 Python 自带的库（Python 3.9 就够）。
macOS、Linux、Windows 都能跑。

    install   记下服务地址和 token，逐个试叫醒本机的 agent，上报用 agent 干过活的文件夹，
              装一个每 15 分钟跑一次的定时任务
    check     问服务器「该扫了吗」；该扫就让每个 agent 扫自己的对话、提炼成进展传上去
              （定时任务跑的就是这个）
    status    看看现在的状态

**每个 agent 只扫自己的对话**：Claude Code 的聊天记录交给 claude 命令提炼，Codex 的交给
codex 命令提炼，互不交叉。提炼完各自上传，服务器再把一整天的进展整理成日报。

定时任务：macOS 用 launchd；Linux 优先 systemd 用户定时器，没有就用 cron；
Windows 用任务计划程序。

隐私边界全在本机：
- 只读服务器回给的白名单文件夹里的对话，别的文件夹连内容都不打开
- 工具调用、工具输出、宿主注入的样板一律丢掉，只留人和 agent 说的话
- 上传的只有提炼后的「做成了什么」，不传对话原文

提炼用的是用户自己的 agent，不需要任何 LLM 密钥。

这份文件由服务器分发（__URL__/local/fecho_local.py）。提示词、解析规则和事件键由
fecho/scan_contract.py 统一维护；改动后运行 scripts/build_local_script.py 重新生成本文件。
"""
import argparse
import glob
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# BEGIN GENERATED SCAN CONTRACT

"""Pure transcript-scan rules shared with the standalone client.

Keep this module limited to the stdlib so its source can be embedded in the
single-file scanner distributed to user machines.
"""
import hashlib
import re


DEFAULT_CHUNK_CHARS = 18000
BOILERPLATE_PATTERN = (
    r"<command-(message|name|args)>|<system-reminder>|<local-command-|"
    r"Base directory for this skill:|<user-prompt-submit-hook>"
)
BOILERPLATE = re.compile(BOILERPLATE_PATTERN, re.MULTILINE)

PROMPT = """你在读一段「人和 coding agent 一起干活」的对话记录，任务是抽出这段时间**做成了什么**。

规则：
- 抽的是**成果和进展**，不是对话内容，也不是用户说过的话。
- 一件事一条。同一件事反复出现，合并成一条。
- **踩的坑、得出的负面结论也算进展**——「试了 X 发现不行，因为 Y」是有价值的记录。
- 只写对话里确实发生的事，不许推断、不许补充没做的事。
- 忽略纯粹的来回确认、纯提问、没有结论的讨论。
- **不许把「还在讨论/倾向于」写成「已决定」**。只有明确拍板的才用「定为/改成/确定」，
  还在比较的要写「在评估 X 和 Y」。
- 每条一到两句话，让人三个月后还看得懂。
- **必须用中文写**，无论对话本身是什么语言。技术名词（MCP、OAuth、SQLite 等）保留原文。

还要判断每条进展属于下面哪个 issue：
- 看的是**说的是不是同一件事**，不是字面有没有重合的词。
- 真的都不属于就写 `-`，系统会归到自由任务。**宁可写 `-` 也不要硬凑**——
  归错了下游的日报全跟着错，归不上只是多一个自由任务。
- 只能从下面给的列表里选，不许自己编 issue 号。
- 如果这段没有任何已经完成、推进或明确踩坑的内容，只输出一行 `NONE`。

{issues}

输出格式：一行一条，`类型 | issue号或-| 内容`，类型是 done / pitfall / decision 三者之一。
不要 JSON、不要代码块、不要编号、不要解释。内容里随便用什么标点都行。

示例：
done | AI-2541 | 配对引擎写完了，拿真实 issue 测下来 11/12 命中
pitfall | AI-2541 | 让模型自己数中文字数会把推理预算烧穿，改成给结构性目标才出得来
decision | - | 闲鱼选品定了强推三个品类，盗版资料类全部淘汰

对话记录如下：

"""

NO_ISSUES = "（当前没有在办的 issue，所有条目的 issue 号都写 `-`）"
PROBE_TEXT = "（这是安装时的连通测试，没有对话内容。）"


def parse_scan_entries(raw, valid_keys=None, issue_pattern=None):
    """Parse the line protocol emitted by the transcript summarizer."""
    cleaned = re.sub(r"^```\w*\s*|\s*```$", "", (raw or "").strip())
    if cleaned.upper() == "NONE":
        return []
    out = []
    for line in cleaned.splitlines():
        line = line.strip().lstrip("-*0123456789. ")
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            continue
        kind = parts[0].lower()
        if kind not in ("done", "pitfall", "decision"):
            continue
        issue, content = (parts[1], "|".join(parts[2:]).strip()) if len(parts) >= 3 else ("", parts[1])
        if not content:
            continue
        issue = issue.upper()
        valid_issue = bool(issue_pattern and issue_pattern.fullmatch(issue))
        if valid_issue and valid_keys is not None:
            valid_issue = issue in valid_keys
        out.append({"kind": kind, "content": content,
                    "issue": issue if valid_issue else None})
    return out


def scan_event_key(producer, session_id, part, item_index):
    """Stable idempotency key for one summarized transcript chunk."""
    seed = "|".join((producer, session_id, part[0]["ts"], part[-1]["ts"], str(item_index)))
    return "scan:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()

# END GENERATED SCAN CONTRACT

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

HOME = Path(os.environ.get("FECHO_LOCAL_HOME") or (Path.home() / ".fecho-cloud"))
CONFIG = HOME / "config.json"
LOG = HOME / "check.log"
LOCK = HOME / "check.lock"
DONE_DIR = HOME / "done"
NOTIFIED = HOME / "notified.json"

# 定时任务
LABEL = "com.feedmob.fecho.cloud"                                        # macOS launchd
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
SYSTEMD_UNIT = "fecho-cloud"                                             # Linux systemd
SYSTEMD_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"
CRON_MARK = "# fecho-cloud"                                              # Linux cron
WINDOWS_TASK = "FechoCloudCheck"                                         # Windows 任务计划程序

# 中国没有夏令时，固定 +8 就是北京时间。不用 zoneinfo：有些机器上没有时区数据库。
BEIJING = timezone(timedelta(hours=8))
CHECK_EVERY_SECONDS = 15 * 60
CHUNK_CHARS = DEFAULT_CHUNK_CHARS
UPLOAD_BATCH = 50
AGENT_TIMEOUT = 600
LOCK_STALE_SECONDS = 2 * 60 * 60

# 每个 agent 的聊天记录在哪。装的时候用来判断「这台电脑上用过哪些 agent」；
# 扫描时以服务器回给的为准。~ 在 Windows 上就是 C:\Users\<你>。
TRANSCRIPTS = {
    "claude-code": "~/.claude/projects/*/*.jsonl",
    "codex": "~/.codex/sessions/*/*/*/*.jsonl",
    "hermes": "~/.hermes/sessions/**/*.jsonl",
}


def _npm_global(name):
    """Windows 上 npm 全局装的命令行在 %APPDATA%\\npm 下，是个 .cmd。"""
    appdata = os.environ.get("APPDATA")
    return [os.path.join(appdata, "npm", name + ".cmd")] if appdata else []


def _local_programs(*parts):
    local = os.environ.get("LOCALAPPDATA")
    return [os.path.join(local, "Programs", *parts)] if local else []


# 能在后台叫醒来提炼的 agent，以及去哪找它的命令行。先找 PATH，再找这些常见位置。
# 不在 PATH 上的也要找：ChatGPT 桌面版把 codex 命令行藏在 app 包里。
# Windows 上桌面版 app 的位置没有实测过，只是常见安装位置，找不到就跳过。
CLI = {
    "claude-code": {"name": "claude", "also": [
        "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
        "~/.local/bin/claude", "/usr/bin/claude",
        "~/.claude/local/claude.exe", "~/.local/bin/claude.exe",
    ] + _npm_global("claude")},
    "codex": {"name": "codex", "also": [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        "/opt/homebrew/bin/codex", "/usr/local/bin/codex", "~/.local/bin/codex", "/usr/bin/codex",
    ] + _npm_global("codex")
      + _local_programs("ChatGPT", "resources", "codex.exe")
      + _local_programs("Codex", "resources", "codex.exe")},
}
FIX_HINT = {
    "claude-code": "在终端里运行 claude，看它能不能正常对话。用 Claude 官方订阅的，进去后输入 /login 登录；"
                   "用 CC Switch 接其他线路的，在 CC Switch 里给 Claude 选一个能用的线路。",
    "codex": "打开 ChatGPT 桌面版确认已登录，或在终端运行 codex login。",
}

# ---------- 小工具 ----------

def log(msg):
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 1024 * 1024:
            LOG.replace(LOG.with_suffix(".log.1"))
    except OSError:
        pass
    line = "[%s] %s" % (datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"), msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    # 定时任务把屏幕输出也导进了同一个日志文件，再 print 一遍每行就会出现两次。
    # 只有人在终端里手动跑的时候才打到屏幕上。
    # Windows 用 pythonw 跑定时任务时根本没有 stdout（是 None），不能直接调 isatty。
    stream = sys.stdout
    if stream is not None and hasattr(stream, "isatty") and stream.isatty():
        print(line, flush=True)


def load_config():
    if not CONFIG.exists():
        raise SystemExit("还没装：先运行 install --url <服务地址> --token <token>（脚本在 %s）"
                         % Path(__file__).resolve())
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_json(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)       # Windows 上 chmod 基本不起作用，也不报错
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    tmp.replace(path)


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__("%s %s" % (status, message))
        self.status = status


def api(cfg, method, path, body=None, timeout=60):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"].rstrip("/") + path, data=data, method=method,
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Content-Type": "application/json", "User-Agent": "fecho-local"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(text)
            text = detail.get("detail") or detail.get("error") or text
        except ValueError:
            pass
        raise ApiError(exc.code, str(text)[:300])


def beijing(ts):
    """对话记录里的时间戳（UTC、带 Z）→ 北京时间。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")[:32])
    except ValueError:
        try:
            when = datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(BEIJING)


# ---------- 路径规则（和服务器 cloudscan.py 同一套） ----------

_DRIVE = re.compile(r"^[A-Za-z]:(/|$)")


def norm_path(path):
    """统一成正斜杠、去掉末尾斜杠。Windows 的 C:\\Users\\a 变成 C:/Users/a。"""
    path = (path or "").strip().replace("\\", "/")
    if len(path) > 1 and not re.fullmatch(r"[A-Za-z]:/", path):
        path = path.rstrip("/")
    return path


def is_absolute(path):
    """以 / 开头（macOS、Linux），或以盘符开头（Windows 的 C:/）。"""
    path = norm_path(path)
    return path.startswith("/") or bool(_DRIVE.match(path))


def _compare_key(path):
    # Windows 的路径不分大小写，C:/Users 和 c:/users 是同一个文件夹
    return path.lower() if _DRIVE.match(path) else path


def in_scope(path, allowed):
    """这个目录在白名单里吗（等于某个白名单文件夹，或在它下面）。和服务器同一条规则。"""
    path = norm_path(path)
    if not path:
        return False
    key = _compare_key(path)
    for folder in allowed:
        folder = norm_path(folder)
        if not folder:
            continue
        fkey = _compare_key(folder)
        if key == fkey or key.startswith(fkey.rstrip("/") + "/"):
            return True
    return False


def is_temp_path(path):
    """临时目录里的对话不算干过活的文件夹。"""
    p = norm_path(path).lower()
    return (p.startswith(("/private/tmp", "/tmp/", "/var/folders/"))
            or p == "/tmp" or "/appdata/local/temp" in p)


# ---------- 读对话记录（和 scan.py 同一套规则） ----------

def iter_records(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def parts_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(p.get("text", "")) for p in content
        if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))


def claude_text(rec):
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        # 只取 text：tool_use / tool_result / thinking 全部丢掉——那是大头，也最可能带敏感内容
        body = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    return "" if BOILERPLATE.search(body) else body


def normalized_records(agent, path):
    """把 Claude / Codex / Hermes 的 JSONL 收敛成同一种最小结构。"""
    path = Path(path)
    if agent == "claude-code":
        out = []
        for rec in iter_records(path):
            if rec.get("type") not in ("user", "assistant"):
                continue
            out.append({"timestamp": rec.get("timestamp"), "role": rec.get("type"),
                        "cwd": rec.get("cwd") or "", "text": claude_text(rec),
                        "entrypoint": rec.get("entrypoint") or ""})
        return path.stem, out

    cwd, sid, out = "", path.stem, []
    for rec in iter_records(path):
        payload = rec.get("payload") or {}
        if rec.get("type") == "session_meta":
            cwd = payload.get("cwd") or cwd
            sid = payload.get("id") or payload.get("session_id") or sid
            continue
        if rec.get("type") == "turn_context":
            cwd = payload.get("cwd") or cwd
            continue
        if rec.get("type") == "response_item" and payload.get("type") == "message":
            role, content = payload.get("role"), payload.get("content")
        elif isinstance(rec.get("message"), dict):
            role = rec["message"].get("role") or rec.get("type")
            content = rec["message"].get("content")
        else:
            role, content = rec.get("role"), rec.get("content")
        if role not in ("user", "assistant"):
            continue
        body = parts_text(content)
        if BOILERPLATE.search(body):
            body = ""
        out.append({"timestamp": rec.get("timestamp") or rec.get("created_at"), "role": role,
                    "cwd": rec.get("cwd") or payload.get("cwd") or cwd, "text": body})
    return "%s:%s" % (agent, sid), out


def collect(date, folders, agent, transcripts):
    """某一个 agent 的聊天记录里，白名单文件夹中、北京时间 date 这天的对话。

    按 (会话, 文件夹) 分组。只读这一个 agent 自己的记录。
    """
    day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=BEIJING)
    groups = {}
    for value in glob.glob(os.path.expanduser(transcripts), recursive=True):
        try:
            # 那天开始之前就没再动过的文件，不可能有那天的对话，连打开都不用
            if datetime.fromtimestamp(os.path.getmtime(value), BEIJING) < day_start:
                continue
        except OSError:
            continue
        session_id, records = normalized_records(agent, value)
        for rec in records:
            when = beijing(rec.get("timestamp"))
            if not when or when.strftime("%Y-%m-%d") != date:
                continue
            cwd = rec.get("cwd") or ""
            if not in_scope(cwd, folders):
                continue          # 不在白名单：内容不取，更不会交给 agent
            body = (rec.get("text") or "").strip()
            if not body:
                continue
            groups.setdefault((session_id, cwd), []).append(
                {"at": when, "ts": rec["timestamp"], "role": rec["role"], "text": body,
                 "entrypoint": rec.get("entrypoint") or ""})
    for rows in groups.values():
        rows.sort(key=lambda r: r["at"])
    return groups


def split(rows, limit=CHUNK_CHARS):
    out, cur, size = [], [], 0
    for r in rows:
        n = len(r["text"]) + 24
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def render(rows, cap=2000):
    out = []
    for r in rows:
        body = r["text"]
        if len(body) > cap:
            body = body[: cap // 2] + "\n…(略)…\n" + body[-cap // 2:]
        out.append("[%s %s] %s" % (r["at"].strftime("%H:%M"),
                                   "用户" if r["role"] == "user" else "agent", body))
    return "\n\n".join(out)


def parse_entries(raw):
    """本机版本不保存 issue 归属；解析协议由 scan_contract 统一维护。"""
    cleaned = (raw or "").strip()
    if cleaned.upper() == "NONE":
        return []
    entries = parse_scan_entries(cleaned)
    if not entries:
        raise ValueError("agent 的输出不符合约定格式：%s" % cleaned[:120])
    return [{"kind": item["kind"], "content": item["content"]} for item in entries]


def event_key(agent, session_id, part, index):
    return scan_event_key(agent, session_id, part, index)


# ---------- 叫醒本机 agent 来提炼 ----------

def find_cli(agent):
    spec = CLI.get(agent)
    if not spec:
        return None
    found = shutil.which(spec["name"])      # Windows 上会自动带上 .cmd / .exe
    if found:
        return found
    for candidate in spec["also"]:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _no_window():
    """Windows 上别给每次调用都弹一个黑色命令行窗口。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def run_agent(cfg, agent, text):
    """让这个 agent 的命令行读一段文字、吐出几行结果。"""
    cli = (cfg.get("runners") or {}).get(agent)
    if not cli:
        raise RuntimeError("本机没有能叫醒的 %s" % agent)
    env = dict(os.environ)
    if cfg.get("path"):
        env["PATH"] = cfg["path"]        # 定时任务里的 PATH 很短，找不到 node 之类的
    prompt = PROMPT.replace("{issues}", NO_ISSUES) + text
    # 一定按 UTF-8 传：Windows 中文系统默认是 GBK，中文提示词会报错或变乱码
    common = {"input": prompt, "capture_output": True, "text": True, "encoding": "utf-8",
              "errors": "replace", "timeout": AGENT_TIMEOUT, "cwd": str(HOME), "env": env,
              "creationflags": _no_window()}
    if agent == "claude-code":
        # 不带工具、不加载 MCP、不留会话记录。
        # 不留记录很要紧，否则这次提炼本身会变成一段新对话，下次又被扫进来。
        args = [cli, "-p", "--output-format", "text", "--no-session-persistence",
                "--strict-mcp-config", "--tools", ""]
        r = subprocess.run(args, **common)
        if r.returncode:
            raise RuntimeError("claude 退出码 %d：%s" % (
                r.returncode, (r.stderr or r.stdout).strip()[-300:]))
        return r.stdout
    if agent == "codex":
        # --ephemeral 同样是为了不留会话记录；只读沙箱，它什么都改不了
        out = HOME / "codex-last.txt"
        if out.exists():
            out.unlink()
        args = [cli, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                "-o", str(out), "-"]
        r = subprocess.run(args, **common)
        if r.returncode:
            raise RuntimeError("codex 退出码 %d：%s" % (
                r.returncode, (r.stderr or r.stdout).strip()[-300:]))
        return out.read_text(encoding="utf-8") if out.exists() else r.stdout
    raise RuntimeError("还不支持在后台叫醒 %s" % agent)


def probe(cfg, agent):
    """装的时候真叫醒一次。叫不醒就别装——不然每晚悄悄失败，谁也不知道。"""
    try:
        parse_entries(run_agent(cfg, agent, PROBE_TEXT))
        return True, "能叫醒"
    except subprocess.TimeoutExpired:
        return False, "等了 %d 秒没回话" % AGENT_TIMEOUT
    except Exception as exc:                  # noqa: BLE001
        return False, str(exc)[:200]


# ---------- 系统通知 ----------

_WINDOWS_BALLOON = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; "
    "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
    "$n.ShowBalloonTip(8000, 'Fecho', $env:FECHO_NOTIFY_TEXT, 'Info'); "
    "Start-Sleep -Seconds 9; $n.Dispose()")


def notify_command(text):
    """弹一条系统通知要跑的命令。通知文字只走参数或环境变量，**不拼进命令本身**——
    服务器传回来的文字里要是带了引号或命令，拼进去就会被当成命令执行。"""
    if IS_MAC:
        return (["osascript", "-e", "on run argv", "-e",
                 'display notification (item 1 of argv) with title "Fecho"', "-e", "end run", text],
                None)
    if IS_WINDOWS:
        env = dict(os.environ, FECHO_NOTIFY_TEXT=text)
        return (["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _WINDOWS_BALLOON],
                env)
    if shutil.which("notify-send"):
        return (["notify-send", "Fecho", text], None)
    return None, None


def notify(notices):
    """服务器要提醒的事（比如日报没生成成功），用系统通知弹出来。同一条只弹一次。"""
    if not notices:
        return
    try:
        seen = set(json.loads(NOTIFIED.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()
    fresh = [n for n in notices if n not in seen]
    for text in fresh:
        log("通知：%s" % text)
        args, env = notify_command(text)
        if not args:
            continue
        try:
            subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_no_window())
        except OSError:
            pass                              # 弹不出来不要紧，日志里已经记着了
    if fresh:
        save_json(NOTIFIED, sorted(seen | set(fresh))[-50:])


# ---------- check：定时任务每 15 分钟跑的 ----------

def acquire_lock():
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime > LOCK_STALE_SECONDS:
            LOCK.unlink()                 # 上次跑崩了留下的
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def check(cfg, force_date=None):
    due = api(cfg, "GET", "/api/scan/due")
    notify(due.get("notices"))
    if force_date:
        due = dict(due, due=True, date=force_date)
    if not due.get("due"):
        log("不扫：%s" % due.get("reason"))
        return 0
    if not acquire_lock():
        log("上一轮还没跑完，这次跳过")
        return 0
    try:
        return scan_day(cfg, due)
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def scan_day(cfg, due):
    """每个开着扫描的 agent，用它自己的命令行扫它自己的对话。"""
    date = due["date"]
    folders = due.get("folders") or []
    log("开始扫 %s（白名单 %d 个文件夹）" % (date, len(folders)))

    # 这一天已经提炼并上传成功的段落记在本机，失败重试时不再重复花 token
    done_file = DONE_DIR / ("%s.json" % date)
    try:
        done = set(json.loads(done_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        done = set()

    errors, uploaded, pieces = [], 0, 0
    for spec in due.get("agents") or []:
        agent = spec["agent"]
        if not (cfg.get("runners") or {}).get(agent):
            # 服务器上开着，本机却叫不醒：必须算失败，不能悄悄当成「今天没对话」
            errors.append("%s 在网页上开着扫描，但这台电脑叫不醒它" % agent)
            log("跳过 %s：本机叫不醒它。%s" % (agent, FIX_HINT.get(agent, "")))
            continue
        for (session_id, cwd), rows in sorted(collect(date, folders, agent, spec["transcripts"]).items()):
            for part in split(rows):
                seed = event_key(agent, session_id, part, -1)
                if seed in done:
                    continue
                pieces += 1
                try:
                    items = parse_entries(run_agent(cfg, agent, render(part)))
                except Exception as exc:          # noqa: BLE001 一段失败不拖累别的段
                    errors.append("%s/%s: %s" % (agent, Path(norm_path(cwd)).name, str(exc)[:160]))
                    log("%s 提炼失败 %s：%s" % (agent, cwd, str(exc)[:200]))
                    continue
                # Claude Code 桌面版和命令行共用一份记录，标一下这段是从哪个入口聊的
                entrypoint = next((r["entrypoint"] for r in part if r.get("entrypoint")), "")
                entries = [dict({"content": it["content"], "kind": it["kind"], "date": date,
                                 "project": cwd, "agent": agent, "session_id": session_id,
                                 "source_event_key": event_key(agent, session_id, part, i)},
                                **({"entrypoint": entrypoint} if entrypoint else {}))
                           for i, it in enumerate(items)]
                for start in range(0, len(entries), UPLOAD_BATCH):
                    r = api(cfg, "POST", "/api/scan/submit",
                            {"date": date, "entries": entries[start:start + UPLOAD_BATCH]})
                    uploaded += int(r.get("recorded") or 0)
                done.add(seed)
                save_json(done_file, sorted(done))

    error = "；".join(errors)[:500] if errors else None
    api(cfg, "POST", "/api/scan/submit",
        {"date": date, "entries": [], "finished": True, "error": error})
    log("扫完 %s：%d 段对话，新记 %d 条%s" % (
        date, pieces, uploaded, "；有失败，服务器会半小时后让我重试" if error else ""))
    return 1 if error else 0


# ---------- 定时任务：macOS launchd / Linux systemd 或 cron / Windows 任务计划程序 ----------
#
# 每种都拆成「生成内容」（纯函数，好测）和「写进系统」两步。
# 电脑合着错过扫描时间不用定时器操心：服务器的「该扫了吗」会让第二天早上先补昨天。

def _systemd_user_available():
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def schedule_kind():
    """这台电脑用哪种定时任务。一个都没有就是 None。"""
    if IS_MAC:
        return "launchd"
    if IS_WINDOWS:
        return "schtasks"
    if _systemd_user_available():
        return "systemd"
    if shutil.which("crontab"):
        return "cron"
    return None


SCHEDULE_LABEL = {"launchd": "macOS launchd", "systemd": "systemd 用户定时器",
                  "cron": "cron", "schtasks": "Windows 任务计划程序"}


def launchd_plist(python, script):
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, str(script), "check"],
        "StartInterval": CHECK_EVERY_SECONDS,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }, sort_keys=True)


def systemd_units(python, script):
    service = (
        "[Unit]\n"
        "Description=Fecho 本机采集：问服务器该不该扫，该扫就扫\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        'ExecStart="%s" "%s" check\n'
        "StandardOutput=append:%s\n"
        "StandardError=append:%s\n" % (python, script, LOG, LOG))
    timer = (
        "[Unit]\n"
        "Description=每 15 分钟跑一次 Fecho 本机采集\n\n"
        "[Timer]\n"
        "OnCalendar=*:0/15\n"
        # 合盖错过的那次，开机后补跑
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n")
    return {SYSTEMD_UNIT + ".service": service, SYSTEMD_UNIT + ".timer": timer}


def cron_line(python, script):
    return '*/15 * * * * "%s" "%s" check >> "%s" 2>&1 %s' % (python, script, LOG, CRON_MARK)


def windows_python(python):
    """定时任务用 pythonw.exe：不弹黑色命令行窗口。找不到就退回原来的。"""
    # 按字符串切，不用 os.path：在别的系统上测这段时 os.path 不认 Windows 的反斜杠
    folder, _, name = python.replace("/", "\\").rpartition("\\")
    if folder and name.lower() == "python.exe":
        candidate = folder + "\\pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return python


def schtasks_create_args(python, script):
    return ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "15", "/TN", WINDOWS_TASK,
            "/TR", '"%s" "%s" check' % (windows_python(python), script)]


def _run(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(args, **kw)


def _crontab_lines():
    r = _run(["crontab", "-l"])
    if r.returncode:                      # 从来没有过 crontab 时会返回非 0
        return []
    return [line for line in (r.stdout or "").splitlines()]


def _write_crontab(lines):
    text = "\n".join(lines).rstrip("\n") + "\n"
    r = _run(["crontab", "-"], input=text)
    if r.returncode:
        raise SystemExit("cron 定时任务没装上：%s" % (r.stderr or r.stdout))


def install_schedule(script):
    """装定时任务，返回装的是哪种。装不了就退出并说清楚怎么手动配。"""
    python = sys.executable
    kind = schedule_kind()
    if kind == "launchd":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(launchd_plist(python, script))
        target = "gui/%d" % os.getuid()
        _run(["launchctl", "bootout", "%s/%s" % (target, LABEL)])
        r = _run(["launchctl", "bootstrap", target, str(PLIST)])
        if r.returncode:
            raise SystemExit("定时任务没装上：%s" % (r.stderr or r.stdout))
    elif kind == "systemd":
        SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
        for name, content in systemd_units(python, script).items():
            (SYSTEMD_DIR / name).write_text(content, encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        r = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT + ".timer"])
        if r.returncode:
            raise SystemExit("systemd 定时器没装上：%s" % (r.stderr or r.stdout))
    elif kind == "cron":
        lines = [line for line in _crontab_lines() if CRON_MARK not in line]
        _write_crontab(lines + [cron_line(python, script)])
    elif kind == "schtasks":
        r = _run(schtasks_create_args(python, script))
        if r.returncode:
            raise SystemExit("Windows 计划任务没装上：%s" % (r.stderr or r.stdout))
        _run(["schtasks", "/Run", "/TN", WINDOWS_TASK])      # 装完先跑一次
    else:
        raise SystemExit(
            "这台电脑上找不到能用的定时任务工具（systemd 或 cron）。请自己配一个每 15 分钟运行一次的任务：\n"
            '  "%s" "%s" check' % (python, script))
    return kind


def uninstall_schedule():
    kind = schedule_kind()
    if kind == "launchd":
        _run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)])
        if PLIST.exists():
            PLIST.unlink()
    elif kind == "systemd":
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT + ".timer"])
        for name in systemd_units("python", "script"):
            path = SYSTEMD_DIR / name
            if path.exists():
                path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
    elif kind == "cron":
        lines = _crontab_lines()
        if any(CRON_MARK in line for line in lines):
            _write_crontab([line for line in lines if CRON_MARK not in line])
    elif kind == "schtasks":
        _run(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK])
    return kind


def schedule_installed():
    kind = schedule_kind()
    if kind == "launchd":
        return PLIST.exists()
    if kind == "systemd":
        return (SYSTEMD_DIR / (SYSTEMD_UNIT + ".timer")).exists()
    if kind == "cron":
        return any(CRON_MARK in line for line in _crontab_lines())
    if kind == "schtasks":
        return _run(["schtasks", "/Query", "/TN", WINDOWS_TASK]).returncode == 0
    return False


# ---------- install ----------

def used_agents():
    """这台电脑上用过哪些 agent：有聊天记录的就算。"""
    return [a for a, pattern in TRANSCRIPTS.items()
            if glob.glob(os.path.expanduser(pattern), recursive=True)]


def work_folders():
    """用 agent 干过活的文件夹。只读路径，不读对话内容。"""
    seen = {}
    cwd_re = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
    for pattern in TRANSCRIPTS.values():
        for value in glob.glob(os.path.expanduser(pattern), recursive=True):
            try:
                day = datetime.fromtimestamp(os.path.getmtime(value), BEIJING).strftime("%Y-%m-%d")
                with open(value, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for raw in set(cwd_re.findall(text)):
                try:
                    path = json.loads('"%s"' % raw)
                except ValueError:
                    continue
                if not is_absolute(path) or is_temp_path(path):
                    continue
                path = norm_path(path)
                if day > seen.get(path, ""):
                    seen[path] = day
    return [{"path": p, "last_used": d} for p, d in sorted(seen.items())]


def cmd_install(args):
    url = args.url.rstrip("/")
    # 定时任务里的 PATH 很短，claude 这类命令要靠 node，得把现在的 PATH 带过去
    cfg = {"url": url, "token": args.token, "path": os.environ.get("PATH", ""), "runners": {}}
    try:
        api(cfg, "GET", "/api/scan/due")
    except ApiError as exc:
        raise SystemExit("连不上 Fecho 或 token 不对：%s" % exc)

    wanted = [a.strip() for a in args.agents.split(",")] if args.agents else used_agents()
    report = []
    for agent in wanted:
        if agent not in TRANSCRIPTS:
            raise SystemExit("不认识的 agent：%s（支持 %s）" % (agent, "、".join(TRANSCRIPTS)))
        if agent not in CLI:
            report.append((agent, False, "还不支持在后台叫醒它（没实测过），暂时不扫它的对话"))
            continue
        cli = find_cli(agent)
        if not cli:
            report.append((agent, False, "找不到它的命令行"))
            continue
        cfg["runners"][agent] = cli
        ok, why = probe(cfg, agent)
        if not ok:
            del cfg["runners"][agent]
            report.append((agent, False, "%s。%s" % (why, FIX_HINT.get(agent, ""))))
        else:
            report.append((agent, True, cli))

    print("逐个试叫醒本机的 agent：")
    for agent, ok, detail in report:
        print("  %s %s：%s" % ("✓" if ok else "✗", agent, detail))
    if not cfg["runners"]:
        raise SystemExit("\n一个都叫不醒，没有装定时任务。按上面的提示修好后，重新运行这条 install。")

    HOME.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass
    script = HOME / "fecho_local.py"
    if Path(__file__).resolve() != script.resolve():
        shutil.copyfile(__file__, script)
    save_json(CONFIG, cfg)

    # 能叫醒的开扫描；叫不醒的明确关掉——否则服务器以为它会扫、每晚等一个永远不来的结果
    for agent, ok, _ in report:
        api(cfg, "POST", "/api/agents/%s" % agent, {"scan_enabled": ok})
    folders = work_folders()
    api(cfg, "POST", "/api/folders/report", {"folders": folders})
    kind = install_schedule(script)

    print("\n装好了。")
    print("- 会扫对话的 agent：%s" % "、".join(sorted(cfg["runners"])))
    skipped = [a for a, ok, _ in report if not ok]
    if skipped:
        print("- 暂时不扫：%s。修好后重新运行这条 install 就会加上" % "、".join(skipped))
    print("- 上报了 %d 个用 agent 干过活的文件夹，请到 %s/onboard 勾选哪些算工作" % (len(folders), url))
    print("- 定时任务（%s）每 15 分钟问一次服务器该不该扫；日志在 %s"
          % (SCHEDULE_LABEL.get(kind, kind), LOG))


def cmd_uninstall(_args):
    kind = uninstall_schedule()
    print("定时任务（%s）已移除。配置留在 %s，不需要可以整个删掉。"
          % (SCHEDULE_LABEL.get(kind, "无"), HOME))


def cmd_check(args):
    try:
        return check(load_config(), force_date=args.date)
    except ApiError as exc:
        log("问服务器失败：%s" % exc)
        return 1
    except urllib.error.URLError as exc:
        log("连不上服务器（没联网？）：%s" % exc)
        return 1


def cmd_status(_args):
    cfg = load_config()
    print("服务：%s" % cfg["url"])
    print("token：%s…" % cfg["token"][:10])
    for agent, cli in sorted((cfg.get("runners") or {}).items()):
        print("扫 %s 的对话，用：%s" % (agent, cli))
    kind = schedule_kind()
    print("定时任务：%s（%s）" % ("已装" if schedule_installed() else "没装",
                               SCHEDULE_LABEL.get(kind, "这台电脑上找不到定时任务工具")))
    try:
        due = api(cfg, "GET", "/api/scan/due")
        print("服务器说：%s（每天 %s 扫，白名单 %d 个文件夹）" % (
            due.get("reason"), due.get("scan_at"), len(due.get("folders") or [])))
    except (ApiError, urllib.error.URLError) as exc:
        print("问服务器失败：%s" % exc)
    if LOG.exists():
        print("最近日志：")
        for line in LOG.read_text(encoding="utf-8").splitlines()[-5:]:
            print("  " + line)


def main(argv=None):
    p = argparse.ArgumentParser(prog="fecho_local.py", description="Fecho 本机采集（云端版）")
    sub = p.add_subparsers(dest="cmd")
    i = sub.add_parser("install", help="装好并开始定时检查")
    i.add_argument("--url", required=True)
    i.add_argument("--token", required=True)
    i.add_argument("--agents", help="只装这几个，逗号分隔（默认：这台电脑上用过的全部）")
    c = sub.add_parser("check", help="问一次服务器，该扫就扫")
    c.add_argument("--date", help="不管到没到点，直接扫这一天（YYYY-MM-DD）")
    sub.add_parser("status", help="看状态")
    sub.add_parser("uninstall", help="移除定时任务")
    args = p.parse_args(argv)
    handlers = {"install": cmd_install, "check": cmd_check, "status": cmd_status,
                "uninstall": cmd_uninstall}
    if args.cmd not in handlers:
        p.print_help()
        return 2
    return handlers[args.cmd](args) or 0


if __name__ == "__main__":
    # Windows 控制台默认不是 UTF-8，直接 print 中文可能报错
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    sys.exit(main())
