"""把一条进展配到一个任务上，以及判断两条进展是不是同一件事。

**归属不在这里判。** 拿进展的字面和 issue 标题比重合度这条路已经废掉了：
做 AI-2541 时说的是「配对引擎」「单测」，和标题「写一个提交工作日志的系统」
一个词都不重合；反过来，两句话都出现 agent 就能把闲鱼选品配进日志系统。
「这句话说的是不是这件事」交给读得懂意思的模型判（scan 里的 m3、或调用方
agent 自己），这里只认不会错的信号：

  1. explicit      —— 正文里写了 issue 号，或调用时直接指定
  2. project-bound —— 这个工作目录绑过 issue（人主动配的）
  3. same-session  —— 都没有时，跟着同一对话里刚才那个任务，标记为「猜的」
  4. new-task      —— 新立一个自由任务

写进库之后还会被 verify 那一步重判一次（见 digest.verify_assignments）——
上面第 1 条看着确定，其实也是 agent 的判断，一样会错。

`similarity` 是另一件事：比两条**进展之间**像不像，用来挡扫描重跑产生的近似
重复。文本对文本正是字符串相似度擅长的，和拿它去猜语义归属不是一回事。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config

ISSUE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
_PUNCT = re.compile(
    "[" + "".join("\\" + c for c in
        u",\u3002\uff01\uff1f\u3001\uff1b\uff1a\u201c\u201d\u2018\u2019"
        u"\uff08\uff09()[]{}<>\u00b7\u2014_*`#>|.,!?;:'\"/\\\\\uff0c\uff0f"
    ) + "]+")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-_.]{2,}")
_CJK = re.compile(r"[一-鿿]")
# 中文里到处都是、不带信息量的字，参与配对只会制造噪音
_STOP = set("的了和与在是我们你他她它个这那有为对把被从到就要会能不也都还很就已经把做了下过着上中里")


def extract_issue_keys(text: str) -> List[str]:
    seen, out = set(), []
    for m in ISSUE_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _norm(text: str) -> str:
    return _PUNCT.sub(" ", (text or "").lower())


def _cjk_bigrams(text: str) -> set:
    chars = [c for c in _norm(text) if _CJK.match(c) and c not in _STOP]
    s = "".join(chars)
    if len(s) < 2:
        return set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


# 在这个语境里满地都是、毫无区分度的词。实测 agent 一个词就能把闲鱼选品的内容
# 送进「写一个提交工作日志的系统」——标题里只有 agent / 专用 两个 latin token，
# 命中一个就是 1/2 的召回率，乘权重正好过线。
_COMMON_TOKENS = {
    "agent", "agents", "ai", "api", "app", "bug", "cli", "code", "data", "demo",
    "doc", "docs", "issue", "json", "llm", "log", "logs", "mcp", "pro", "test",
    "tests", "todo", "url", "web", "http", "https", "com", "www",
}


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall(_norm(text))
            if (not t.isdigit() or len(t) >= 3) and t not in _COMMON_TOKENS}


def _distinctive(tok: str) -> bool:
    """像 awesome-gpt-image-2 / lu3 / litellm 这种词，对上就基本能定案。"""
    return len(tok) >= 4 and (any(c.isdigit() for c in tok) or "-" in tok or len(tok) >= 6)


def _cjk_seq(text: str) -> str:
    return "".join(c for c in _norm(text) if _CJK.match(c) and c not in _STOP)


def _longest_common_run(a: str, b: str) -> int:
    """最长连续共现的中文片段长度。

    「工作日志」四个字连着出现，比零散撞上三个双字词强得多——
    前者几乎一定是在说同一件事，后者可能只是中文里的常用搭配。
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def similarity(a: str, b: str) -> float:
    """两段文本有多像。只用于近似重复判断，不要拿来猜语义归属。"""
    tb, cb = _cjk_bigrams(b), _cjk_bigrams(a)
    cjk = len(tb & cb) / min(len(tb), len(cb)) if tb and cb else 0.0

    tt, ct = _tokens(b), _tokens(a)
    shared = tt & ct
    tok = len(shared) / len(tt) if tt else 0.0

    base = 0.65 * cjk + 0.35 * tok
    if any(_distinctive(t) for t in shared):
        base = max(base, 0.55)          # 罕见词命中，单独就够定案

    run = _longest_common_run(_cjk_seq(a), _cjk_seq(b))
    if run >= 4:
        base = max(base, 0.55)          # 四字连续共现，基本可以定案
    elif run == 3:
        base = max(base, 0.38)          # 三字，够过线但仍留给上下文纠正
    return round(min(base, 1.0), 3)


def project_binding(project: Optional[str]) -> Optional[str]:
    """这个工作目录绑到了哪个 issue。最长的匹配片段胜出，避免父目录抢走子目录。"""
    if not project:
        return None
    p = str(project)
    hit = [(len(frag), key) for frag, key in config.PROJECT_BINDINGS.items() if frag and frag in p]
    return max(hit)[1] if hit else None


def decide(
    content: str,
    tasks: List[Dict[str, Any]],
    session_task_id: Optional[str] = None,
    explicit_issue: Optional[str] = None,
    explicit_task_id: Optional[str] = None,
    project: Optional[str] = None,
    allow_session_fallback: bool = True,
) -> Dict[str, Any]:
    """只认不会错的信号。语义归属由模型判，不在这里猜。

    返回 {method, issue_key?, task_id?, score, confidence?}。
    """
    if explicit_task_id:
        return {"method": "explicit", "task_id": explicit_task_id, "score": 1.0}

    keys = ([explicit_issue] if explicit_issue else []) + extract_issue_keys(content)
    if keys:
        return {"method": "explicit", "issue_key": keys[0], "score": 1.0}

    # 工作目录绑了 issue —— 人主动配的，比任何猜测都可信。
    bound = project_binding(project)
    if bound:
        return {"method": "project-bound", "issue_key": bound, "score": None,
                "via": "project:%s" % project}

    # 什么线索都没有时，跟着同一对话里刚才那个任务。这是**猜的**，
    # 但至少能把一段对话里的进展聚在一起，之后 verify 会重判。
    # 扫描来源关掉：批量抽取的条目共用一个 session_id，彼此没有先后关系，
    # 一条错会顺着惯性把后面全带偏。
    if allow_session_fallback and session_task_id and any(
            t["task_id"] == session_task_id for t in tasks):
        return {"method": "task-continue", "task_id": session_task_id, "score": None,
                "via": "same-session", "confidence": "low"}

    return {"method": "new-task", "score": None}
