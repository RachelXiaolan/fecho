"""Pure transcript-scan rules shared with the standalone client.

Keep this module limited to the stdlib so its source can be embedded in the
single-file scanner distributed to user machines.
"""
import hashlib
import re


DEFAULT_CHUNK_CHARS = 18000
BOILERPLATE_PATTERN = (
    r"<command-(message|name|args)>|<system-reminder>|<local-command-|"
    r"Base directory for this skill:|<user-prompt-submit-hook>"
)
BOILERPLATE = re.compile(BOILERPLATE_PATTERN, re.MULTILINE)

PROMPT = """你在读一段「人和 coding agent 一起干活」的对话记录，任务是抽出这段时间**做成了什么**。

规则：
- 抽的是**成果和进展**，不是对话内容，也不是用户说过的话。
- 一件事一条。同一件事反复出现，合并成一条。
- **踩的坑、得出的负面结论也算进展**——「试了 X 发现不行，因为 Y」是有价值的记录。
- 只写对话里确实发生的事，不许推断、不许补充没做的事。
- 忽略纯粹的来回确认、纯提问、没有结论的讨论。
- **不许把「还在讨论/倾向于」写成「已决定」**。只有明确拍板的才用「定为/改成/确定」，
  还在比较的要写「在评估 X 和 Y」。
- 每条一到两句话，让人三个月后还看得懂。
- **必须用中文写**，无论对话本身是什么语言。技术名词（MCP、OAuth、SQLite 等）保留原文。

还要判断每条进展属于下面哪个 issue：
- 看的是**说的是不是同一件事**，不是字面有没有重合的词。
- 真的都不属于就写 `-`，系统会归到自由任务。**宁可写 `-` 也不要硬凑**——
  归错了下游的日报全跟着错，归不上只是多一个自由任务。
- 只能从下面给的列表里选，不许自己编 issue 号。
- 如果这段没有任何已经完成、推进或明确踩坑的内容，只输出一行 `NONE`。

{issues}

输出格式：一行一条，`类型 | issue号或-| 内容`，类型是 done / pitfall / decision 三者之一。
不要 JSON、不要代码块、不要编号、不要解释。内容里随便用什么标点都行。

示例：
done | AI-2541 | 配对引擎写完了，拿真实 issue 测下来 11/12 命中
pitfall | AI-2541 | 让模型自己数中文字数会把推理预算烧穿，改成给结构性目标才出得来
decision | - | 闲鱼选品定了强推三个品类，盗版资料类全部淘汰

对话记录如下：

"""

NO_ISSUES = "（当前没有在办的 issue，所有条目的 issue 号都写 `-`）"
PROBE_TEXT = "（这是安装时的连通测试，没有对话内容。）"


def parse_scan_entries(raw, valid_keys=None, issue_pattern=None):
    """Parse the line protocol emitted by the transcript summarizer."""
    cleaned = re.sub(r"^```\w*\s*|\s*```$", "", (raw or "").strip())
    if cleaned.upper() == "NONE":
        return []
    out = []
    for line in cleaned.splitlines():
        line = line.strip().lstrip("-*0123456789. ")
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            continue
        kind = parts[0].lower()
        if kind not in ("done", "pitfall", "decision"):
            continue
        issue, content = (parts[1], "|".join(parts[2:]).strip()) if len(parts) >= 3 else ("", parts[1])
        if not content:
            continue
        issue = issue.upper()
        valid_issue = bool(issue_pattern and issue_pattern.fullmatch(issue))
        if valid_issue and valid_keys is not None:
            valid_issue = issue in valid_keys
        out.append({"kind": kind, "content": content,
                    "issue": issue if valid_issue else None})
    return out


def scan_event_key(producer, session_id, part, item_index):
    """Stable idempotency key for one summarized transcript chunk."""
    seed = "|".join((producer, session_id, part[0]["ts"], part[-1]["ts"], str(item_index)))
    return "scan:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
