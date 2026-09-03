"""静态 Bearer token 即身份：一人一 token，V0 不做 OAuth。"""
import json
from typing import Any, Dict, Optional

from . import config


def _load() -> Dict[str, Dict[str, Any]]:
    if not config.TOKENS_FILE.exists():
        return {}
    with config.TOKENS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def resolve(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """token -> {author, persona, display_name}；无效返回 None。"""
    if not token:
        return None
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    ident = _load().get(token)
    if not ident:
        return None
    out = dict(ident)
    out.setdefault("persona", out.get("author"))
    out.setdefault("display_name", out.get("author"))
    return out


def all_authors() -> Dict[str, Dict[str, Any]]:
    return {v["author"]: v for v in _load().values()}
