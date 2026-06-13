"""PR-A2 (Trilha A) — invoke por pipeline-entidade (contrato API-first selado).

POST /api/v1/pipelines/{id}/invoke resolve raiz+membros e executa via
execute_pipeline DELIMITADO ao subgrafo. aposentado→409; sem message→400;
sem raiz→422; happy→executa selado (allowed_agent_ids=membros).
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
from app.routes import pipelines as pl_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl_routes.router)
    return TestClient(app, raise_server_exceptions=False)


def _pipe(status="publicado"):
    return {"id": "p1", "name": "Folha", "status": status}


class TestInvokePipeline:
    def test_409_when_aposentado(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe("aposentado")))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 409, r.text

    def test_400_when_no_message(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={})
        assert r.status_code == 400, r.text

    def test_404_when_pipeline_missing(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(None))
        r = _client().post("/api/v1/pipelines/ghost/invoke", json={"message": "oi"})
        assert r.status_code == 404, r.text

    def test_422_when_no_root(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": None, "nodes": [], "edges": []}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 422, r.text

    def test_happy_executes_sealed(self, monkeypatch):
        captured = {}
        async def fake_exec(**k):
            captured.update(k)
            return {"status": "completed", "output": "resposta", "final_state": "Recommend",
                    "interaction_id": "int1", "total_agents": 2, "completed_agents": 2,
                    "pipeline_steps": [{"agent_id": "r"}, {"agent_id": "a"}], "duration_ms": 42}
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({
            "root_agent_id": "r", "nodes": [{"id": "r"}, {"id": "a"}], "edges": [],
        }))
        monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi", "session_id": "s1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pipeline_id"] == "p1"
        assert body["output"] == "resposta"
        assert body["completed_agents"] == 2
        # executou SELADO: allowed_agent_ids = membros do subgrafo; entry = raiz
        assert captured["entry_agent_id"] == "r"
        assert captured["allowed_agent_ids"] == {"r", "a"}
        assert captured["session_id"] == "s1"

    def test_aposentado_engine_valueerror_maps_409(self, monkeypatch):
        # Defesa em profundidade: se o gate do motor levantar ValueError, vira 409.
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe("publicado")))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
        async def boom(**k):
            raise ValueError("Pipeline 'X' está aposentado — não é roteável.")
        monkeypatch.setattr(engine, "execute_pipeline", boom)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 409, r.text
