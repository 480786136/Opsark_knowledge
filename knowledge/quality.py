"""Evidence-preserving draft preparation; never invent facts or execute source text."""

import re

RULESET = "knowledge-draft-v2"
STATUS = {
    "succeeded": "执行成功",
    "failed": "失败",
    "blocked": "被阻止",
    "not_run": "未执行",
    "unknown": "未知",
    "passed": "通过",
    "partial": "部分完成",
}


def code_block(text):
    # Source text may itself contain Markdown fences.
    fence = "`" * max(
        3, max((len(part) for part in re.findall(r"`+", text)), default=0) + 1
    )
    return f"{fence}\n{text}\n{fence}"


def prepare_draft(record):
    p = record.payload
    steps = p["steps"]
    context = p.get("context", {})
    issues = []
    sections = [
        f"# {p['title']}",
        "> 本文由来源证据规则整理，尚需人工审核；历史操作仅供参考，不代表当前环境可直接执行。",
        "## 任务目标",
        p["problem"],
        "## 适用范围与前提",
        f"环境：{context.get('environment') or '来源未提供，使用前需确认'}",
        "软件："
        + (
            "；".join(
                f"{s['name']} {s.get('version', '')}".strip()
                for s in context.get("software", [])
            )
            or "来源未提供版本信息"
        ),
        "步骤说明中的前提与预期需要和下方实际输出核对；未出现的条件不视为已满足。",
        "## 操作步骤",
    ]
    if not steps:
        issues.append("来源没有步骤，不能据此复现操作。")
    verification, outputs = [], []
    for index, step in enumerate(steps, 1):
        sections += [
            f"### {index}. 操作",
            step["description"],
            f"执行状态：{STATUS[step['execution_status']]}",
        ]
        if step.get("command"):
            sections.append(code_block(step["command"]))
        else:
            sections.append("命令未提供；不能从步骤名称推测命令。")
        validation = [e for e in step["evidence"] if e["kind"] == "validation"]
        if step["validation_status"] == "passed" and not validation:
            issues.append(
                f"步骤 {index} 声称校验通过，但缺少独立校验证据，需人工核对。"
            )
        if step["validation_status"] in {"unknown", "failed"}:
            issues.append(
                f"步骤 {index} 的独立校验为{STATUS[step['validation_status']]}。"
            )
        verification += [
            f"### 步骤 {index}",
            f"独立校验状态：{STATUS[step['validation_status']]}",
        ]
        if step["validation_status"] == "not_run":
            verification.append("未执行独立校验；主命令观测与独立验收应区别阅读。")
        for evidence in step["evidence"]:
            target = (
                verification
                if evidence["kind"] in {"validation", "observation", "expectation"}
                else outputs
            )
            target += [
                f"步骤 {index} · 证据 {evidence['evidence_id']}",
                evidence["summary"],
            ]
            if evidence.get("excerpt"):
                target.append(code_block(evidence["excerpt"]))
        if not any(e.get("excerpt") for e in step["evidence"]):
            issues.append(f"步骤 {index} 没有输出摘录，仅有状态摘要。")
        if step["execution_status"] in {"failed", "blocked"}:
            issues.append(
                f"保留步骤 {index} 的失败/阻止记录，不能将该尝试写成成功经验。"
            )
    sections += [
        "## 验收标准与校验证据",
        *verification,
        "## 关键执行输出",
        *(outputs or ["来源没有执行输出。"]),
        "## 结果与结论边界",
        f"客户端报告结果：{STATUS[p['outcome']['status']]}（未由知识服务重新执行验证）。",
        p["outcome"]["summary"],
        "## 审核与复用注意事项",
        *(f"- {issue}" for issue in issues),
        "- 核对目标、适用环境、命令和验收证据后再发布；不得补写来源不存在的成功结果。",
        "## 来源与整理记录",
        f"来源记录：{record.id}",
        f"客户端任务：{record.source_record_id} · 来源修订：{record.source_revision}",
        f"整理规则：{RULESET}；方式：规则整理（未调用 AI 模型）。",
    ]
    return "\n\n".join(sections)
