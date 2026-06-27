"""POST /api/v1/api-keys — criação de chave de integração.

Bug pego ao exercitar a estação de cURL (2026-06-27): criar chave COM expiração
(o modal default "90 dias" manda `expires_at: '...Z'`, aware) dava 500 —
`asyncpg.DataError: can't subtract offset-naive and offset-aware datetimes`,
porque a coluna `expires_at` é TIMESTAMP (naive). A rota agora normaliza o
datetime aware p/ UTC naive antes do insert. Sem expiração (None) sempre
funcionou — por isso passou despercebido (o teste E2E cria chave sem expiry).
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
from app.routes import api_keys as ak


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(ak.router)
    app.dependency_overrides[ak.require_user] = lambda: {"id": "u-test"}
    return TestClient(app, raise_server_exceptions=False)


def _stub_repos(monkeypatch):
    captured = {}
    async def fake_create(row):
        captured.update(row)
        return row
    monkeypatch.setattr(db.api_keys_repo, "create", fake_create)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))
    monkeypatch.setattr(ak, "generate_api_key", lambda: ("ag_live_plain", "ag_live_pref", "hash-xyz"))
    return captured


def test_aware_expiry_normalizado_para_naive_utc(monkeypatch):
    """A regressão: '...Z' (aware) → o que vai pro banco precisa ser NAIVE (UTC),
    senão asyncpg dá 500 na coluna TIMESTAMP."""
    captured = _stub_repos(monkeypatch)
    r = _client().post("/api/v1/api-keys", json={"name": "cURL · p", "expires_at": "2026-09-25T20:09:31.355Z"})
    assert r.status_code == 201, r.text
    ea = captured["expires_at"]
    assert isinstance(ea, datetime)
    assert ea.tzinfo is None, "expires_at precisa ser naive (coluna TIMESTAMP sem tz)"
    # Z = UTC → o instante naive bate com o UTC original
    assert (ea.year, ea.month, ea.day, ea.hour, ea.minute) == (2026, 9, 25, 20, 9)


def test_offset_explicito_convertido_para_utc_naive(monkeypatch):
    """ISO com offset não-UTC (-03:00) → converte pra UTC e tira o tz."""
    captured = _stub_repos(monkeypatch)
    r = _client().post("/api/v1/api-keys", json={"name": "x", "expires_at": "2026-09-25T17:00:00-03:00"})
    assert r.status_code == 201, r.text
    ea = captured["expires_at"]
    assert ea.tzinfo is None
    assert (ea.hour, ea.minute) == (20, 0)  # 17:00 -03:00 == 20:00 UTC


def test_sem_expiracao_fica_none(monkeypatch):
    captured = _stub_repos(monkeypatch)
    r = _client().post("/api/v1/api-keys", json={"name": "x"})
    assert r.status_code == 201, r.text
    assert captured["expires_at"] is None


def test_expiracao_invalida_400(monkeypatch):
    _stub_repos(monkeypatch)
    r = _client().post("/api/v1/api-keys", json={"name": "x", "expires_at": "não-é-data"})
    assert r.status_code == 400, r.text
