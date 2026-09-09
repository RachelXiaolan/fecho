"""Unattended Fecho pipeline and Beijing-time scheduling state."""
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from . import clock, config, service, store


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
