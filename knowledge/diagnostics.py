"""Log safe technical metadata, never exception messages, SQL parameters or source text."""

import json
import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .security import ApiError
from .config import settings

AI_CODES = {
    "AI_NOT_CONFIGURED",
    "AI_INVALID_ENDPOINT",
    "AI_INPUT_TOO_LARGE",
    "AI_TIMEOUT",
    "AI_OUTPUT_TOO_LARGE",
    "AI_INCOMPLETE_OUTPUT",
    "AI_INVALID_REFERENCE",
    "AI_STALE_DRAFT",
    "AI_SOURCE_UNAVAILABLE",
    "AI_EVIDENCE_TYPE_MISMATCH",
    "AI_INVALID_REFERENCE",
}


def failure_code(exc):
    if isinstance(exc, ValueError) and str(exc) in AI_CODES:
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "AI_TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError):
        return "AI_HTTP_ERROR"
    if isinstance(exc, httpx.RequestError):
        return "AI_CONNECTION_ERROR"
    if isinstance(exc, ValidationError):
        return "AI_SCHEMA_INVALID"
    if isinstance(
        exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError)
    ):
        return "AI_RESPONSE_INVALID"
    if isinstance(exc, ApiError):
        return "AI_SENSITIVE_CONTENT"
    if isinstance(exc, SQLAlchemyError):
        return "AI_DATABASE_ERROR"
    return "AI_REFINEMENT_FAILED"


def log_failure(exc, job_id, target, revision):
    info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "knowledge_refinement_failed",
        "job_id": job_id,
        "document_id": target,
        "revision": revision,
        "code": failure_code(exc),
        "exception_type": type(exc).__name__,
        "stage": "worker_or_database",
        **getattr(exc, "ai_diagnostic", {}),
    }
    info["stack"] = [
        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)
    ]
    if isinstance(exc, ValidationError):
        info["validation_errors"] = [
            {
                "type": error["type"],
                "path": [
                    part
                    if isinstance(part, int)
                    or part in {"title", "claims", "section", "kind", "text", "refs"}
                    else "[field]"
                    for part in error["loc"]
                ],
            }
            for error in exc.errors(
                include_input=False, include_context=False, include_url=False
            )[:20]
        ]
    if isinstance(exc, SQLAlchemyError):
        info["database_exception_type"] = type(getattr(exc, "orig", None)).__name__
    logger = logging.getLogger(__name__)
    if not any(type(h) is logging.StreamHandler for h in logger.handlers):
        logger.addHandler(logging.StreamHandler())
    message = json.dumps(info, ensure_ascii=False)
    # Console output remains available if the configured log directory is not writable.
    path = settings().worker_log_path
    if path:
        try:
            absolute = str(Path(path).resolve())
            for existing in list(logger.handlers):
                if (
                    isinstance(existing, RotatingFileHandler)
                    and existing.baseFilename != absolute
                ):
                    logger.removeHandler(existing)
                    existing.close()
            if not any(
                isinstance(h, RotatingFileHandler) and h.baseFilename == absolute
                for h in logger.handlers
            ):
                Path(absolute).parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    absolute, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
                logger.addHandler(handler)
        except OSError:
            logger.error("worker_log_file_unavailable; diagnostic follows on stderr")
    logger.error(message)
    return info
