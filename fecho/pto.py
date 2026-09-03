"""PTO(timeoff) 解析。

V0 只提供 LocalProvider（config/pto.json），真 timeoff API 的接入点留在
TimeoffApiProvider —— 管道的分支逻辑现在就走通，数据源二批替换即可。
"""
import json
from typing import Any, Dict, List

from . import config


class LocalProvider:
    """本地覆盖表：{"rachel": ["2026-09-03", "2026-09-04"]}"""

    name = "local"

    def status(self, author: str, date: str) -> str:
        if not config.PTO_FILE.exists():
            return "ok"
        with config.PTO_FILE.open(encoding="utf-8") as f:
            data: Dict[str, List[str]] = json.load(f)
        return "pto" if date in data.get(author, []) else "ok"


class TimeoffApiProvider:
    """二批：对接公司 timeoff 系统。

    预期契约（调研中）：GET {base}/api/timeoff?user={author}&date={date}
      -> {"status": "approved|pending|none", "type": "pto|sick|..."}
    approved -> "pto"，其余 -> "ok"，请求失败 -> "unknown"（降级为正常出稿）。
    """

    name = "timeoff-api"

    def status(self, author: str, date: str) -> str:  # pragma: no cover - 二批
        raise NotImplementedError("timeoff API 对接为二批功能")


def provider() -> Any:
    return LocalProvider()


def status(author: str, date: str) -> str:
    try:
        return provider().status(author, date)
    except Exception:
        return "unknown"
