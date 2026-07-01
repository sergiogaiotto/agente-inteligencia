"""Default-deny de auth no /api/v1/* — prova positiva+negativa (SKILL.md §2).

Garante que:
  (-) endpoint /api/v1 não-allowlistado sem sessão → 401;
  (+) mesmo endpoint com cookie de sessão ASSINADO → passa;
  (+) allowlist (login/logout/me/check-setup, bootstrap, ingress federado) é
      alcançável sem sessão (para não quebrar o fluxo de autenticação/federação).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.api_auth import (
    install_api_auth_middleware,
    is_public_api_path,
    requires_auth,
)
from app.core.auth import sign_session


# ─────────────────────────────────────────────────────────────
# Unit — tabela de decisão do gate
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/v1/agents", True),
    ("POST", "/api/v1/agents/x/invoke", True),
    ("PUT", "/api/v1/settings", True),
    ("GET", "/api/v1/users", True),            # list_users deixa de ser público
    ("GET", "/api/v1/users/some-uuid", True),
    # allowlist (públicos por design):
    ("POST", "/api/v1/users/login", False),
    ("POST", "/api/v1/users/logout", False),
    ("GET", "/api/v1/users/check-setup", False),
    ("GET", "/api/v1/users/me", False),
    ("POST", "/api/v1/users", False),          # bootstrap (handler self-enforce)
    ("POST", "/api/v1/federation/invoke", False),
    # fora do data plane — não é gateado por este middleware:
    ("GET", "/api/health", False),
    ("GET", "/", False),
    ("GET", "/static/js/app.js", False),
    ("GET", "/.well-known/maestro-federation.json", False),
])
def test_gate_decision_table(method, path, expected):
    assert requires_auth(method, path) is expected


def test_public_is_method_specific():
    # GET /api/v1/users (list) é gateado; POST /api/v1/users (bootstrap) é público.
    assert is_public_api_path("POST", "/api/v1/users") is True
    assert is_public_api_path("GET", "/api/v1/users") is False


# ─────────────────────────────────────────────────────────────
# Integração — middleware num app real
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    async def _fake_find_by_id(uid):
        if uid == "u-ok":
            return {"id": "u-ok", "username": "bob", "status": "active", "role": "admin"}
        return None

    import app.core.database as db
    monkeypatch.setattr(db.users_repo, "find_by_id", _fake_find_by_id)

    app = FastAPI()
    install_api_auth_middleware(app)

    @app.get("/api/v1/agents")
    async def guarded():
        return {"ok": True}

    @app.get("/api/v1/users/me")
    async def me():
        return {"user": None}

    @app.post("/api/v1/users")
    async def bootstrap():
        return {"created": True}

    @app.post("/api/v1/federation/invoke")
    async def fed():
        return {"fed": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return TestClient(app)


def test_guarded_endpoint_anon_is_401(client):
    assert client.get("/api/v1/agents").status_code == 401


def test_guarded_endpoint_forged_cookie_is_401(client):
    client.cookies.set("user_id", "u-ok")  # UUID cru, sem assinatura
    assert client.get("/api/v1/agents").status_code == 401


def test_guarded_endpoint_signed_cookie_passes(client):
    client.cookies.set("user_id", sign_session("u-ok"))
    r = client.get("/api/v1/agents")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_allowlisted_me_reachable_anon(client):
    r = client.get("/api/v1/users/me")
    assert r.status_code == 200
    assert r.json() == {"user": None}


def test_allowlisted_bootstrap_reachable_anon(client):
    assert client.post("/api/v1/users").status_code == 200


def test_allowlisted_federation_invoke_reachable_anon(client):
    assert client.post("/api/v1/federation/invoke").status_code == 200


def test_non_dataplane_health_reachable_anon(client):
    assert client.get("/api/health").status_code == 200
