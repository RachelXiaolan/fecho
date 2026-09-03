"""配置：文件优先，环境变量覆盖。

装完就能跑，不需要先起服务：默认数据落在 ~/.fecho/ 下，单进程直连 SQLite。
只有做团队共享部署时才需要 REST 服务（设 FECHO_REMOTE_URL 指过去）。

密钥只落在 ~/.fecho/config.json（0600），不进代码、不进仓库、不打印。
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

HOME = Path(os.getenv("FECHO_HOME", Path.home() / ".fecho"))
CONFIG_FILE = HOME / "config.json"

_cache: Optional[Dict[str, Any]] = None


def load() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        if CONFIG_FILE.exists():
            try:
                with CONFIG_FILE.open(encoding="utf-8") as f:
                    _cache = json.load(f)
            except (OSError, ValueError):
                _cache = {}
        else:
            _cache = {}
    return _cache


def save(data: Dict[str, Any]) -> None:
    """写配置。里面有 token，所以目录和文件都收权限。"""
    global _cache
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)
    _cache = data


def update(**kw: Any) -> Dict[str, Any]:
    data = dict(load())
    for k, v in kw.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    save(data)
    return data


def get(key: str, env: Optional[str] = None, default: Any = None) -> Any:
    """环境变量 > 配置文件 > 默认值。"""
    if env:
        v = os.getenv(env)
        if v not in (None, ""):
            return v
    v = load().get(key)
    return default if v in (None, "") else v


def _path(key: str, env: str, default: Path) -> Path:
    v = get(key, env)
    return Path(v) if v else default


# ---- 数据位置 ----
DB_PATH = _path("db", "FECHO_DB", HOME / "fecho.db")
LOGS_DIR = _path("logs_dir", "FECHO_LOGS_DIR", HOME / "logs")
PERSONAS_DIR = _path("personas_dir", "FECHO_PERSONAS_DIR",
                     Path(__file__).resolve().parent / "presets" / "personas")
PTO_FILE = _path("pto_file", "FECHO_PTO_FILE", HOME / "pto.json")
TOKENS_FILE = _path("tokens_file", "FECHO_TOKENS", HOME / "tokens.json")

# ---- 身份 ----
AUTHOR = get("author", "FECHO_AUTHOR", os.getenv("USER") or "me")
DISPLAY_NAME = get("display_name", "FECHO_DISPLAY_NAME", "")
PERSONA = get("persona", "FECHO_PERSONA", "default")

# ---- 团队联邦（可选）：日终把成品（日报/口播稿）推给共享收集端 ----
# collector 只存收到的成品，物理上没有 entries/tasks 表——不是靠权限管住的隐私边界，
# 是收集端的库里压根不存在能读到原始进展的表。
COLLECTOR_URL = get("collector_url", "FECHO_COLLECTOR_URL", "")
COLLECTOR_TOKEN = get("collector_token", "FECHO_COLLECTOR_TOKEN", "")
# 下面两个只在你本机就是 collector（跑 `fecho serve`）时才有意义
HOST = get("host", "FECHO_HOST", "127.0.0.1")
PORT = int(get("port", "FECHO_PORT", 8899))

# ---- LLM ----
LLM_BASE_URL = get("llm_base_url", "FECHO_LLM_BASE_URL", "")
LLM_API_KEY = get("llm_api_key", "FECHO_LLM_API_KEY", "")
LLM_MODEL = get("llm_model", "FECHO_LLM_MODEL", "")
LLM_TIMEOUT = float(get("llm_timeout", "FECHO_LLM_TIMEOUT", 180))
LLM_REASONING_EFFORT = get("llm_reasoning_effort", "FECHO_LLM_REASONING_EFFORT", "")

# ---- Mobius ----
MOBIUS_URL = get("mobius_url", "FECHO_MOBIUS_URL", "")
MOBIUS_TOKEN = get("mobius_token", "FECHO_MOBIUS_TOKEN", "")
MOBIUS_ASSIGNEE = get("mobius_assignee", "FECHO_MOBIUS_ASSIGNEE", "")

# ---- 调参 ----
MATCH_THRESHOLD = float(get("match_threshold", "FECHO_MATCH_THRESHOLD", 0.30))
TASK_CONTINUE_THRESHOLD = float(get("task_continue_threshold",
                                    "FECHO_TASK_CONTINUE_THRESHOLD", 0.35))
VOICE_MIN_CHARS = int(get("voice_min", "FECHO_VOICE_MIN", 200))
VOICE_MAX_CHARS = int(get("voice_max", "FECHO_VOICE_MAX", 280))


def reload_module() -> None:
    """配置写盘之后刷新本模块的常量（setup / login 之后调）。"""
    global _cache
    _cache = None
    import importlib
    import sys

    importlib.reload(sys.modules[__name__])


def llm_configured() -> bool:
    return bool(LLM_BASE_URL and LLM_API_KEY and LLM_MODEL)


def mobius_configured() -> bool:
    return bool(MOBIUS_URL and MOBIUS_TOKEN)


def redacted() -> Dict[str, Any]:
    """给 doctor / setup_status 用：说清楚配没配，但不吐出值。"""
    def mark(v: Any) -> str:
        return "已配置" if v else "未配置"

    return {
        "home": str(HOME),
        "author": AUTHOR,
        "display_name": DISPLAY_NAME or AUTHOR,
        "persona": PERSONA,
        "db": str(DB_PATH),
        "logs_dir": str(LOGS_DIR),
        "team": {"collector_url": COLLECTOR_URL or None, "token": mark(COLLECTOR_TOKEN)},
        "llm": {"base_url": LLM_BASE_URL or None, "model": LLM_MODEL or None,
                "api_key": mark(LLM_API_KEY)},
        "mobius": {"url": MOBIUS_URL or None, "assignee": MOBIUS_ASSIGNEE or None,
                   "token": mark(MOBIUS_TOKEN),
                   "auth": load().get("mobius_auth", "none")},
    }
