"""把一条进展配到一个任务上。

优先级从确定到模糊，先命中先赢：
  1. explicit        —— 正文里写了 issue 号，或调用时直接指定
  2. mobius-auto     —— 和「我名下在办的 Mobius issue」标题够像
  3. task-continue   —— 接着同一个对话里刚才那个任务，或近期的自由任务
  4. new-task        —— 都不是，新立一个自由任务

打分对中英混排做了区分：中文比 bigram 的**包含度**（不是 Jaccard——进展句通常
比 issue 标题短很多，Jaccard 会被长度差压死）；英文和带数字的 token 单独算，
像 awesome-gpt-image-2 / lu3 / MCP 这种词一旦对上就是强信号。
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


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall(_norm(text)) if not t.isdigit() or len(t) >= 3}


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


def score(content: str, title: str) -> float:
    tb, cb = _cjk_bigrams(title), _cjk_bigrams(content)
    cjk = len(tb & cb) / min(len(tb), len(cb)) if tb and cb else 0.0

    tt, ct = _tokens(title), _tokens(content)
    shared = tt & ct
    tok = len(shared) / len(tt) if tt else 0.0

    base = 0.65 * cjk + 0.35 * tok
    if any(_distinctive(t) for t in shared):
        base = max(base, 0.55)          # 罕见词命中，单独就够定案

    run = _longest_common_run(_cjk_seq(content), _cjk_seq(title))
    if run >= 4:
        base = max(base, 0.55)          # 四字连续共现，基本可以定案
    elif run == 3:
        base = max(base, 0.38)          # 三字，够过线但仍留给上下文纠正
    return round(min(base, 1.0), 3)


def best_issue(
    content: str, issues: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], float]:
    best, best_s = None, 0.0
    for i in issues:
        s = score(content, i["title"])
        if s > best_s:
            best, best_s = i, s
    return best, best_s


def best_task(
    content: str,
    tasks: List[Dict[str, Any]],
    recent_texts: Dict[str, str],
) -> Tuple[Optional[Dict[str, Any]], float]:
    """和已有任务比：既比任务标题，也比这个任务下最近的进展原文。"""
    best, best_s = None, 0.0
    for t in tasks:
        s = max(score(content, t["title"]), score(content, recent_texts.get(t["task_id"], "")))
        if s > best_s:
            best, best_s = t, s
    return best, best_s


def project_binding(project: Optional[str]) -> Optional[str]:
    """这个工作目录绑到了哪个 issue。最长的匹配片段胜出，避免父目录抢走子目录。"""
    if not project:
        return None
    p = str(project)
    hit = [(len(frag), key) for frag, key in config.PROJECT_BINDINGS.items() if frag and frag in p]
    return max(hit)[1] if hit else None


def decide(
    content: str,
    issues: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    recent_texts: Dict[str, str],
    session_task_id: Optional[str] = None,
    explicit_issue: Optional[str] = None,
    explicit_task_id: Optional[str] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """返回 {method, issue_key?, task_id?, score, runner_up?}。"""
    if explicit_task_id:
        return {"method": "explicit", "task_id": explicit_task_id, "score": 1.0}

    keys = ([explicit_issue] if explicit_issue else []) + extract_issue_keys(content)
    if keys:
        return {"method": "explicit", "issue_key": keys[0], "score": 1.0}

    issue, s_issue = best_issue(content, issues)
    if issue and s_issue >= config.MATCH_THRESHOLD:
        return {
            "method": "mobius-auto",
            "issue_key": issue["issue_key"],
            "title": issue["title"],
            "score": s_issue,
        }

    # 工作目录绑了 issue —— 用它。
    # 位置是刻意的：排在关键词证据**之后**（正文里明确提到别的 issue 时，那个更
    # 具体的信号该赢），但排在「接续已有任务」**之前**。后者是启发式打分，实测会
    # 被一个碰巧标题相近的旧自由任务截胡；绑定是人主动配的，更可信。
    bound = project_binding(project)
    if bound:
        return {"method": "project-bound", "issue_key": bound, "score": None,
                "via": "project:%s" % project}

    task, s_task = best_task(content, tasks, recent_texts)

    if task and s_task >= config.TASK_CONTINUE_THRESHOLD:
        return {"method": "task-continue", "task_id": task["task_id"], "score": s_task}

    # 到这里说明：正文里没有 issue 号，也配不到任何 issue 或已有任务。
    # 这种「接口跑通了」式的句子靠关键词永远判不出归属，只有一个信号可用——
    # 刚才在这个对话里推进的是哪个任务。所以同一对话的任务是**默认归属**，
    # 不再要求相似度（要求了就等于把这条路堵死）。
    #
    # 代价说清楚：同一个对话里换了话题、新话题又没有特征词时会错归。
    # V0 不假装能解决，只保证归属对 agent 可见（返回值里写明配对方式），
    # agent 判断错了可以带 issue 或 task_id 重记。
    if session_task_id and any(t["task_id"] == session_task_id for t in tasks):
        st = next(t for t in tasks if t["task_id"] == session_task_id)
        return {
            "method": "task-continue",
            "task_id": session_task_id,
            "score": max(score(content, st["title"]),
                         score(content, recent_texts.get(session_task_id, ""))),
            "via": "same-session",
            "confidence": "low",
        }


    return {
        "method": "new-task",
        "score": max(s_issue, s_task),
        "runner_up": (issue or {}).get("issue_key") if s_issue >= s_task else None,
    }
