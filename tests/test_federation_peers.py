"""PR8b2 — registro de peers de federação.

Cobre o módulo `federation_peers` (geração/cifra/rotação/lookup, com crypto REAL —
round-trip via fallback dev) e os endpoints root-only de CRUD. Repo em memória
(fake) — sem Postgres. Async via asyncio.run; rotas via TestClient + override.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.catalog.federation_peers as peers
from app.core.auth import require_user
from app.routes import federation as fed_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


class _FakeRepo:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def create(self, row):
        for r in self.rows.values():  # simula UNIQUE(workspace)
            if r.get("workspace") == row.get("workspace"):
                raise Exception("duplicate key value violates unique constraint")
        r = dict(row)
        r.setdefault("created_at", None)
        r.setdefault("rotated_at", None)
        self.rows[r["id"]] = r
        return r

    async def find_all(self, limit=500, **filters):
        out = [r for r in self.rows.values() if all(r.get(k) == v for k, v in filters.items())]
        return out[:limit]

    async def find_by_id(self, _id):
        return self.rows.get(_id)

    async def update(self, _id, changes):
        if _id in self.rows:
            self.rows[_id].update(changes)
        return self.rows.get(_id)


@pytest.fixture
def repo(monkeypatch):
    r = _FakeRepo()
    monkeypatch.setattr(peers, "federation_peers_repo", r)
    return r


# ── módulo ──
class TestPeerModule:
    def test_generate_secret_distinct_nonempty(self):
        a, b = peers.generate_peer_secret(), peers.generate_peer_secret()
        assert a and b and a != b

    def test_validate_base_url(self):
        assert peers.validate_base_url(None) is None
        assert peers.validate_base_url("") is None
        assert peers.validate_base_url("https://peer.example/") == "https://peer.example"
        assert peers.validate_base_url("http://localhost:7000") == "http://localhost:7000"
        with pytest.raises(ValueError):
            peers.validate_base_url("ftp://peer.example")
        with pytest.raises(ValueError):
            peers.validate_base_url("peer.example")

    def test_register_returns_plaintext_and_encrypts(self, repo):
        row, secret = asyncio.run(peers.register_peer("acme", "https://acme.example"))
        assert secret and row["workspace"] == "acme"
        assert row["base_url"] == "https://acme.example"
        # segredo guardado CIFRADO (enc::), não plaintext
        assert row["shared_secret"].startswith("enc::")
        assert secret not in row["shared_secret"]
        # round-trip: peer_secrets decifra de volta ao plaintext
        assert peers.peer_secrets(row) == [secret]

    def test_register_invalid_workspace_raises(self, repo):
        with pytest.raises(ValueError):
            asyncio.run(peers.register_peer("Acme Corp!"))

    def test_register_self_workspace_rejected(self, repo):
        # local_workspace() cai p/ 'default' sem pool → registrar 'default' = self
        with pytest.raises(ValueError, match="própria instância"):
            asyncio.run(peers.register_peer("default"))

    def test_register_duplicate_workspace_raises(self, repo):
        asyncio.run(peers.register_peer("acme"))
        with pytest.raises(Exception) as ei:
            asyncio.run(peers.register_peer("acme"))
        assert "duplicate" in str(ei.value).lower()

    def test_peer_secrets_filters_empty(self):
        assert peers.peer_secrets({"shared_secret": None, "secret_prev": None}) == []

    def test_get_active_peer_by_workspace(self, repo):
        asyncio.run(peers.register_peer("acme"))
        found = asyncio.run(peers.get_active_peer_by_workspace("acme"))
        assert found and found["workspace"] == "acme"
        assert asyncio.run(peers.get_active_peer_by_workspace("ghost")) is None
        assert asyncio.run(peers.get_active_peer_by_workspace("")) is None

    def test_revoked_peer_not_active(self, repo):
        row, _ = asyncio.run(peers.register_peer("acme"))
        revoked = asyncio.run(peers.revoke_peer(row["id"]))
        assert revoked is not None and revoked["workspace"] == "acme"  # row p/ audit
        assert asyncio.run(peers.get_active_peer_by_workspace("acme")) is None

    def test_revoke_missing_returns_none(self, repo):
        assert asyncio.run(peers.revoke_peer("nope")) is None

    def test_rotate_overlaps_old_and_new(self, repo):
        row, old = asyncio.run(peers.register_peer("acme"))
        updated, new = asyncio.run(peers.rotate_peer_secret(row["id"]))
        assert new != old
        # janela de sobreposição: AMBOS verificáveis
        secrets_now = peers.peer_secrets(updated)
        assert new in secrets_now and old in secrets_now
        assert updated.get("rotated_at") is not None

    def test_rotate_missing_returns_none(self, repo):
        assert asyncio.run(peers.rotate_peer_secret("nope")) is None


# ── rotas (root-only) ──
class TestPeerRoutes:
    def _client(self, user):
        app = FastAPI()
        app.include_router(fed_routes.peers_router)
        app.dependency_overrides[require_user] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    def _root(self):
        return {"id": "root-1", "role": "root"}

    def _member(self):
        return {"id": "u-1", "role": "member"}

    def _patch_audit(self, monkeypatch):
        monkeypatch.setattr(fed_routes.audit_repo, "create", _async(None))

    def test_non_root_forbidden(self, monkeypatch):
        self._patch_audit(monkeypatch)
        c = self._client(self._member())
        assert c.post("/api/v1/federation/peers", json={"workspace": "acme"}).status_code == 403
        assert c.get("/api/v1/federation/peers").status_code == 403
        assert c.post("/api/v1/federation/peers/x/rotate").status_code == 403
        assert c.delete("/api/v1/federation/peers/x").status_code == 403

    def test_create_returns_secret_once(self, monkeypatch):
        self._patch_audit(monkeypatch)
        row = {"id": "p1", "workspace": "acme", "base_url": None, "status": "active",
               "secret_prev": None}
        monkeypatch.setattr(fed_routes.peers, "register_peer", _async((row, "SECRET123")))
        r = self._client(self._root()).post("/api/v1/federation/peers", json={"workspace": "acme"})
        assert r.status_code == 201
        body = r.json()
        assert body["shared_secret"] == "SECRET123"
        assert body["workspace"] == "acme"
        assert "secret_prev" not in body  # nunca expõe o cifrado

    def test_create_duplicate_409(self, monkeypatch):
        self._patch_audit(monkeypatch)
        async def boom(*a, **k):
            raise Exception("duplicate key value violates unique constraint")
        monkeypatch.setattr(fed_routes.peers, "register_peer", boom)
        r = self._client(self._root()).post("/api/v1/federation/peers", json={"workspace": "acme"})
        assert r.status_code == 409

    def test_create_invalid_workspace_422(self, monkeypatch):
        self._patch_audit(monkeypatch)
        async def bad(*a, **k):
            raise ValueError("workspace inválido")
        monkeypatch.setattr(fed_routes.peers, "register_peer", bad)
        r = self._client(self._root()).post("/api/v1/federation/peers", json={"workspace": "X Y"})
        assert r.status_code == 422

    def test_list_omits_secret(self, monkeypatch):
        self._patch_audit(monkeypatch)
        rows = [{"id": "p1", "workspace": "acme", "base_url": "https://a", "status": "active",
                 "secret_prev": "enc::xxx", "shared_secret": "enc::yyy",
                 "created_at": None, "rotated_at": None}]
        monkeypatch.setattr(fed_routes.peers, "list_peers", _async(rows))
        r = self._client(self._root()).get("/api/v1/federation/peers")
        assert r.status_code == 200
        peer = r.json()["peers"][0]
        assert peer["workspace"] == "acme"
        assert peer["has_prev_secret"] is True
        assert "shared_secret" not in peer and "secret_prev" not in peer

    def test_rotate_found_and_missing(self, monkeypatch):
        self._patch_audit(monkeypatch)
        row = {"id": "p1", "workspace": "acme", "base_url": None, "status": "active",
               "secret_prev": "enc::old", "created_at": None, "rotated_at": None}
        monkeypatch.setattr(fed_routes.peers, "rotate_peer_secret", _async((row, "NEWSECRET")))
        r = self._client(self._root()).post("/api/v1/federation/peers/p1/rotate")
        assert r.status_code == 200 and r.json()["shared_secret"] == "NEWSECRET"
        monkeypatch.setattr(fed_routes.peers, "rotate_peer_secret", _async(None))
        assert self._client(self._root()).post("/api/v1/federation/peers/x/rotate").status_code == 404

    def test_delete_found_and_missing(self, monkeypatch):
        self._patch_audit(monkeypatch)
        monkeypatch.setattr(fed_routes.peers, "revoke_peer", _async({"workspace": "acme"}))
        assert self._client(self._root()).delete("/api/v1/federation/peers/p1").status_code == 200
        monkeypatch.setattr(fed_routes.peers, "revoke_peer", _async(None))
        assert self._client(self._root()).delete("/api/v1/federation/peers/x").status_code == 404
