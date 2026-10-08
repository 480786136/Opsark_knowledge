"""Validate an offline pack; explicitly import manual drafts into a local KB.

No SQL access, auto-publication, AI calls, updates or deletion. Sequential reruns
skip identical tagged drafts; concurrent imports are NOT supported by this API.
"""

import argparse
import getpass
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_PACK = ROOT / "knowledge_packs/ops_starter_v1/catalog.json"
ADMIN = "/api/admin/v1/knowledge"


def render(pack, item):
    return (
        f"# {item['title']}\n\n"
        "参考资料，待人工审核；未在目标环境执行，不是成功案例或操作授权。\n"
        f"资料编号：{pack['pack_id']}:{item['id']}；来源核对：{item['verified_at']}。\n\n"
        f"## 适用范围\n\n{item['scope']}\n\n"
        f"## 问题与诊断\n\n{item['symptoms']}\n\n"
        f"{item['interpretation']}\n\n"
        f"## 低影响检查示例（未执行）\n\n```text\n{item['commands']}\n```\n\n"
        "占位符必须替换为已确认且获授权的目标；本工具不会执行命令。\n\n"
        f"## 验收要求（不是执行结果）\n\n{item['acceptance']}\n\n"
        f"## 风险与禁止自动操作\n\n{item['risks']}\n\n"
        "## 官方参考\n\n"
        + "\n".join(f"- [来源 {i}]({url})" for i, url in enumerate(item["sources"], 1))
        + "\n"
    )


def marker(pack, item):
    return f"{pack['pack_id']}:{item['id']}"


def document_payload(pack, item, base_id):
    from knowledge.schemas import DocumentInput

    return DocumentInput(
        knowledge_base_id=base_id,
        title=item["title"],
        content=render(pack, item),
        tags=[marker(pack, item), "参考资料", "未执行"],
        software_names=item["software"],
        context={"software": [{"name": name} for name in item["software"]]},
    ).model_dump()


def load_pack(path=DEFAULT_PACK):
    from knowledge.security import check_sensitive

    pack = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        pack.get("schema_version") != 1
        or pack.get("kind") != "curated_reference"
        or pack.get("execution_status") != "not_executed"
        or pack.get("review_status") != "pending"
    ):
        raise ValueError("知识包必须是 v1 待审核、未执行的参考资料")
    if not re.fullmatch(r"[a-z0-9-]{1,12}", pack.get("pack_id", "")):
        raise ValueError("非法知识包编号")
    items = pack.get("documents", [])
    if not items or len(items) > 200:
        raise ValueError("单次知识包限 1..200 篇")
    seen = set()
    for item in items:
        ident = item.get("id", "")
        if not re.fullmatch(r"[a-z0-9-]{1,27}", ident) or ident in seen:
            raise ValueError("知识条目编号重复或非法")
        seen.add(ident)
        date.fromisoformat(item["verified_at"])
        for name in (
            "title",
            "scope",
            "symptoms",
            "commands",
            "interpretation",
            "acceptance",
            "risks",
        ):
            if not isinstance(item.get(name), str) or not item[name].strip():
                raise ValueError(f"{ident}: 缺少 {name}")
        if not item.get("sources") or any(
            urlsplit(url).scheme != "https"
            or not urlsplit(url).hostname
            or urlsplit(url).username
            or urlsplit(url).password
            for url in item["sources"]
        ):
            raise ValueError(f"{ident}: 必须提供 HTTPS 来源")
        payload = document_payload(pack, item, "offline-validation")
        check_sensitive(payload)
    return pack


def local_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "只允许明确的本机 IP 地址（127.0.0.1 或 [::1]），不能带凭据或路径"
        )
    _ = parsed.port  # Reject malformed ports before requesting a password.
    return value.rstrip("/")


def checked(response, expected=200):
    if response.status_code != expected:
        # Do not print responses: they may contain private data or credentials.
        raise RuntimeError(
            f"知识服务请求失败，HTTP {response.status_code}；已创建的草稿不会回滚"
        )
    return response.json()


def pages(client, path):
    found = []
    for offset in range(0, 1000001, 200):
        part = checked(client.get(path, params={"offset": offset, "limit": 200}))
        found.extend(part)
        if len(part) < 200:
            return found
    raise RuntimeError("数据超过分页预算，停止导入")


def import_drafts(client, pack, base_id):
    """Requires an authenticated admin client. Never updates existing documents."""
    bases = pages(client, ADMIN + "/knowledge-bases")
    if not any(row["id"] == base_id and row["enabled"] for row in bases):
        raise ValueError("目标知识库不存在或未启用，请先在管理界面新建独立测试库")
    existing = [
        row
        for row in pages(client, ADMIN + "/documents")
        if row["knowledge_base_id"] == base_id
    ]
    work, result = [], []
    # Preflight every conflict before the first document write.
    for item in pack["documents"]:
        payload = document_payload(pack, item, base_id)
        matches = [row for row in existing if marker(pack, item) in row["tags"]]
        if len(matches) > 1:
            raise ValueError(f"{item['id']}: 目标库已存在重复标记，请人工处理")
        if matches:
            row = matches[0]
            if any(row.get(key) != value for key, value in payload.items()):
                raise ValueError(f"{item['id']}: 已有内容不同；保护人工编辑，停止导入")
            result.append(
                {"pack_item": item["id"], "document_id": row["id"], "action": "skipped"}
            )
        else:
            work.append((item, payload))
    for item, payload in work:
        row = checked(client.post(ADMIN + "/documents", json=payload), 201)
        if row["status"] != "draft" or row.get("published_version") is not None:
            raise RuntimeError("服务返回非预期待审核状态，停止导入")
        result.append(
            {
                "pack_item": item["id"],
                "document_id": row["id"],
                "action": "created_draft",
            }
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    parser.add_argument(
        "--apply", action="store_true", help="显式允许创建草稿；默认仅离线检查"
    )
    parser.add_argument("--base-id", help="管理界面中已创建的独立测试知识库 ID")
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args(argv)
    pack = load_pack(args.pack)
    if not args.apply:
        print(
            json.dumps(
                {
                    "mode": "offline",
                    "pack_id": pack["pack_id"],
                    "documents": len(pack["documents"]),
                    "writes": 0,
                    "titles": [item["title"] for item in pack["documents"]],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not args.base_id:
        parser.error("--apply 必须提供 --base-id；不会自动选择或新建正式知识库")
    url = local_url(args.url)
    password = getpass.getpass("本机知识管理员密码（不保存）: ")
    with httpx.Client(
        base_url=url, timeout=30, trust_env=False, follow_redirects=False
    ) as client:
        login = checked(
            client.post(
                "/api/admin/v1/session",
                json={"username": args.username, "password": password},
            )
        )
        password = ""
        client.headers["X-CSRF-Token"] = login["csrf"]
        try:
            results = import_drafts(client, pack, args.base_id)
            print(
                json.dumps(
                    {"mode": "drafts_only", "results": results},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            try:
                checked(client.delete("/api/admin/v1/session"))
            except (RuntimeError, httpx.HTTPError):
                print("提示：会话注销未确认；请在管理端检查会话。", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError:
        raise SystemExit(
            "本机连接失败；结果可能部分完成，请勿并行重试。串行重跑会核对已有标记。"
        ) from None
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
