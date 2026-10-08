"""Backend-neutral chunk identity and export contract. SQL owns publication state."""

import hashlib
import json
import re

CHUNKER_VERSION = "sections-v3"


def chunk_content(content, limit=1800):
    """Preserve bounded code blocks and exact original line coordinates.

    Oversized blocks/lines are split without inventing fences or citation lines.
    Each emitted body is bounded even when a single source line is enormous.
    """
    if limit < 1:
        raise ValueError("CHUNK_LIMIT_INVALID")
    lines, units, number = content.splitlines(), [], 0
    while number < len(lines):
        start = number
        opening = re.match(r"^\s*(`{3,}|~{3,})", lines[number])
        if opening:
            marker = opening.group(1)
            number += 1
            while number < len(lines):
                closed = re.fullmatch(
                    r"\s*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*",
                    lines[number],
                )
                number += 1
                if closed:
                    break
        else:
            number += 1
        text = "\n".join(lines[start:number])
        heading = not opening and bool(re.match(r"^#{1,6}\s", lines[start]))
        if len(text) <= limit:
            units.append((text, start + 1, number, heading))
        else:
            for index in range(start, number):
                line = lines[index]
                for offset in range(0, max(1, len(line)), limit):
                    units.append(
                        (
                            line[offset : offset + limit],
                            index + 1,
                            index + 1,
                            heading and index == start and offset == 0,
                        )
                    )
    rows, buffer, first, last = [], "", 0, 0
    for text, start, end, heading in units:
        # Same-line fragments cannot be joined with a fabricated newline.
        if first and (heading or start <= last or len(buffer) + len(text) + 1 > limit):
            rows.append((buffer, first, last))
            buffer, first = "", 0
        if first:
            buffer += "\n" + text
        else:
            buffer, first = text, start
        last = end
    if first:
        rows.append((buffer, first, last))
    return [row for row in rows if row[0].strip()]


def chunk_id(version_id, ordinal, content):
    value = json.dumps(
        [CHUNKER_VERSION, version_id, ordinal, content], ensure_ascii=False
    )
    return hashlib.sha256(value.encode()).hexdigest()


def vector_entity(chunk, version, document, build=None):
    """Export a published SQL chunk for a future vector-store upsert.

    Consumers must recheck SQL authorization and published_version on retrieval.
    This is an export shape, not an external synchronization implementation.
    """
    if chunk.version_id != version.id or version.document_id != document.id:
        raise ValueError("INDEX_IDENTITY_MISMATCH")
    if document.published_version != version.version:
        raise ValueError("INDEX_VERSION_NOT_PUBLISHED")
    if getattr(document, "superseded_by", None) or getattr(
        document, "status", "published"
    ) in {"deleted", "rejected"}:
        raise ValueError("INDEX_VERSION_NOT_PUBLISHED")
    if (
        getattr(version, "active_build_id", None)
        and getattr(chunk, "build_id", None) != version.active_build_id
    ):
        raise ValueError("INDEX_BUILD_NOT_ACTIVE")
    if getattr(version, "active_build_id", None) and (
        build is None
        or build.id != version.active_build_id
        or build.version_id != version.id
        or build.status != "ready"
    ):
        raise ValueError("INDEX_BUILD_REQUIRED")
    return {
        "id": chunk.id,
        "knowledge_base_id": document.knowledge_base_id,
        "document_id": document.id,
        "version_id": version.id,
        "document_version": version.version,
        "source_record_id": next(
            iter(getattr(version, "source_record_ids", [document.source_record_id])), ""
        )
        or "",
        "source_record_ids": getattr(
            version,
            "source_record_ids",
            [document.source_record_id] if document.source_record_id else [],
        ),
        "index_version": build.index_version if build else version.index_version,
        "build_id": build.id if build else None,
        "content_hash": hashlib.sha256(chunk.content.encode()).hexdigest(),
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "environment": version.environment,
        "software_names": version.software_names,
        "title": version.title,
        "content": chunk.content,
        "embedding": chunk.embedding,
    }
