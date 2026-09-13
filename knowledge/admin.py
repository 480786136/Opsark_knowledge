"""Local knowledge administration: no platform imports, cookies or service dependency."""

import secrets
import time
from collections import defaultdict, deque
from threading import Lock
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .config import settings
from .db import get_db
from .models import KnowledgeAdmin, KnowledgeSession
from .security import ApiError, digest

router = APIRouter(prefix="/api/admin/v1")
COOKIE = "opsark_knowledge_session"
attempts = defaultdict(deque)
lock = Lock()


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin not in settings().allowed_origins.split(","):
        raise ApiError(403, "ORIGIN_DENIED")


def require_admin(request: Request, db=Depends(get_db)):
    session = db.get(KnowledgeSession, digest(request.cookies.get(COOKIE, "")))
    if not session or session.expires <= time.time():
        raise ApiError(401, "SESSION_EXPIRED", "请登录知识管理员账户")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf
        ):
            raise ApiError(403, "CSRF_DENIED")
    request.state.knowledge_actor = session.admin_id
    return session


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/session")
def login(
    payload: LoginInput, request: Request, response: Response, db=Depends(get_db)
):
    check_origin(request)
    now = time.monotonic()
    with lock:
        for name in list(attempts):
            if not attempts[name] or attempts[name][-1] < now - 300:
                del attempts[name]
        history = attempts[request.client.host if request.client else "unknown"]
        while history and history[0] <= now - 300:
            history.popleft()
        if len(history) >= 10:
            raise ApiError(429, "RATE_LIMITED")
        history.append(now)
    admin = db.scalar(
        select(KnowledgeAdmin).where(KnowledgeAdmin.username == payload.username)
    )
    try:
        if not admin or not PasswordHasher().verify(
            admin.password_hash, payload.password
        ):
            raise ApiError(401, "INVALID_CREDENTIALS")
    except VerificationError:
        raise ApiError(401, "INVALID_CREDENTIALS")
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        KnowledgeSession(
            token_hash=digest(raw),
            admin_id=admin.id,
            csrf=csrf,
            expires=time.time() + 28800,
        )
    )
    db.commit()
    response.set_cookie(
        COOKIE,
        raw,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="strict",
        max_age=28800,
    )
    return {"username": admin.username, "csrf": csrf}


@router.get("/session")
def current(session=Depends(require_admin), db=Depends(get_db)):
    return {
        "username": db.get(KnowledgeAdmin, session.admin_id).username,
        "csrf": session.csrf,
    }


@router.delete("/session")
def logout(response: Response, session=Depends(require_admin), db=Depends(get_db)):
    db.delete(session)
    db.commit()
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/config")
def config(session=Depends(require_admin)):
    return {
        "knowledge_connected": True,
        "knowledge_base_url": "/api/v1",
        "ai_refinement_ready": bool(
            settings().ai_refinement_enabled
            and settings().ai_model
            and settings().ai_api_key
        ),
    }
