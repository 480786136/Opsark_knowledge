"""Typed, source-preserving input preparation for experience-v2."""

import re

OBSERVED_KINDS = frozenset({"command_result", "validation", "observation"})


def evidence_kind(item):
    """Normalize legacy expectations and summary-only claims without changing source."""
    if (
        item["kind"] == "expectation"
        or re.match(r"expected(?:[-_]|$)", item["evidence_id"], re.IGNORECASE)
        or re.search(
            r"预期验收|(?:^|\s)预期[:：]|不是已验证事实", item.get("summary", "")
        )
    ):
        return "expectation"
    if item["kind"] in OBSERVED_KINDS and not item.get("excerpt", "").strip():
        return "client_report"
    return item["kind"]


def prepare_evidence(payload):
    catalog = {}
    warnings = []
    metrics = {
        "step_count": len(payload.get("steps", [])),
        "evidence_count": 0,
        "observed_excerpt_count": 0,
        "summary_only_count": 0,
        "expectation_count": 0,
        "failed_step_count": 0,
        "unknown_execution_count": 0,
        "unverified_step_count": 0,
        "truncated_evidence_count": 0,
    }

    def put(ref, kind, value):
        if ref in catalog:
            raise ValueError("AI_INVALID_REFERENCE")
        catalog[ref] = {"kind": kind, "value": value}

    put("record/context", "context", payload.get("context", {}))
    put("record/outcome", "client_report", payload.get("outcome", {}))
    put("record/problem", "client_report", payload.get("problem", ""))
    for step in payload.get("steps", []):
        ident = step["step_id"]
        put(f"{ident}/description", "client_report", step.get("description", ""))
        put(f"{ident}/command", "command", step.get("command", ""))
        put(
            f"{ident}/status",
            "client_report",
            {
                "execution_status": step["execution_status"],
                "validation_status": step["validation_status"],
            },
        )
        for item in step["evidence"]:
            kind = evidence_kind(item)
            ref = f"{ident}/{item['evidence_id']}"
            put(ref, kind, item)
            # Preserve the supplied kind for audit while making the citable type
            # explicit. An empty excerpt is not proof that a command had no output.
            catalog[ref]["source_kind"] = item["kind"]
            metrics["evidence_count"] += 1
            metrics["observed_excerpt_count"] += int(kind in OBSERVED_KINDS)
            metrics["summary_only_count"] += int(kind == "client_report")
            metrics["expectation_count"] += int(kind == "expectation")
            if kind == "client_report":
                warnings.append(
                    f"{ref}: 没有原始输出摘录，仅有来源摘要；不能据此认定执行或验收事实。"
                )
            if re.search(
                r"已截断|输出截断|\[.*?truncat(?:ed|ion).*?\]",
                item.get("excerpt", ""),
                re.IGNORECASE,
            ):
                metrics["truncated_evidence_count"] += 1
                warnings.append(
                    f"{ref}: 摘录含截断标记，只能核对可见片段，不能推断完整输出。"
                )
        if step["validation_status"] == "passed" and not any(
            catalog[f"{ident}/{item['evidence_id']}"]["kind"] == "validation"
            for item in step["evidence"]
        ):
            warnings.append(
                f"{ident}: 客户端声明校验通过，但没有独立校验证据；不能据此认定已验收。"
            )
        if step["execution_status"] in {"failed", "blocked"}:
            metrics["failed_step_count"] += 1
            warnings.append(
                f"{ident}: 来源报告失败或被阻止；后续尝试成功不抹去本次失败记录。"
            )
        if step["execution_status"] in {"unknown", "not_run"}:
            metrics["unknown_execution_count"] += 1
            warnings.append(
                f"{ident}: 执行状态为 {step['execution_status']}，不能推断该步骤已执行成功。"
            )
        if step["validation_status"] != "passed" or not any(
            catalog[f"{ident}/{item['evidence_id']}"]["kind"] == "validation"
            for item in step["evidence"]
        ):
            metrics["unverified_step_count"] += 1
        if not step["evidence"]:
            warnings.append(
                f"{ident}: 来源没有证据条目；步骤描述与状态均只是客户端声明。"
            )
        if re.search(r"2\s*>\s*/dev/null|\|\|\s*(?:true|:)", step.get("command", "")):
            warnings.append(
                f"{ident}: 命令存在错误信息抑制；无输出不能单独证明不存在目标。"
            )
    context = payload.get("context", {})
    missing = [
        key
        for key in ("server_ref", "environment", "software", "runtime")
        if not context.get(key)
    ]
    if missing:
        warnings.append("环境字段缺失：" + "、".join(missing) + "；禁止推断为已确认。")
    runtime = context.get("runtime") or {}
    missing_runtime = [
        key
        for key in ("os", "shell", "scope", "privilege", "visibility")
        if not str(runtime.get(key, "")).strip()
    ]
    if missing_runtime:
        warnings.append(
            "运行环境细项未提供："
            + "、".join(missing_runtime)
            + "；已有字段仅为来源声明。"
        )
    if payload.get("outcome", {}).get("status") == "succeeded" and (
        metrics["failed_step_count"]
        or metrics["unknown_execution_count"]
        or not metrics["observed_excerpt_count"]
    ):
        warnings.append(
            "客户端整体结果报告成功，但存在失败、未知步骤或缺少原始摘录；最终结果需逐项审核。"
        )
    # Quality warnings are derived from the source record and are useful context for
    # the model. Give them stable evidence IDs so every citable value sent to the
    # model follows the same contract and remains auditable in the source catalog.
    for index, warning in enumerate(warnings, start=1):
        put(f"record/quality-warning-{index}", "quality_warning", warning)
    metrics["warning_count"] = len(warnings)
    return {
        "problem": payload.get("problem", ""),
        "evidence": catalog,
        "quality_warnings": warnings,
        "quality_metrics": metrics,
    }


def validate_claims(claims, catalog):
    for claim in claims:
        if not set(claim.refs).issubset(catalog):
            raise ValueError("AI_INVALID_REFERENCE")
        kinds = {catalog[ref]["kind"] for ref in claim.refs}
        if claim.kind == "事实":
            if not kinds:
                raise ValueError("AI_INVALID_REFERENCE")
            if not kinds.issubset({"command_result", "validation", "observation"}):
                raise ValueError("AI_EVIDENCE_TYPE_MISMATCH")
        elif claim.kind == "验收要求":
            if not kinds or kinds != {"expectation"}:
                raise ValueError("AI_EVIDENCE_TYPE_MISMATCH")
        elif claim.kind == "来源声明":
            if not kinds or not kinds.issubset({"context", "client_report", "command"}):
                raise ValueError("AI_EVIDENCE_TYPE_MISMATCH")
