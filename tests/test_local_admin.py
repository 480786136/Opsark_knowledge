from argon2 import PasswordHasher
from sqlalchemy import select
from knowledge.models import KnowledgeAdmin, Audit
from knowledge.admin import attempts
from test_workflow import env as source_env

env = source_env


def test_local_administration_without_platform_or_service_token(env, monkeypatch):
    from knowledge.config import settings

    client, factory, service = env
    monkeypatch.setattr(settings(), "knowledge_service_token", "")
    attempts.clear()
    with factory() as db:
        db.add(
            KnowledgeAdmin(
                username="knowledge",
                password_hash=PasswordHasher().hash("synthetic-password-only"),
            )
        )
        db.commit()
    path = "/api/admin/v1/knowledge/knowledge-bases"
    assert client.get(path).status_code == 401
    login = client.post(
        "/api/admin/v1/session",
        json={"username": "knowledge", "password": "synthetic-password-only"},
    )
    assert login.status_code == 200, login.text
    assert "opsark_knowledge_session=" in login.headers["set-cookie"]
    headers = {"X-CSRF-Token": login.json()["csrf"], "X-Opsark-Actor": "forged"}
    assert client.post(path, json={"name": "denied"}).status_code == 403
    assert (
        client.post(
            path,
            headers={**headers, "Origin": "https://evil.example"},
            json={"name": "denied"},
        ).status_code
        == 403
    )
    created = client.post(path, headers=headers, json={"name": "independent"})
    assert created.status_code == 201, created.text
    assert client.get(path).json()[0]["name"] == "independent"
    with factory() as db:
        assert db.scalar(select(Audit)).actor != "forged"
    assert client.get("/internal/v1/knowledge-bases").status_code == 401
    assert client.delete("/api/admin/v1/session", headers=headers).status_code == 200
    assert client.get(path).status_code == 401


def test_platform_cookie_cannot_authenticate_knowledge(env):
    client, _, _ = env
    client.cookies.set("opsark_session", "fake-platform-session")
    assert client.get("/api/admin/v1/session").status_code == 401
