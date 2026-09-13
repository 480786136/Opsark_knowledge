"""Backend-neutral chunk identity and export contract. SQL owns publication state."""

import hashlib
import json

CHUNKER_VERSION = "sections-v2"


def chunk_content(content, limit=1800):
    """Keep original line coordinates and section boundaries; bound long lines."""
    rows, buffer, size, start = [], [], 0, 1
    in_fence = False
    fence = ""
    for number, line in enumerate(content.splitlines(), 1):
        heading = not in_fence and line.startswith("#")
        if buffer and (heading or size + len(line) + 1 > limit):
            rows.append(("\n".join(buffer), start, number - 1))
            buffer, size = [], 0
        if not buffer:
            start = number
        stripped = line.lstrip()
        if stripped.startswith(("~~~", "```")):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence = False
        if len(line) > limit:
            for offset in range(0, len(line), limit):
                rows.append((line[offset : offset + limit], number, number))
        else:
            buffer.append(line)
            size += len(line) + 1
    if buffer:
        rows.append(("\n".join(buffer), start, len(content.splitlines())))
    return [row for row in rows if row[0].strip()]


def chunk_id(version_id, ordinal, content):
    value = json.dumps(
        [CHUNKER_VERSION, version_id, ordinal, content], ensure_ascii=False
    )
    return hashlib.sha256(value.encode()).hexdigest()


def vector_entity(chunk, version, document):
    """Export a published SQL chunk for a future vector-store upsert.

    Consumers must recheck SQL authorization and published_version on retrieval.
    This is an export shape, not an external synchronization implementation.
    """
    if chunk.version_id != version.id or version.document_id != document.id:
        raise ValueError("INDEX_IDENTITY_MISMATCH")
    if document.published_version != version.version:
        raise ValueError("INDEX_VERSION_NOT_PUBLISHED")
    return {
        "id": chunk.id,
        "knowledge_base_id": document.knowledge_base_id,
        "document_id": document.id,
        "version_id": version.id,
        "document_version": version.version,
        "source_record_id": document.source_record_id or "",
        "index_version": version.index_version,
        "content_hash": hashlib.sha256(chunk.content.encode()).hexdigest(),
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "environment": version.environment,
        "software_names": version.software_names,
        "title": version.title,
        "content": chunk.content,
        "embedding": chunk.embedding,
    }
