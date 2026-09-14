"""每个人写日报的偏好：从他亲手改日报的地方学出来，以后出日报都照着写。

一人一份，存在服务器上，互相隔离——每个人的日报风格不一样。
人在网页上能看、能改、能清空：学偏了直接删掉那一条。

学的是**写法**，不是事实：改掉写错的内容、归错的任务，那是修正当天，不是偏好。
链接、状态图标、分段标题、任务名是代码拼的，模型碰不到，改了也不算偏好
（任务名的改动由 digest.save_human_edit 直接存成短名）。

偏好不算进日报指纹。算进去的话，偏好一更新，所有日报都会被标成「需要重新生成」。
"""
import re
from typing import Any, Dict, List, Optional

from . import db, llm, store

MAX_RULES = 15
MAX_CHARS = 4000

LEARN_PROMPT = """你在维护一份「这个人写日报的偏好」文档。对比模型写的日报和他亲手改过的版本，找出他在**写法**上的偏好，更新这份文档。

规则：
- 只总结写法和格式上的偏好：措辞、长短、语气、详略、要不要写某一类内容、条目怎么组织。
- 他改的如果是**事实**（写错的内容、归错的任务、漏掉或多写的事），那是修正当天的日报，不是偏好，不要写进去。
- 链接、✅ ⭕️ ❌ 这些状态图标、Done / In Progress 这些分段标题、任务名，是系统自动加的。他改了这些也不要写成偏好。
- 和已有偏好重复的合并成一条；和已有偏好冲突的，以这次的改动为准，删掉旧的那条。
- 每条一句话，写成下次可以直接照做的要求。最多 %d 条。
- 这次改动看不出任何写法上的偏好：已有文档就原样输出；没有已有文档就只输出一行 NONE。

输出：只输出更新后的完整文档，每条一行、以「- 」开头。不要标题、不要编号、不要解释。""" % MAX_RULES


def get(author: str) -> Dict[str, Any]:
    with db.cursor() as conn:
        row = conn.execute("SELECT content_md, updated_at FROM style_profiles WHERE author=?",
                           (author,)).fetchone()
    return dict(row) if row else {"content_md": "", "updated_at": None}


def save(author: str, content_md: str) -> Dict[str, Any]:
    """人在网页上改偏好，或者学习完写回。存空就是清空。"""
    content = (content_md or "").strip()
    if len(content) > MAX_CHARS:
        raise ValueError("写作偏好太长了（%d 字），最多 %d 字" % (len(content), MAX_CHARS))
    with db.cursor() as conn:
        if content:
            conn.execute(
                "INSERT INTO style_profiles (author, content_md, updated_at) VALUES (?,?,?)"
                " ON CONFLICT (author) DO UPDATE SET content_md=excluded.content_md,"
                " updated_at=excluded.updated_at", (author, content, store.now_iso()))
        else:
            conn.execute("DELETE FROM style_profiles WHERE author=?", (author,))
    return get(author)


def for_prompt(author: str) -> str:
    """拼进出日报提示词的那一段。没有偏好就是空串。"""
    content = get(author)["content_md"]
    if not content:
        return ""
    return ("\n这个人写日报的偏好（从他以前亲手改日报的地方总结出来的，写法照这些调整；"
            "和上面的硬规则冲突时以硬规则为准）：\n%s\n" % content)


def _parse(raw: str) -> Optional[List[str]]:
    text = re.sub(r"^```\w*\s*|\s*```$", "", (raw or "").strip())
    if text.upper() == "NONE":
        return None
    rules: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line[:2] in ("- ", "* ", "• "):
            body = line[2:].strip()
            if body and body not in rules:
                rules.append(body)
    return rules[:MAX_RULES]


def learn(author: str, date: str) -> Dict[str, Any]:
    """拿这天「改之前」和「改之后」的日报对比，更新这个人的写作偏好。

    模型调用失败直接抛出去：云端版由后台程序半小时后补跑一次。
    """
    current = db.get_report(author, date, "daily")
    if not current or current.get("generator") != "human":
        return {"status": "skipped", "reason": "这天的日报没有被人改过"}
    history = [h for h in db.report_history(author, date) if h["kind"] == "daily"]
    if not history:
        return {"status": "skipped", "reason": "找不到改之前的版本"}
    before, after = history[0]["content_md"], current["content_md"]
    if before.strip() == after.strip():
        return {"status": "skipped", "reason": "改前改后一样"}

    old = get(author)["content_md"]
    raw = llm.chat([
        {"role": "system", "content": LEARN_PROMPT},
        {"role": "user", "content": "已有的偏好文档：\n%s\n\n模型写的版本：\n%s\n\n他改后的版本：\n%s"
                                    % (old or "（还没有）", before, after)},
    ], temperature=0.2, max_tokens=3000)
    rules = _parse(raw)
    if not rules:
        return {"status": "unchanged", "content_md": old}
    content = "\n".join("- " + r for r in rules)
    save(author, content)
    return {"status": "learned", "content_md": content, "rules": len(rules)}
