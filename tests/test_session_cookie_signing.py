"""Cookie de sessão ASSINADO — prova positiva+negativa (SKILL.md §1, CWE-565/639).

Antes: o cookie `user_id` era o UUID cru do usuário; qualquer um forjava
`Cookie: user_id=<uuid>` e virava aquele usuário (inclusive root). Agora o
cookie carrega um token HMAC (itsdangerous) derivado de `secret_key`, verificado
server-side com expiração. Testes garantem que:
  (+) token legítimo autentica e o UUID é recuperado;
  (-) token adulterado / UUID cru forjado / token expirado são REJEITADOS (None/401).
"""
from __future__ import annotations

import types

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import auth
from app.core.auth import (
    read_session_uid_from_value,
    require_user,
    sign_session,
)


# ─────────────────────────────────────────────────────────────
# Nível unitário — sign/verify
# ─────────────────────────────────────────────────────────────

def test_roundtrip_recovers_uid():
    token = sign_session("user-123")
    assert token != "user-123"  # não é o valor cru
    assert read_session_uid_from_value(token) == "user-123"


def test_forged_raw_uuid_is_rejected():
    # O vetor de ataque original: mandar o UUID cru como cookie.
    assert read_session_uid_from_value("41e3a8b8-0000-1111-2222-aaaabbbbcccc") is None


def test_tampered_token_is_rejected():
    token = sign_session("user-123")
    # Adultera um caractere no MEIO do payload (não o último — em base64url o
    # caractere final tem bits redundantes e pode decodificar nos mesmos bytes).
    i = len(token) // 2
    tampered = token[:i] + ("X" if token[i] != "X" else "Y") + token[i + 1:]
    assert read_session_uid_from_value(tampered) is None


def test_empty_and_none_are_rejected():
    assert read_session_uid_from_value("") is None
    assert read_session_uid_from_value(None) is None


def test_token_from_other_secret_is_rejected(monkeypatch):
    # Token assinado com outra secret_key não pode ser validado com a nossa.
    from itsdangerous import URLSafeTimedSerializer
    foreign = URLSafeTimedSerializer("outra-chave-do-atacante", salt="maestro-session-v1")
    forged = foreign.dumps("root-uuid")
    assert read_session_uid_from_value(forged) is None


def test_expired_token_is_rejected(monkeypatch):
    token = sign_session("user-123")
    # Reaplica a MESMA secret_key porém com max_age negativo → expira na hora.
    real = auth.get_settings()
    fake = types.SimpleNamespace(
        secret_key=real.secret_key, session_max_age_seconds=-1
    )
    monkeypatch.setattr(auth, "get_settings", lambda: fake)
    assert read_session_uid_from_value(token) is None


# ─────────────────────────────────────────────────────────────
# Nível de integração — require_user via cookie
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    async def _fake_find_by_id(uid):
        if uid == "user-123":
            return {"id": "user-123", "username": "alice", "status": "active",
                    "role": "admin", "password_hash": "x"}
        return None

    import app.core.database as db
    monkeypatch.setattr(db.users_repo, "find_by_id", _fake_find_by_id)

    app = FastAPI()

    @app.get("/protected")
    async def protected(user: dict = Depends(require_user)):
        return {"id": user["id"]}

    return TestClient(app)


def test_valid_signed_cookie_authenticates(client):
    client.cookies.set("user_id", sign_session("user-123"))
    r = client.get("/protected")
    assert r.status_code == 200
    assert r.json()["id"] == "user-123"
    # password_hash nunca vaza
    assert "password_hash" not in r.json()


def test_forged_raw_cookie_is_401(client):
    client.cookies.set("user_id", "user-123")  # UUID cru, sem assinatura
    r = client.get("/protected")
    assert r.status_code == 401


def test_no_cookie_is_401(client):
    r = client.get("/protected")
    assert r.status_code == 401
