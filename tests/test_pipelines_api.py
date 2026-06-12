"""Testes dos endpoints REST do Estúdio de Pipelines (PR1) + migração.

Estratégia (igual a tests/test_catalog_api.py): mini FastAPI app só com o router
de pipelines + monkeypatch dos repos/membership para dicts in-memory. Cobre
plumbing HTTP, validação, lifecycle governado (422) e exclusividade de
membership — sem Postgres real. As rotas de pipelines não usam auth (igual mesh).
"""

from __future__ import annotations

import json
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import (
    pipelines_repo,
    pipeline_membership,
    agents_repo,
    audit_repo,
    migrate_mesh_groups_to_pipelines,
    settings_store,
)
from app.routes.pipelines import router as pipelines_router


# ─── Fixtures ─────────────────────────────────────────────────────


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(pipelines_router)
    return TestClient(app)


def _bind_pipelines_repo(monkeypatch, store: dict):
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

    async def fake_find_all(limit=100, offset=0, **filters):
        rows = list(store.values())
        for k, v in filters.items():
            rows = [r for r in rows if r.get(k) == v]
        return rows[offset:offset + limit]

    monkeypatch.setattr(pipelines_repo, "create", fake_create)
    monkeypatch.setattr(pipelines_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(pipelines_repo, "update", fake_update)
    monkeypatch.setattr(pipelines_repo, "delete", fake_delete)
    monkeypatch.setattr(pipelines_repo, "find_all", fake_find_all)


def _bind_membership(monkeypatch, mem: dict):
    """mem = {agent_id: pipeline_id} — PK em agent_id garante exclusividade."""

    async def m_set(agent_id, pipeline_id):
        mem[agent_id] = pipeline_id

    async def m_remove(agent_id):
        return mem.pop(agent_id, None) is not None

    async def m_remove_from(pipeline_id, agent_id):
        if mem.get(agent_id) == pipeline_id:
            del mem[agent_id]
            return True
        return False

    async def m_agents_of(pipeline_id):
        return [a for a, p in mem.items() if p == pipeline_id]

    async def m_pipeline_of(agent_id):
        return mem.get(agent_id)

    async def m_all():
        return [{"agent_id": a, "pipeline_id": p} for a, p in mem.items()]

    monkeypatch.setattr(pipeline_membership, "set", m_set)
    monkeypatch.setattr(pipeline_membership, "remove", m_remove)
    monkeypatch.setattr(pipeline_membership, "remove_from", m_remove_from)
    monkeypatch.setattr(pipeline_membership, "agents_of", m_agents_of)
    monkeypatch.setattr(pipeline_membership, "pipeline_of", m_pipeline_of)
    monkeypatch.setattr(pipeline_membership, "all", m_all)


@pytest.fixture
def storage(monkeypatch):
    pipelines: dict = {}
    mem: dict = {}
    agents = {"a1": {"id": "a1", "name": "Cobrança", "domain": "financeiro"},
              "a2": {"id": "a2", "name": "Triagem", "domain": "financeiro"},
              "a3": {"id": "a3", "name": "RH", "domain": "rh"}}

    _bind_pipelines_repo(monkeypatch, pipelines)
    _bind_membership(monkeypatch, mem)

    async def fake_agent_find(aid):
        return dict(agents[aid]) if aid in agents else None

    async def fake_audit(data):
        return data

    monkeypatch.setattr(agents_repo, "find_by_id", fake_agent_find)
    monkeypatch.setattr(audit_repo, "create", fake_audit)
    return {"pipelines": pipelines, "membership": mem, "agents": agents}


def _create(client, name="Pipeline X") -> str:
    r = client.post("/api/v1/pipelines", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ─── CRUD ─────────────────────────────────────────────────────────


class TestCRUD:
    def test_create_minimal(self, storage):
        c = make_client()
        r = c.post("/api/v1/pipelines", json={"name": "Cobrança PIX"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "Cobrança PIX"
        assert body["status"] == "rascunho"
        assert body["agent_count"] == 0
        assert body["agent_ids"] == []
        assert set(body["next_states"]) == {"publicado", "aposentado"}

    def test_create_rejects_blank_name(self, storage):
        c = make_client()
        # Pydantic min_length=1 → 422 antes do handler
        assert c.post("/api/v1/pipelines", json={"name": ""}).status_code == 422
        # só espaços → handler retorna 422
        assert c.post("/api/v1/pipelines", json={"name": "   "}).status_code == 422

    def test_get_and_404(self, storage):
        c = make_client()
        pid = _create(c)
        assert c.get(f"/api/v1/pipelines/{pid}").status_code == 200
        assert c.get("/api/v1/pipelines/nope").status_code == 404

    def test_list_includes_counts(self, storage):
        c = make_client()
        pid = _create(c, "P1")
        c.post(f"/api/v1/pipelines/{pid}/agents", json={"agent_id": "a1"})
        r = c.get("/api/v1/pipelines")
        assert r.status_code == 200
        items = r.json()["pipelines"]
        assert len(items) == 1
        assert items[0]["agent_count"] == 1
        assert items[0]["agent_ids"] == ["a1"]

    def test_list_filter_by_status(self, storage):
        c = make_client()
        p1 = _create(c, "P1")
        _create(c, "P2")
        c.post(f"/api/v1/pipelines/{p1}/status", json={"status": "publicado"})
        r = c.get("/api/v1/pipelines", params={"status": "publicado"})
        ids = [p["id"] for p in r.json()["pipelines"]]
        assert ids == [p1]

    def test_update_metadata_not_status(self, storage):
        c = make_client()
        pid = _create(c)
        r = c.put(f"/api/v1/pipelines/{pid}", json={"name": "Renomeado", "domain": "financeiro"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Renomeado"
        assert body["domain"] == "financeiro"
        assert body["status"] == "rascunho"  # inalterado

    def test_delete_then_404(self, storage):
        c = make_client()
        pid = _create(c)
        assert c.delete(f"/api/v1/pipelines/{pid}").status_code == 200
        assert c.get(f"/api/v1/pipelines/{pid}").status_code == 404
        assert c.delete(f"/api/v1/pipelines/{pid}").status_code == 404


# ─── Lifecycle governado (/status) ────────────────────────────────


class TestStatusGoverned:
    def test_happy_path(self, storage):
        c = make_client()
        pid = _create(c)
        assert c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "publicado"}).json()["status"] == "publicado"
        assert c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "aposentado"}).json()["status"] == "aposentado"
        assert c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "publicado"}).json()["status"] == "publicado"

    def test_invalid_transition_422(self, storage):
        c = make_client()
        pid = _create(c)
        c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "publicado"})
        c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "aposentado"})
        # aposentado → rascunho NÃO é permitido
        r = c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "rascunho"})
        assert r.status_code == 422, r.text
        assert "não pode transitar" in r.json()["detail"]

    def test_invalid_status_value_422(self, storage):
        c = make_client()
        pid = _create(c)
        r = c.post(f"/api/v1/pipelines/{pid}/status", json={"status": "bogus"})
        assert r.status_code == 422
        assert "inválido" in r.json()["detail"]

    def test_status_on_missing_pipeline_404(self, storage):
        c = make_client()
        assert c.post("/api/v1/pipelines/nope/status", json={"status": "publicado"}).status_code == 404


# ─── Membership exclusiva (/agents) ───────────────────────────────


class TestMembership:
    def test_add_and_remove(self, storage):
        c = make_client()
        pid = _create(c)
        r = c.post(f"/api/v1/pipelines/{pid}/agents", json={"agent_id": "a1"})
        assert r.status_code == 200, r.text
        assert r.json()["agent_ids"] == ["a1"]
        r2 = c.delete(f"/api/v1/pipelines/{pid}/agents/a1")
        assert r2.status_code == 200
        assert r2.json()["agent_ids"] == []

    def test_exclusivity_moves_agent(self, storage):
        c = make_client()
        p1 = _create(c, "P1")
        p2 = _create(c, "P2")
        c.post(f"/api/v1/pipelines/{p1}/agents", json={"agent_id": "a1"})
        # incluir o mesmo agente em P2 deve MOVÊ-LO (sai de P1)
        r = c.post(f"/api/v1/pipelines/{p2}/agents", json={"agent_id": "a1"})
        assert r.status_code == 200
        assert r.json()["moved_from"] == p1
        assert c.get(f"/api/v1/pipelines/{p1}").json()["agent_ids"] == []
        assert c.get(f"/api/v1/pipelines/{p2}").json()["agent_ids"] == ["a1"]
        # garantia de exclusividade no store
        assert storage["membership"] == {"a1": p2}

    def test_add_unknown_agent_404(self, storage):
        c = make_client()
        pid = _create(c)
        assert c.post(f"/api/v1/pipelines/{pid}/agents", json={"agent_id": "ghost"}).status_code == 404

    def test_add_to_missing_pipeline_404(self, storage):
        c = make_client()
        assert c.post("/api/v1/pipelines/nope/agents", json={"agent_id": "a1"}).status_code == 404

    def test_remove_agent_not_member_404(self, storage):
        c = make_client()
        pid = _create(c)
        assert c.delete(f"/api/v1/pipelines/{pid}/agents/a1").status_code == 404


# ─── Migração mesh_groups → pipelines ─────────────────────────────


class TestMigration:
    def _bind(self, monkeypatch, settings, pipelines, mem):
        async def s_get(key, default=""):
            return settings.get(key, default)

        async def s_set(key, value):
            settings[key] = value

        async def p_create(data):
            pipelines[data["id"]] = dict(data)
            return data

        async def p_find(id_):
            return dict(pipelines[id_]) if id_ in pipelines else None

        async def p_update(id_, data):
            if id_ in pipelines:
                pipelines[id_].update(data)
                return dict(pipelines[id_])
            return None

        async def m_set(aid, pid):
            mem[aid] = pid

        async def m_pipeline_of(aid):
            return mem.get(aid)

        monkeypatch.setattr(settings_store, "get", s_get)
        monkeypatch.setattr(settings_store, "set", s_set)
        monkeypatch.setattr(pipelines_repo, "create", p_create)
        monkeypatch.setattr(pipelines_repo, "find_by_id", p_find)
        monkeypatch.setattr(pipelines_repo, "update", p_update)
        monkeypatch.setattr(pipeline_membership, "set", m_set)
        monkeypatch.setattr(pipeline_membership, "pipeline_of", m_pipeline_of)

    def test_groups_become_pipelines_with_chain_name(self, monkeypatch):
        settings = {
            "mesh_groups": json.dumps([
                {"id": "g1", "name": "Grupo Cobrança", "color": "red", "agent_ids": ["a1", "a2"]},
                {"id": "g2", "name": "Grupo RH", "color": "blue", "agent_ids": ["a3"]},
            ]),
            "mesh_chain_names": json.dumps({"a1": "Pipeline Cobrança PIX"}),
        }
        pipelines: dict = {}
        mem: dict = {}
        self._bind(monkeypatch, settings, pipelines, mem)

        res = asyncio.run(migrate_mesh_groups_to_pipelines())
        assert res["skipped"] is False
        assert res["migrated"] == 2
        assert res["renamed"] == 1
        # g1: rascunho, cor preservada, nome herdado do chain (a1 é membro)
        assert pipelines["g1"]["status"] == "rascunho"
        assert pipelines["g1"]["color"] == "red"
        assert pipelines["g1"]["name"] == "Pipeline Cobrança PIX"
        assert pipelines["g2"]["name"] == "Grupo RH"
        # membership exclusiva e completa
        assert mem == {"a1": "g1", "a2": "g1", "a3": "g2"}
        assert settings["pipelines_migrated_from_groups"] == "1"

    def test_idempotent_second_run_is_noop(self, monkeypatch):
        settings = {"mesh_groups": json.dumps([
            {"id": "g1", "name": "G1", "color": "teal", "agent_ids": ["a1"]},
        ])}
        pipelines: dict = {}
        mem: dict = {}
        self._bind(monkeypatch, settings, pipelines, mem)

        first = asyncio.run(migrate_mesh_groups_to_pipelines())
        assert first["migrated"] == 1
        second = asyncio.run(migrate_mesh_groups_to_pipelines())
        assert second["skipped"] is True
        assert second["migrated"] == 0
        assert len(pipelines) == 1  # não duplicou

    def test_empty_groups_sets_flag(self, monkeypatch):
        settings: dict = {}
        pipelines: dict = {}
        mem: dict = {}
        self._bind(monkeypatch, settings, pipelines, mem)
        res = asyncio.run(migrate_mesh_groups_to_pipelines())
        assert res["migrated"] == 0
        assert res["skipped"] is False
        assert settings["pipelines_migrated_from_groups"] == "1"
