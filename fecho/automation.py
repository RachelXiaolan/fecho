"""Unattended Fecho pipeline and Beijing-time scheduling state."""
from copy import deepcopy
from datetime import datetime
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import clock, config, service, store


DAILY_LABEL = "com.feedmob.fecho.daily"
DASHBOARD_LABEL = "com.feedmob.fecho.dashboard"
OWNED_LABELS = (DAILY_LABEL, DASHBOARD_LABEL)


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


def tick(
    *,
    instant: Optional[datetime] = None,
    state: Optional[Dict[str, Any]] = None,
    runner: Optional[Callable[[str], Dict[str, Any]]] = None,
    save: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run at most once per Beijing calendar day inside a short grace window."""
    instant = instant or clock.now()
    state = deepcopy(state if state is not None else schedule_state())
    runner = runner or (lambda date: run_daily(date))
    save = save or _save_state
    date = clock.today(instant)

    if not state.get("enabled"):
        return {"status": "disabled", "date": date}
    if state.get("last_run_date") == date:
        return {"status": "already-run", "date": date}
    if not _due(instant, state.get("daily_time", clock.DEFAULT_DAILY_TIME)):
        return {"status": "not-due", "date": date}

    started_at = store.now_iso()
    result = runner(date)
    state["last_result"] = {
        "status": "succeeded" if result.get("ok") else "failed",
        "date": date,
        "at": started_at,
        **({"error": result.get("error", "未知错误")} if not result.get("ok") else {}),
    }
    if result.get("ok"):
        state["last_run_date"] = date
    save(state)
    return {"status": state["last_result"]["status"], "date": date, "result": result}


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
    daily = {
        **common,
        "Label": DAILY_LABEL,
        "ProgramArguments": [python, "-m", "fecho.cli", "schedule", "tick"],
        "StartInterval": 60,
        "StandardOutPath": str(log_home / "automation.log"),
        "StandardErrorPath": str(log_home / "automation.log"),
    }
    dashboard = {
        **common,
        "Label": DASHBOARD_LABEL,
        "ProgramArguments": [
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
        _invoke(runner, ["launchctl", "bootout", "%s/%s" % (target, label)], check=False)
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


def status(home: Optional[Path] = None) -> Dict[str, Any]:
    home = Path(home or Path.home())
    state = schedule_state()
    state["timezone"] = "Asia/Shanghai"
    state["dashboard_url"] = "http://127.0.0.1:8900/"
    state["launch_agents"] = {
        label: _agent_path(home, label).exists() for label in OWNED_LABELS
    }
    return state


def uninstall_schedule() -> Dict[str, Any]:
    result = uninstall_launch_agents()
    state = schedule_state()
    state["enabled"] = False
    _save_state(state)
    return {**result, "daily_time": state.get("daily_time", clock.DEFAULT_DAILY_TIME)}
