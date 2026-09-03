"""OpenAI 兼容端点的最小客户端。key 只从环境变量来。"""
import re
from typing import List, Optional

import httpx

from . import config


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """推理占满了 token 预算，正文没吐出来。可重试。"""


class LLMNotConfigured(LLMError):
    pass


_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """推理模型（minimax-m3 等）可能把思维链内联在 content 里；代理层也可能已经拆走。
    两种形态都要能吃下——端点行为不该泄漏到整理层。"""
    return _THINK.sub("", text or "").strip()


MAX_TOKEN_CEILING = 16000


def chat(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 4000,
    truncation_retries: int = 2,
) -> str:
    """推理模型的思维链长度不稳定：同一个提示词可能烧 1.6k，也可能烧穿 4k。
    撞上就翻倍预算重来，而不是把整天的产物降级——这是瞬时失败，不是能力问题。
    """
    budget = max_tokens
    last: Optional[LLMTruncated] = None
    for _ in range(truncation_retries + 1):
        try:
            return _chat_once(messages, model, temperature, budget)
        except LLMTruncated as exc:
            last = exc
            if budget >= MAX_TOKEN_CEILING:
                break
            budget = min(budget * 2, MAX_TOKEN_CEILING)
    raise last  # type: ignore[misc]


def _chat_once(
    messages: List[dict],
    model: Optional[str],
    temperature: float,
    max_tokens: int,
) -> str:
    if not config.llm_configured():
        raise LLMNotConfigured(
            "未配置 LLM：请设置 SCRIBE_LLM_BASE_URL 与 SCRIBE_LLM_API_KEY"
        )
    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": model or config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if config.LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = config.LLM_REASONING_EFFORT
    headers = {
        "Authorization": "Bearer %s" % config.LLM_API_KEY,
        "Content-Type": "application/json",
    }

    def _post(body):
        try:
            return httpx.post(url, json=body, headers=headers, timeout=config.LLM_TIMEOUT)
        except httpx.HTTPError as exc:
            raise LLMError("LLM 请求失败: %s" % exc) from exc

    r = _post(payload)
    if r.status_code >= 400 and "reasoning_effort" in payload:
        # 非标准参数，端点不认就去掉重来一次，不能因为调优参数把整条管道拖死
        payload.pop("reasoning_effort")
        r = _post(payload)
    if r.status_code >= 400:
        raise LLMError("LLM %d: %s" % (r.status_code, r.text[:400]))
    data = r.json()
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError("LLM 返回格式异常: %s" % str(data)[:400]) from exc

    content = strip_reasoning(msg.get("content") or "")
    if not content:
        # 推理模型把预算烧在思维链上，正文空了。这是可诊断的失败，不是格式错误。
        if msg.get("reasoning_content") or choice.get("finish_reason") == "length":
            raise LLMTruncated(
                "LLM 正文为空（finish_reason=%s，max_tokens=%d 被推理占满）"
                % (choice.get("finish_reason"), max_tokens)
            )
        raise LLMError("LLM 返回空内容: %s" % str(data)[:300])
    return content


def probe() -> dict:
    """连通性自检，不产出内容。"""
    info = {
        "configured": config.llm_configured(),
        "base_url": config.LLM_BASE_URL or None,
        "model": config.LLM_MODEL,
        "reasoning_effort": config.LLM_REASONING_EFFORT or None,
    }
    if not info["configured"]:
        info["ok"] = False
        info["error"] = "未配置"
        return info
    try:
        out = chat([{"role": "user", "content": "回复两个字：就绪"}], max_tokens=200)
        info["ok"] = True
        info["sample"] = out[:40]
    except LLMError as exc:
        info["ok"] = False
        info["error"] = str(exc)[:300]
    return info
