"""Testes dos endpoints REST do catálogo.

Estratégia: mini FastAPI app só com o router do catálogo + dependency
override para `require_user` + monkeypatch dos métodos do repo. Cobre
plumbing HTTP, validação Pydantic e regras de autorização, sem precisar
de Postgres real. Persistência real é coberta no smoke test manual.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.core.database import catalog_entries_repo, audit_repo
from app.routes.catalog import router as catalog_router


# ─── Fixtures ─────────────────────────────────────────────────────


def make_app(user: dict) -> FastAPI:
    """Mini app com require_user mockado e o catalog router montado."""
    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[require_user] = lambda: user
    return app


def make_client(user: dict) -> TestClient:
    return TestClient(make_app(user))


@pytest.fixture
def fake_storage(monkeypatch):
    """Substitui os métodos do catalog_entries_repo e audit_repo por mocks
    em memória. Retorna dict para inspeção pelos testes.
    """
    store: dict[str, dict] = {}
    audit_log: list[dict] = []

    async def fake_create(data):
        store[data["id"]] = dict(data)
        return data

    async def fake_find_by_id(id_):
        return dict(store[id_]) if id_ in store else None

    async def fake_update(id_, data):
        if id_ not in store:
            return None
        store[id_].update(data)
        return dict(store[id_])

    async def fake_delete(id_):
        return store.pop(id_, None) is not None

    async def fake_audit_create(data):
        audit_log.append(dict(data))
        return data

    monkeypatch.setattr(catalog_entries_repo, "create", fake_create)
    monkeypatch.setattr(catalog_entries_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(catalog_entries_repo, "update", fake_update)
    monkeypatch.setattr(catalog_entries_repo, "delete", fake_delete)
    monkeypatch.setattr(audit_repo, "create", fake_audit_create)

    return {"entries": store, "audit": audit_log}


def _payload(**over):
    base = {
        "name": "Smoke Agent",
        "kind": "agent",
        "artifact_type": "agent",
        "artifact_id": "agent-123",
        "version": "1.0.0",
    }
    base.update(over)
    return base


# ─── POST /entries ────────────────────────────────────────────────


class TestCreate:
    def test_create_minimal_valid(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload())
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Smoke Agent"
        assert body["status"] == "draft"
        assert body["owner_user_id"] == "u1"
        assert body["urn"].startswith("urn:maestro:default:agent:smoke-agent:")
        # Tags e adapter_config voltam como tipos nativos
        assert body["tags"] == []
        assert body["adapter_config"] == {}

    def test_create_audited(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        c.post("/api/v1/catalog/entries", json=_payload())
        assert len(fake_storage["audit"]) == 1
        evt = fake_storage["audit"][0]
        assert evt["entity_type"] == "catalog_entry"
        assert evt["action"] == "created"
        assert evt["actor"] == "u1"

    def test_create_agent_requires_artifact_link(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload(artifact_type=None, artifact_id=None))
        assert r.status_code == 422
        assert "vínculo" in r.json()["detail"].lower()

    def test_create_external_platform_no_artifact_required(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post(
            "/api/v1/catalog/entries",
            json={"name": "ChatGPT", "kind": "external_platform"},
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "external_platform"

    def test_create_rejects_invalid_kind(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload(kind="bogus"))
        assert r.status_code == 422

    def test_create_rejects_non_semver(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload(version="bad"))
        assert r.status_code == 422

    def test_create_duplicate_urn_returns_409(self, fake_storage, monkeypatch):
        c = make_client({"id": "u1", "role": "comum"})

        async def boom(data):
            raise Exception("duplicate key value violates unique constraint")

        monkeypatch.setattr(catalog_entries_repo, "create", boom)
        r = c.post("/api/v1/catalog/entries", json=_payload())
        assert r.status_code == 409
        assert "URN" in r.json()["detail"]


# ─── GET /entries/{id} ────────────────────────────────────────────


class TestGetOne:
    def test_get_own_draft(self, fake_storage):
        # Cria via POST e depois GET
        c = make_client({"id": "u1", "role": "comum"})
        cr = c.post("/api/v1/catalog/entries", json=_payload())
        eid = cr.json()["id"]

        r = c.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200
        assert r.json()["id"] == eid

    def test_other_user_blocked_on_draft(self, fake_storage):
        # u1 cria
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        # u2 tenta acessar — 404 (não vaza existência)
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 404

    def test_root_sees_other_users_draft(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_other_user_sees_published_company(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post(
            "/api/v1/catalog/entries",
            json=_payload(visibility="company"),
        ).json()["id"]
        # Simula publicação direta no storage (em PR 3 haverá endpoint)
        fake_storage["entries"][eid]["status"] = "published"

        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_404_when_not_found(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries/nonexistent")
        assert r.status_code == 404


# ─── PUT /entries/{id} ────────────────────────────────────────────


class TestUpdate:
    def test_owner_updates_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]

        r = c.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "novo texto"},
        )
        assert r.status_code == 200
        assert r.json()["description"] == "novo texto"

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "hack"},
        )
        assert r.status_code == 403

    def test_root_can_update_others(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "root edit"},
        )
        assert r.status_code == 200

    def test_cant_update_non_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        fake_storage["entries"][eid]["status"] = "published"
        r = c.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "after publish"},
        )
        assert r.status_code == 409

    def test_update_recalculates_urn_on_name_change(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        r = c.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"name": "Outro Nome"},
        )
        assert r.status_code == 200
        assert "outro-nome" in r.json()["urn"]

    def test_update_404_when_missing(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.put(
            "/api/v1/catalog/entries/missing",
            json={"description": "x"},
        )
        assert r.status_code == 404


# ─── DELETE /entries/{id} ─────────────────────────────────────────


class TestDelete:
    def test_owner_deletes_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        r = c.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200
        assert eid not in fake_storage["entries"]

    def test_cant_delete_published(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        fake_storage["entries"][eid]["status"] = "published"
        r = c.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 409

    def test_can_delete_archived(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        fake_storage["entries"][eid]["status"] = "archived"
        r = c.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 403

    def test_root_deletes_others(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_delete_404_when_missing(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.delete("/api/v1/catalog/entries/missing")
        assert r.status_code == 404


# ─── GET /entries (list) ──────────────────────────────────────────


class TestList:
    """List delega para list_visible_entries (SQL); aqui só testamos
    o plumbing HTTP via mock dessa função."""

    def test_list_returns_pagination_envelope(self, monkeypatch):
        async def fake_list(user, **kwargs):
            return [{"id": "e1", "name": "X"}], 42

        monkeypatch.setattr(
            "app.routes.catalog.list_visible_entries",
            fake_list,
        )
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 42
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert len(body["entries"]) == 1

    def test_list_passes_filters(self, monkeypatch):
        captured = {}

        async def fake_list(user, **kwargs):
            captured.update(kwargs)
            return [], 0

        monkeypatch.setattr(
            "app.routes.catalog.list_visible_entries",
            fake_list,
        )
        c = make_client({"id": "u1", "role": "comum"})
        c.get("/api/v1/catalog/entries?kind=agent&status=published&domain=fiscal&limit=10&offset=20")
        assert captured["kind"] == "agent"
        assert captured["status"] == "published"
        assert captured["domain"] == "fiscal"
        assert captured["limit"] == 10
        assert captured["offset"] == 20

    def test_list_rejects_limit_too_high(self, monkeypatch):
        async def fake_list(user, **kwargs):
            return [], 0

        monkeypatch.setattr(
            "app.routes.catalog.list_visible_entries",
            fake_list,
        )
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries?limit=500")
        assert r.status_code == 422
