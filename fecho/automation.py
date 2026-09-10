"""Unattended Fecho pipeline and Beijing-time scheduling state."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from urllib import request as urllib_request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import clock, config, service, store


DAILY_LABEL = "com.feedmob.fecho.daily"
DASHBOARD_LABEL = "com.feedmob.fecho.dashboard"
OWNED_LABELS = (DAILY_LABEL, DASHBOARD_LABEL)
SENSITIVE_ENV = ("FECHO_LLM_API_KEY", "FECHO_MOBIUS_TOKEN", "MOBIUS_API_KEY")

# 失败后补跑：只补一次，间隔 30 分钟。
# 正常触发窗口只有 10 分钟，一次网络抖动就丢一整天，所以要补；
# 但补跑无限重试会在真故障时刷屏，所以只补一次就停手并报警。
RETRY_LIMIT = 1
RETRY_DELAY_MINUTES = 30


def schedule_state() -> Dict[str, Any]:
    value = deepcopy(config.load().get("automation") or {})
    value.setdefault("enabled", False)
    value.setdefault("daily_time", clock.DEFAULT_DAILY_TIME)
    return value


def _save_state(value: Dict[str, Any]) -> None:
    config.update(automation=value)


def _public_error(exc: Exception) -> str:
    return "%s: %s" % (exc.__class__.__name__, str(exc))


def run_daily(
    date: str,
    *,
    sync: Optional[Callable[[], Dict[str, Any]]] = None,
    scan_func: Optional[Callable[..., Dict[str, Any]]] = None,
    digest_func: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run sync -> transcript scan -> report without letting sync block local work."""
    from . import scan as scanner

    sync = sync or service.sync_issues
    scan_func = scan_func or scanner.scan
    digest_func = digest_func or service.end_of_day
    result: Dict[str, Any] = {"ok": False, "date": date, "warnings": [], "stages": {}}

    try:
        synced = sync()
        result["stages"]["sync"] = {"ok": True, "count": synced.get("count")}
    except Exception as exc:
        message = _public_error(exc)
        result["warnings"].append("Mobius 同步失败：%s" % message)
        result["stages"]["sync"] = {"ok": False, "error": message}

    try:
        scanned = scan_func(days=2)
    except Exception as exc:
        scanned = {"ok": False, "error": _public_error(exc)}
    if not scanned.get("ok"):
        result["error"] = "会话扫描失败：%s" % scanned.get("error", "未知错误")
        result["stages"]["scan"] = {"ok": False, "error": scanned.get("error")}
        return result
    result["stages"]["scan"] = {
        "ok": True,
        "recorded": scanned.get("recorded", 0),
        "groups": len(scanned.get("groups") or []),
    }

    try:
        report = digest_func(date, force=False)
    except Exception as exc:
        result["error"] = "日报生成失败：%s" % _public_error(exc)
        result["stages"]["digest"] = {"ok": False, "error": _public_error(exc)}
        return result
    result["stages"]["digest"] = {
        "ok": True,
        "status": report.get("status"),
        "updates": report.get("update_count", 0),
    }
    result["ok"] = True
    result["report"] = result["stages"]["digest"]
    return result


def _due(instant: datetime, daily_time: str) -> bool:
    current = clock.now(instant)
    hour, minute = map(int, clock.validate_daily_time(daily_time).split(":"))
    target = hour * 60 + minute
    actual = current.hour * 60 + current.minute
    end = min(target + 10, 22 * 60)
    return target <= actual < end


def _retry_due(instant: datetime, state: Dict[str, Any], date: str) -> bool:
    """失败后的补跑窗口到了没。

    正常窗口只有 10 分钟，21:00 那次挂了（比如在路上没网），当天就再也不会
    触发——一次网络抖动等于丢一整天。所以失败后排一次补跑。
    launchd 每分钟叫醒一次 tick，补跑不需要另装定时器。
    """
    retry = state.get("retry")
    if not retry or retry.get("date") != date:
        return False
    # attempts = 已经**执行过**的补跑次数。排好但还没跑的那次不算在内，
    # 否则刚排完就被自己挡掉。
    if retry.get("attempts", 0) >= RETRY_LIMIT:
        return False
    try:
        at = datetime.fromisoformat(retry["at"])
    except (KeyError, TypeError, ValueError):
        return False
    return clock.now(instant) >= clock.now(at)


def notify(title: str, message: str, *, runner: Optional[Callable[..., Any]] = None) -> bool:
    """弹一条系统通知。报警失败不能反过来搞挂定时任务，所以一律吞掉异常。"""
    import shutil
    import subprocess

    runner = runner or subprocess.run
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        runner([osascript, "-e", 'display notification "%s" with title "%s"'
                % (safe(message), safe(title))],
               check=False, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def tick(
    *,
    instant: Optional[datetime] = None,
    state: Optional[Dict[str, Any]] = None,
    runner: Optional[Callable[[str], Dict[str, Any]]] = None,
    save: Optional[Callable[[Dict[str, Any]], None]] = None,
    notifier: Optional[Callable[[str, str], Any]] = None,
) -> Dict[str, Any]:
    """每个北京日历日最多成功跑一次；失败排一次补跑并报警。"""
    instant = instant or clock.now()
    state = deepcopy(state if state is not None else schedule_state())
    runner = runner or (lambda date: run_daily(date))
    save = save or _save_state
    notifier = notifier or (lambda title, msg: notify(title, msg))
    date = clock.today(instant)

    if not state.get("enabled"):
        return {"status": "disabled", "date": date}
    if state.get("last_run_date") == date:
        return {"status": "already-run", "date": date}

    is_retry = _retry_due(instant, state, date)
    if not is_retry and not _due(instant, state.get("daily_time", clock.DEFAULT_DAILY_TIME)):
        return {"status": "not-due", "date": date}

    started_at = store.now_iso()
    result = runner(date)
    ok = bool(result.get("ok"))
    error = result.get("error") or "未知错误"
    state["last_result"] = {
        "status": "succeeded" if ok else "failed",
        "date": date,
        "at": started_at,
        **({"attempt": "retry"} if is_retry else {}),
        **({} if ok else {"error": error}),
    }

    if ok:
        state["last_run_date"] = date
        state.pop("retry", None)
    else:
        attempts = (state.get("retry") or {}).get("attempts", 0)
        if is_retry:
            attempts += 1                    # 这次就是补跑，算进已执行次数
        if attempts < RETRY_LIMIT:
            # 还有补跑机会：排下一次。存绝对时间点，tick 每分钟醒一次自己去比。
            state["retry"] = {
                "date": date, "attempts": attempts,
                "at": (clock.now(instant) + timedelta(minutes=RETRY_DELAY_MINUTES)).isoformat(),
            }
            notifier("Fecho 日报任务失败",
                     "%s；%d 分钟后自动重试" % (error, RETRY_DELAY_MINUTES))
        else:
            state["retry"] = {"date": date, "attempts": attempts, "at": None}
            notifier("Fecho 日报任务仍然失败",
                     "%s；重试已用完，今天不再自动跑" % error)

    save(state)
    return {"status": state["last_result"]["status"], "date": date,
            "attempt": "retry" if is_retry else "scheduled", "result": result}


def _agent_path(home: Path, label: str) -> Path:
    return home / "Library" / "LaunchAgents" / (label + ".plist")


def launch_agent_specs(
    *, python: Optional[str] = None, home: Optional[Path] = None,
) -> Dict[str, bytes]:
    python = python or sys.executable
    home = Path(home or Path.home())
    log_home = config.HOME if home == Path.home() else home / ".fecho"
    common: Dict[str, Any] = {
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"FECHO_HOME": str(log_home)},
    }
    clean_env = ["/usr/bin/env"]
    for name in SENSITIVE_ENV:
        clean_env.extend(["-u", name])
    clean_env.append("FECHO_HOME=%s" % log_home)
    daily = {
        **common,
        "Label": DAILY_LABEL,
        "ProgramArguments": clean_env + [
            python, "-m", "fecho.cli", "schedule", "tick"],
        "StartInterval": 60,
        "StandardOutPath": str(log_home / "automation.log"),
        "StandardErrorPath": str(log_home / "automation.log"),
    }
    dashboard = {
        **common,
        "Label": DASHBOARD_LABEL,
        "ProgramArguments": clean_env + [
            python, "-m", "fecho.cli", "web", "--host", "127.0.0.1",
            "--port", "8900", "--no-browser",
        ],
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_home / "dashboard.log"),
        "StandardErrorPath": str(log_home / "dashboard.log"),
    }
    return {
        DAILY_LABEL: plistlib.dumps(daily, sort_keys=True),
        DASHBOARD_LABEL: plistlib.dumps(dashboard, sort_keys=True),
    }


def _invoke(runner: Callable[..., Any], args: list, *, check: bool) -> int:
    value = runner(args, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    code = value if isinstance(value, int) else value.returncode
    if check and code:
        stderr = getattr(value, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise RuntimeError("%s 失败：%s" % (" ".join(args[:2]), str(stderr).strip()))
    return int(code)


def _result_text(value: Any, field: str = "stdout") -> str:
    raw = getattr(value, field, b"") or b""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def release_dashboard_port(
    *, home: Optional[Path] = None, uid: Optional[int] = None, port: int = 8900,
    managed_pid: Optional[int] = None,
    runner: Callable[..., Any] = subprocess.run,
    killer: Callable[[int, int], None] = os.kill,
    waiter: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Stop only a legacy Fecho dashboard owned by this user."""
    home = Path(home or Path.home())
    uid = os.getuid() if uid is None else uid

    def listener() -> Optional[int]:
        result = runner(
            ["lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if int(getattr(result, "returncode", result if isinstance(result, int) else 1)):
            return None
        text = _result_text(result).strip()
        return int(text.splitlines()[0]) if text else None

    pid = listener()
    if pid is None:
        return {"status": "free"}
    process = runner(
        ["ps", "-p", str(pid), "-o", "uid=,command="],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = _result_text(process).strip()
    parts = line.split(None, 1)
    owner = int(parts[0]) if parts and parts[0].isdigit() else -1
    command = parts[1] if len(parts) > 1 else ""
    install = str(home / ".fecho" / "venv" / "bin")
    owned = owner == uid and (pid == managed_pid or
        (install + "/fecho web") in command or
        (command.startswith(install + "/python") and "-m fecho.cli web" in command))
    if not owned:
        raise RuntimeError("端口 %d 已被不是 Fecho 的程序占用；未终止该进程" % port)

    killer(pid, signal.SIGTERM)
    for _ in range(20):
        if listener() is None:
            return {"status": "migrated", "pid": pid}
        waiter(0.1)
    raise RuntimeError("旧 Fecho Dashboard 未能释放端口 %d" % port)


def install_launch_agents(
    *,
    home: Optional[Path] = None,
    python: Optional[str] = None,
    runner: Callable[..., Any] = subprocess.run,
    uid: Optional[int] = None,
) -> Dict[str, Any]:
    if sys.platform != "darwin" and home is None:
        raise RuntimeError("自动后台服务目前只支持 macOS launchd")
    home = Path(home or Path.home())
    uid = os.getuid() if uid is None else uid
    target = "gui/%d" % uid
    specs = launch_agent_specs(python=python, home=home)
    files = []
    for label, content in specs.items():
        path = _agent_path(home, label)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        managed_pid = (_launch_runtime(label, uid=uid, runner=runner).get("pid")
                       if label == DASHBOARD_LABEL else None)
        _invoke(runner, ["launchctl", "bootout", "%s/%s" % (target, label)], check=False)
        if label == DASHBOARD_LABEL:
            release_dashboard_port(
                home=home, uid=uid, managed_pid=managed_pid, runner=runner)
        _invoke(runner, ["launchctl", "bootstrap", target, str(path)], check=True)
        _invoke(runner, ["launchctl", "kickstart", "-k", "%s/%s" % (target, label)], check=True)
        files.append(str(path))
    return {"installed": True, "files": files, "dashboard_url": "http://127.0.0.1:8900/"}


def uninstall_launch_agents(
    *,
    home: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    uid: Optional[int] = None,
) -> Dict[str, Any]:
    if sys.platform != "darwin" and home is None:
        raise RuntimeError("自动后台服务目前只支持 macOS launchd")
    home = Path(home or Path.home())
    uid = os.getuid() if uid is None else uid
    target = "gui/%d" % uid
    removed = []
    for label in OWNED_LABELS:
        path = _agent_path(home, label)
        _invoke(runner, ["launchctl", "bootout", "%s/%s" % (target, label)], check=False)
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return {"installed": False, "removed": removed}


def install_schedule(
    daily_time: str = clock.DEFAULT_DAILY_TIME,
    *,
    state: Optional[Dict[str, Any]] = None,
    save: Optional[Callable[[Dict[str, Any]], None]] = None,
    installer: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    daily_time = clock.validate_daily_time(daily_time)
    state = deepcopy(state if state is not None else schedule_state())
    save = save or _save_state
    installed = (installer or install_launch_agents)()
    state.update({"enabled": True, "daily_time": daily_time})
    save(state)
    return {**installed, "daily_time": daily_time, "timezone": "Asia/Shanghai"}


def run_now(
    *,
    runner: Optional[Callable[[str], Dict[str, Any]]] = None,
    state: Optional[Dict[str, Any]] = None,
    save: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    state = deepcopy(state if state is not None else schedule_state())
    save = save or _save_state
    date = clock.today()
    result = (runner or run_daily)(date)
    state["last_result"] = {
        "status": "succeeded" if result.get("ok") else "failed",
        "date": date,
        "at": store.now_iso(),
        **({"error": result.get("error", "未知错误")} if not result.get("ok") else {}),
    }
    if result.get("ok"):
        state["last_run_date"] = date
    save(state)
    return {"status": state["last_result"]["status"], "date": date, "result": result}


def _launch_runtime(
    label: str, *, uid: int, runner: Callable[..., Any] = subprocess.run,
) -> Dict[str, Any]:
    result = runner(
        ["launchctl", "print", "gui/%d/%s" % (uid, label)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    code = int(getattr(result, "returncode", result if isinstance(result, int) else 1))
    output = _result_text(result)
    state_match = re.search(r"^\s*state\s*=\s*([^\n]+)", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code\s*=\s*(-?\d+)", output, re.MULTILINE)
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)", output, re.MULTILINE)
    return {
        "loaded": code == 0,
        "state": state_match.group(1).strip() if state_match else None,
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
        "pid": int(pid_match.group(1)) if pid_match else None,
    }


def _health(url: str) -> Dict[str, Any]:
    with urllib_request.urlopen(url, timeout=1.0) as response:
        return json.loads(response.read().decode("utf-8"))


def status(
    home: Optional[Path] = None, *, uid: Optional[int] = None,
    runner: Callable[..., Any] = subprocess.run,
    health: Callable[[str], Dict[str, Any]] = _health,
) -> Dict[str, Any]:
    home = Path(home or Path.home())
    uid = os.getuid() if uid is None else uid
    state = schedule_state()
    state["timezone"] = "Asia/Shanghai"
    state["dashboard_url"] = "http://127.0.0.1:8900/"
    configured = {
        label: _agent_path(home, label).exists() for label in OWNED_LABELS
    }
    runtime = {label: _launch_runtime(label, uid=uid, runner=runner)
               for label in OWNED_LABELS}
    daily = runtime[DAILY_LABEL]
    daily["ok"] = bool(
        configured[DAILY_LABEL] and daily["loaded"] and
        daily["last_exit_code"] in (None, 0))
    dashboard = runtime[DASHBOARD_LABEL]
    try:
        dashboard_health = health(state["dashboard_url"] + "healthz")
    except Exception:
        dashboard_health = {"ok": False}
    dashboard["health"] = dashboard_health
    dashboard["ok"] = bool(
        configured[DASHBOARD_LABEL] and dashboard["loaded"] and
        dashboard["state"] == "running" and
        dashboard["last_exit_code"] in (None, 0) and dashboard_health.get("ok"))
    state["configured_launch_agents"] = configured
    state["launch_agents"] = {label: item["loaded"] for label, item in runtime.items()}
    state["runtime"] = runtime
    state["ready"] = bool(state.get("enabled") and daily["ok"] and dashboard["ok"])
    return state


def uninstall_schedule() -> Dict[str, Any]:
    result = uninstall_launch_agents()
    state = schedule_state()
    state["enabled"] = False
    _save_state(state)
    return {**result, "daily_time": state.get("daily_time", clock.DEFAULT_DAILY_TIME)}
