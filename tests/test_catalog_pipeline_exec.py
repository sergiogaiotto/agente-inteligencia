"""PR5 — pipeline como GRAFO no catálogo: snapshot do subgrafo + execução.

Cobre: _build_subgraph (membros + arestas intra-pipeline + raiz); execute_pipeline_entry
(grava steps + finaliza + bump de trust; sandbox não bumpa; crash → failed); e as
pré-condições da rota /execute-pipeline (kind/status).
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.executor as ex
import app.agents.engine as engine
from app.catalog.pipeline_defs import _build_subgraph
from app.routes import catalog as catalog_routes
from app.core.auth import require_user


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ───────────── _build_subgraph ─────────────
def test_build_subgraph_members_edges_root(monkeypatch):
    monkeypatch.setattr(db.pipeline_membership, "agents_of", _async(["a", "b", "c"]))
    monkeypatch.setattr(db.mesh_repo, "find_all", _async([
        {"id": "e1", "source_agent_id": "a", "target_agent_id": "b", "connection_type": "sequential", "config": "{}"},
        {"id": "e2", "source_agent_id": "b", "target_agent_id": "c", "connection_type": "conditional", "config": "{}"},
        {"id": "e3", "source_agent_id": "a", "target_agent_id": "z", "connection_type": "sequential", "config": "{}"},  # z ∉ membros → exclui
    ]))
    agents = {
        "a": {"id": "a", "name": "A", "kind": "router", "status": "active", "version": "1.0.0"},
        "b": {"id": "b", "name": "B", "kind": "subagent", "status": "active", "version": "1.0.0"},
        "c": {"id": "c", "name": "C", "kind": "subagent", "status": "active", "version": "1.0.0"},
    }
    async def _find(aid):
        return agents.get(aid)
    monkeypatch.setattr(db.agents_repo, "find_by_id", _find)

    sub = asyncio.run(_build_subgraph("p1"))
    assert sub["root_agent_id"] == "a"                      # a é source-never-target
    assert {e["id"] for e in sub["edges"]} == {"e1", "e2"}  # e3 excluída (z não é membro)
    assert len(sub["nodes"]) == 3
    assert {n["id"] for n in sub["nodes"]} == {"a", "b", "c"}
    # config parseado p/ OBJETO (não string) — contrato de edge p/ o snapshot/UI
    assert all(isinstance(e["config"], dict) for e in sub["edges"])


def test_build_subgraph_no_edges_picks_first_member(monkeypatch):
    monkeypatch.setattr(db.pipeline_membership, "agents_of", _async(["solo"]))
    monkeypatch.setattr(db.mesh_repo, "find_all", _async([]))
    monkeypatch.setattr(db.agents_repo, "find_by_id", _async({"id": "solo", "name": "S", "kind": "aobd", "status": "active", "version": "1.0.0"}))
    sub = asyncio.run(_build_subgraph("p1"))
    assert sub["root_agent_id"] == "solo"
    assert sub["edges"] == []


# ───────────── execute_pipeline_entry (gravação) ─────────────
def _wire_recording(monkeypatch):
    calls = {"steps": [], "finalize": None, "cost": None}
    async def fake_append(eid, sr):
        calls["steps"].append(sr)
    async def fake_finalize(eid, **k):
        calls["finalize"] = k
    async def fake_cost(entry_id, **k):
        calls["cost"] = {"entry_id": entry_id, **k}
    monkeypatch.setattr(ex, "append_step_result", fake_append)
    monkeypatch.setattr(ex, "finalize_execution", fake_finalize)
    monkeypatch.setattr(ex, "record_invocation_cost", fake_cost)
    return calls


def test_execute_pipeline_entry_completed_records_and_bumps_trust(monkeypatch):
    calls = _wire_recording(monkeypatch)
    monkeypatch.setattr(engine, "execute_pipeline", _async({
        "pipeline_steps": [{"agent_id": "a", "agent_name": "A", "status": "completed", "output": "x", "final_state": "Recommend"}],
        "completed_agents": 1, "duration_ms": 123, "interaction_id": "int1", "status": "completed",
    }))
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u1"}, user_input="oi",
    ))
    assert calls["finalize"]["status"] == "completed"
    assert len(calls["steps"]) == 1
    assert calls["cost"]["entry_id"] == "pe"   # trust bump na ENTRY do pipeline

def test_execute_pipeline_entry_sandbox_no_cost(monkeypatch):
    calls = _wire_recording(monkeypatch)
    monkeypatch.setattr(engine, "execute_pipeline", _async({
        "pipeline_steps": [], "completed_agents": 0, "duration_ms": 5, "interaction_id": None, "status": "completed",
    }))
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u1"}, user_input="oi", is_sandbox=True,
    ))
    assert calls["finalize"] is not None
    assert calls["cost"] is None   # sandbox NÃO grava custo/trust

def test_execute_pipeline_entry_engine_crash_failed(monkeypatch):
    calls = _wire_recording(monkeypatch)
    async def _boom(**k):
        raise RuntimeError("engine down")
    monkeypatch.setattr(engine, "execute_pipeline", _boom)
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u1"}, user_input="oi",
    ))
    assert calls["finalize"]["status"] == "failed"
    assert "engine down" in (calls["finalize"]["error_message"] or "")

def test_execute_pipeline_entry_recording_failure_finalizes_failed(monkeypatch):
    # Se a GRAVAÇÃO falhar (ex.: DB), a row NÃO pode ficar 'running' forever:
    # sela como 'failed' (espelha o catch-all do execute_recipe).
    calls = _wire_recording(monkeypatch)
    monkeypatch.setattr(engine, "execute_pipeline", _async({
        "pipeline_steps": [{"agent_id": "a", "status": "completed", "output": "x"}],
        "completed_agents": 1, "duration_ms": 10, "interaction_id": "int1", "status": "completed",
    }))
    async def boom_append(eid, sr):
        raise RuntimeError("pool down")
    monkeypatch.setattr(ex, "append_step_result", boom_append)
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u1"}, user_input="oi",
    ))
    assert calls["finalize"]["status"] == "failed"
    assert calls["cost"] is None   # não bumpa trust quando a gravação falhou


def test_execute_pipeline_entry_partial_on_step_error(monkeypatch):
    calls = _wire_recording(monkeypatch)
    monkeypatch.setattr(engine, "execute_pipeline", _async({
        "pipeline_steps": [
            {"agent_id": "a", "status": "completed", "output": "ok"},
            {"agent_id": "b", "status": "error", "error": "boom"},
        ],
        "completed_agents": 1, "duration_ms": 50, "interaction_id": "int1", "status": "completed",
    }))
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u1"}, user_input="oi",
    ))
    assert calls["finalize"]["status"] == "partial"   # houve erro mas algo executou


# ───────────── rota /execute-pipeline (pré-condições) ─────────────
def _make_client(user):
    app = FastAPI()
    app.include_router(catalog_routes.router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


class TestExecutePipelineRoute:
    def _entry(self, kind, status):
        return {"id": "e1", "kind": kind, "status": status, "owner_user_id": "u1"}

    def test_422_when_not_pipeline_kind(self, monkeypatch):
        monkeypatch.setattr(db.catalog_entries_repo, "find_by_id", _async({"id": "e1"}))
        monkeypatch.setattr(catalog_routes, "db_row_to_entry_dict", lambda r: self._entry("recipe", "published"))
        monkeypatch.setattr(catalog_routes, "can_user_see", lambda u, e: True)
        r = _make_client({"id": "u1", "role": "comum"}).post("/api/v1/catalog/entries/e1/execute-pipeline", json={"input": "oi"})
        assert r.status_code == 422, r.text

    def test_409_when_not_published(self, monkeypatch):
        monkeypatch.setattr(db.catalog_entries_repo, "find_by_id", _async({"id": "e1"}))
        monkeypatch.setattr(catalog_routes, "db_row_to_entry_dict", lambda r: self._entry("pipeline", "draft"))
        monkeypatch.setattr(catalog_routes, "can_user_see", lambda u, e: True)
        r = _make_client({"id": "u1", "role": "comum"}).post("/api/v1/catalog/entries/e1/execute-pipeline", json={"input": "oi"})
        assert r.status_code == 409, r.text

    def test_202_when_published(self, monkeypatch):
        monkeypatch.setattr(db.catalog_entries_repo, "find_by_id", _async({"id": "e1"}))
        monkeypatch.setattr(catalog_routes, "db_row_to_entry_dict", lambda r: self._entry("pipeline", "published"))
        monkeypatch.setattr(catalog_routes, "can_user_see", lambda u, e: True)
        monkeypatch.setattr(catalog_routes, "create_execution", _async({"id": "exec1", "started_at": None}))
        monkeypatch.setattr(catalog_routes, "_audit", _async(None))
        import app.catalog.pipeline_defs as pdefs
        monkeypatch.setattr(pdefs, "resolve_pipeline_exec", _async(("root1", {"root1"})))
        monkeypatch.setattr(ex, "execute_pipeline_entry", _async(None))
        r = _make_client({"id": "u1", "role": "comum"}).post("/api/v1/catalog/entries/e1/execute-pipeline", json={"input": "oi"})
        assert r.status_code == 202, r.text
        assert r.json()["execution_id"] == "exec1"
        assert r.json()["status"] == "running"
