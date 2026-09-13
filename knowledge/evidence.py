"""Typed, source-preserving input preparation for experience-v2."""

import re


def prepare_evidence(payload):
    catalog = {}
    warnings = []

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
            kind = item["kind"]
            # Older clients encode expectations as observations.
            if (
                kind == "expectation"
                or item["evidence_id"].startswith("expected-")
                or re.search(r"预期验收|不是已验证事实", item["summary"])
            ):
                kind = "expectation"
            put(f"{ident}/{item['evidence_id']}", kind, item)
        if step["validation_status"] == "passed" and not any(
            catalog[f"{ident}/{item['evidence_id']}"]["kind"] == "validation"
            and (item.get("excerpt", "").strip() or item.get("summary", "").strip())
            for item in step["evidence"]
        ):
            warnings.append(
                f"{ident}: 客户端声明校验通过，但没有独立校验证据；不能据此认定已验收。"
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
    return {
        "problem": payload.get("problem", ""),
        "evidence": catalog,
        "quality_warnings": warnings,
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
