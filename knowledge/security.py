import hashlib
import re
import secrets
import time
from collections import defaultdict, deque
from threading import Lock
from fastapi import Depends, Request
from sqlalchemy import select
from .db import get_db
from .models import ApiKey, KnowledgeBase
from .config import settings


class ApiError(Exception):
    def __init__(self, status, code, message="请求无法完成", fields=None):
        self.status, self.code, self.message, self.fields = (
            status,
            code,
            message,
            fields or [],
        )


def digest(value):
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def service_auth(request: Request):
    expected = settings().knowledge_service_token
    if len(expected) < 32 or not secrets.compare_digest(
        request.headers.get("authorization", ""), f"Bearer {expected}"
    ):
        raise ApiError(401, "INVALID_SERVICE_CREDENTIAL")
    return request.headers.get("x-opsark-actor", "platform")[:128]


_calls = defaultdict(deque)
_lock = Lock()


def require_key(scope):
    def auth(request: Request, db=Depends(get_db)):
        header = request.headers.get("authorization", "")
        key = (
            db.scalar(select(ApiKey).where(ApiKey.token_hash == digest(header[7:])))
            if header.startswith("Bearer ")
            else None
        )
        if not key or key.revoked or key.expires <= time.time():
            raise ApiError(401, "INVALID_API_KEY")
        if scope not in key.scopes:
            raise ApiError(403, "SCOPE_DENIED")
        now = time.monotonic()
        with _lock:
            for name in list(_calls):
                if not _calls[name] or _calls[name][-1] < now - 60:
                    del _calls[name]
            calls = _calls[(key.installation_id, scope)]
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if len(calls) >= (30 if scope == "records:write" else 60):
                raise ApiError(429, "RATE_LIMITED")
            calls.append(now)
        return key

    return auth


def check_bases(db, ids, key=None):
    if key and not set(ids).issubset(key.knowledge_base_ids):
        raise ApiError(403, "KNOWLEDGE_BASE_DENIED")
    found = set(
        db.scalars(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id.in_(ids), KnowledgeBase.enabled.is_(True)
            )
        )
    )
    if found != set(ids):
        raise ApiError(403, "KNOWLEDGE_BASE_DENIED")


_secret = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:authorization|cookie)\s*:\s*\S+|\b(?:api[_-]?key|password|passwd|secret|access[_-]?token)\s*[:=]\s*[\"']?[^\s\"']{6,}|\bsk-[A-Za-z0-9_-]{16,}",
    re.I,
)


def check_sensitive(value, path="body"):
    if isinstance(value, str) and _secret.search(value):
        raise ApiError(422, "SENSITIVE_CONTENT_DETECTED", "请脱敏后重试", [path])
    if isinstance(value, dict):
        for k, v in value.items():
            check_sensitive(v, f"{path}.{k}")
    if isinstance(value, list):
        for i, v in enumerate(value):
            check_sensitive(v, f"{path}[{i}]")
