"""人格化模板：模板是配置不是代码。"""
import json
from typing import Any, Dict

from . import config

_FALLBACK = {
    "display_name": "",
    "language": "zh",
    "daily_template": "# {date} 工作日志 · {display_name}\n\n## Done\n- ...\n",
    "daily_style": "条目化，动词开头。",
    "voice_style": "第一人称，口语化。",
    "voice_target_chars": [config.VOICE_MIN_CHARS, config.VOICE_MAX_CHARS],
}


def load(name: str) -> Dict[str, Any]:
    path = config.PERSONAS_DIR / ("%s.json" % name)
    if not path.exists():
        path = config.PERSONAS_DIR / "default.json"
    if not path.exists():
        return dict(_FALLBACK)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    merged = dict(_FALLBACK)
    merged.update(data)
    # 任务别名存在个人配置里（issue 号 → 显示用的短名），不写死在预设模板里
    merged["task_aliases"] = dict(config.load().get("task_aliases") or {})
    return merged


def fingerprint(persona: Dict[str, Any]) -> str:
    import hashlib

    blob = json.dumps(persona, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
