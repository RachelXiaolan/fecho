"""One-command Fecho onboarding without exposing shared LLM credentials."""
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlparse

import httpx

from . import automation, clock, config, scope


LLM_FIELDS = {
    "llm_base_url", "llm_api_key", "llm_model",
    "llm_reasoning_effort", "llm_timeout",
}
REQUIRED_LLM_FIELDS = {"llm_base_url", "llm_api_key", "llm_model"}


def _llm_values(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value[key] for key in LLM_FIELDS if value.get(key) not in (None, "")}


def _valid_llm(value: Dict[str, Any]) -> bool:
    return REQUIRED_LLM_FIELDS.issubset(_llm_values(value))


def _http_fetch(url: str) -> Dict[str, Any]:
    response = httpx.get(url, timeout=20, follow_redirects=True)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("共享 LLM 配置必须是 JSON object")
    return value


def resolve_shared_llm(
    *,
    current: Optional[Dict[str, Any]] = None,
    shared_file: Optional[Path] = None,
    shared_url: Optional[str] = None,
    fetch: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    current = current if current is not None else config.load()
    if _valid_llm(current):
        return {"source": "existing", "values": _llm_values(current)}

    shared_file = shared_file or os.getenv("FECHO_SHARED_CONFIG")
    shared_url = shared_url or os.getenv("FECHO_SHARED_CONFIG_URL")
    if shared_file:
        try:
            value = json.loads(Path(shared_file).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("读取共享 LLM 配置失败：%s" % exc) from exc
        source = "file"
    elif shared_url:
        parsed = urlparse(shared_url)
        if parsed.scheme != "https":
            raise ValueError("共享 LLM 配置地址必须使用 HTTPS")
        value = (fetch or _http_fetch)(shared_url)
        source = "url"
    else:
        raise RuntimeError(
            "缺少共享 LLM 配置。由管理员设置 FECHO_SHARED_CONFIG 或 "
            "FECHO_SHARED_CONFIG_URL 后重试；普通安装者不需要填写 MiniMax key。")

    if not isinstance(value, dict) or not _valid_llm(value):
        raise RuntimeError("共享 LLM 配置缺少 llm_base_url、llm_api_key 或 llm_model")
    return {"source": source, "values": _llm_values(value)}


def persist_shared_llm(values: Dict[str, Any]) -> Dict[str, Any]:
    safe = _llm_values(values)
    if not _valid_llm(safe):
        raise RuntimeError("共享 LLM 配置不完整")
    config.update(**safe)
    config.reload_module()
    return {"configured": True, "model": safe["llm_model"]}


def _configure(values: Dict[str, Any]) -> None:
    config.update(**values)
    config.reload_module()


def _connect_mobius(assignee: str) -> Dict[str, Any]:
    from . import oauth, service

    url = config.MOBIUS_URL or "https://mobius.feedmob.com/api/mcp"
    # Onboarding belongs to the current installer. Never inherit a previous
    # person's Mobius identity merely because this machine has a token.
    config.update(mobius_url=url, mobius_assignee=assignee,
                  mobius_token=None, mobius_oauth=None, mobius_auth=None)
    config.reload_module()
    started = oauth.login(url, open_browser=True, timeout=300)
    oauth.complete(started["ctx"])
    return service.sync_issues(assignee)


def onboard(
    *,
    author: str,
    display_name: Optional[str] = None,
    work_prefixes: Iterable[str] = (),
    ignores: Iterable[str] = (),
    mobius_assignee: Optional[str] = None,
    daily_time: str = clock.DEFAULT_DAILY_TIME,
    shared_file: Optional[Path] = None,
    shared_url: Optional[str] = None,
    skip_mobius: bool = False,
    skip_schedule: bool = False,
    dry_run: bool = False,
    configure: Optional[Callable[[Dict[str, Any]], None]] = None,
    scope_add: Optional[Callable[[str, str], Any]] = None,
    install_hosts: Optional[Callable[[], Dict[str, Any]]] = None,
    connect_mobius: Optional[Callable[[str], Dict[str, Any]]] = None,
    install_schedule: Optional[Callable[[str], Dict[str, Any]]] = None,
    doctor: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not author.strip():
        raise ValueError("author 不能为空")
    daily_time = clock.validate_daily_time(daily_time)
    provision = resolve_shared_llm(shared_file=shared_file, shared_url=shared_url)
    work_prefixes = [str(Path(p).expanduser().resolve()) for p in work_prefixes]
    ignores = [str(Path(p).expanduser().resolve()) for p in ignores]
    if not work_prefixes and not (scope.rules()["work_prefixes"] or scope.rules()["work"]):
        raise RuntimeError("至少设置一个工作区白名单；白名单外的会话不会读取")
    if not skip_mobius and not mobius_assignee:
        raise RuntimeError("连接 Mobius 需要 --mobius-assignee")

    summary: Dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "author": author,
        "llm": {"configured": True, "model": provision["values"]["llm_model"],
                "source": provision["source"]},
        "scope": {"work_prefixes": work_prefixes, "ignore": ignores},
        "schedule": {"daily_time": daily_time, "timezone": "Asia/Shanghai"},
    }
    if dry_run:
        return summary

    configure = configure or _configure
    scope_add = scope_add or scope.add
    if install_hosts is None:
        from . import hosts

        install_hosts = hosts.install_all
    connect_mobius = connect_mobius or _connect_mobius
    install_schedule = install_schedule or automation.install_schedule
    if doctor is None:
        from . import service

        doctor = service.doctor

    settings = {
        "author": author,
        "display_name": display_name or author,
        "mobius_assignee": mobius_assignee,
        **provision["values"],
    }
    configure({k: v for k, v in settings.items() if v not in (None, "")})
    for value in work_prefixes:
        scope_add("work_prefixes", value)
    for value in ignores:
        scope_add("ignore", value)

    summary["hosts"] = install_hosts()
    if not skip_mobius:
        synced = connect_mobius(str(mobius_assignee))
        summary["mobius"] = {"connected": True, "issues": synced.get("count", 0)}
    else:
        summary["mobius"] = {"connected": False, "skipped": True}
    if not skip_schedule:
        installed = install_schedule(daily_time)
        summary["schedule"].update({"installed": bool(installed.get("installed")),
                                    "dashboard_url": installed.get("dashboard_url")})
    else:
        summary["schedule"]["installed"] = False
    summary["doctor"] = doctor()
    return summary
