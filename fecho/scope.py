"""工作范围：哪些目录的活动可以进工作日志。

这是隐私边界，不是功能开关，所以有两条硬规则：

1. **默认不扫。** 没登记过的目录一律跳过。失败方向必须是「少记」而不是
   「泄露」——少记你看日报时会发现，补一条就行；泄露不可逆。
2. **只靠路径判断，不靠模型判断。** 一旦想「让模型看一眼再决定这是不是私事」，
   内容已经发到公司的 LLM 端点了，泄露已经发生。所以这一层必须在调 LLM
   之前完成，且只能用本地元数据。

判定顺序（先命中先赢）：
    ignore 片段 → 忽略
    显式登记为 work / 绑了 issue → 工作
    work 前缀 → 工作
    其余 → 未登记（跳过，但把**路径**报出来让人一次性决定；绝不报内容）
"""
from typing import Any, Dict, List, Optional, Tuple

from . import config


def _rules() -> Dict[str, List[str]]:
    s = config.load().get("scope") or {}
    return {
        "work_prefixes": list(s.get("work_prefixes") or []),
        "work": list(s.get("work") or []),
        "ignore": list(s.get("ignore") or []),
    }


def classify(path: Optional[str]) -> Tuple[str, str]:
    """返回 (verdict, why)。verdict ∈ {work, ignored, unregistered}。"""
    if not path:
        return "unregistered", "没有路径信息"
    p = str(path)
    r = _rules()

    for frag in r["ignore"]:
        if frag and frag in p:
            return "ignored", "匹配忽略规则 %s" % frag

    for frag in r["work"]:
        if frag and frag in p:
            return "work", "已登记为工作项目 %s" % frag

    from . import match
    bound = match.project_binding(p)
    if bound:
        return "work", "绑定了 %s" % bound

    for prefix in r["work_prefixes"]:
        if prefix and p.startswith(prefix):
            return "work", "在工作前缀 %s 之下" % prefix

    return "unregistered", "没登记过"


def is_work(path: Optional[str]) -> bool:
    return classify(path)[0] == "work"


def add(kind: str, value: str) -> Dict[str, Any]:
    """kind ∈ {work_prefixes, work, ignore}。"""
    r = _rules()
    if kind not in r:
        raise ValueError("未知类别: %s" % kind)
    if value not in r[kind]:
        r[kind].append(value)
    config.update(scope=r)
    return r


def remove(value: str) -> Dict[str, Any]:
    r = _rules()
    for kind in r:
        r[kind] = [v for v in r[kind] if v != value]
    config.update(scope=r)
    return r


def rules() -> Dict[str, List[str]]:
    return _rules()
