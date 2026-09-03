"""把本机整理好的成品推给团队 collector。

只推「日报 + 口播稿」这两段最终文本，从不推原始进展——本地的 entries/tasks/
updates 表整个不参与这个模块。collector 不可达或没配置都不影响本地：
本地文件永远是权威副本，推送失败只是团队视图暂时看不到，agent 该有的产物照样都在。
"""
from typing import Any, Dict, Optional

import httpx

from . import config, db


class PushError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.COLLECTOR_URL and config.COLLECTOR_TOKEN)


def push(author: str, date: str) -> Dict[str, Any]:
    if not configured():
        return {"pushed": False, "reason": "未配置团队 collector（FECHO_COLLECTOR_URL）"}

    daily = db.get_report(author, date, "daily")
    voice = db.get_report(author, date, "voice")
    if not daily and not voice:
        return {"pushed": False, "reason": "本地还没有可推的报告"}

    body = {
        "date": date,
        "daily_md": daily["content_md"] if daily else None,
        "voice_md": voice["content_md"] if voice else None,
        "meta": {
            "generator": (daily or voice)["generator"],
            "model": (daily or {}).get("model") or (voice or {}).get("model"),
        },
    }
    try:
        r = httpx.post(
            config.COLLECTOR_URL.rstrip("/") + "/reports",
            json=body,
            headers={"Authorization": "Bearer %s" % config.COLLECTOR_TOKEN},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return {"pushed": False, "reason": "collector 连不上: %s" % str(exc)[:200]}
    if r.status_code >= 400:
        return {"pushed": False, "reason": "collector %d: %s" % (r.status_code, r.text[:200])}
    return {"pushed": True, **r.json()}


def team_digest(date: str, author: Optional[str] = None) -> Dict[str, Any]:
    """从 collector 读团队某天的成品——团队视图，只有 finished 产物，没有原始任务。"""
    if not configured():
        raise PushError("未配置团队 collector（FECHO_COLLECTOR_URL）")
    params = {"date": date}
    if author:
        params["author"] = author
    try:
        r = httpx.get(
            config.COLLECTOR_URL.rstrip("/") + "/reports",
            params=params,
            headers={"Authorization": "Bearer %s" % config.COLLECTOR_TOKEN},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PushError("collector 连不上: %s" % exc) from exc
    if r.status_code >= 400:
        raise PushError("collector %d: %s" % (r.status_code, r.text[:200]))
    return r.json()
