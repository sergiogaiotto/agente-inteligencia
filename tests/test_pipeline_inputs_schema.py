"""GET /api/v1/pipelines/{id}/inputs-schema — inputs ESPERADOS do pipeline.

Helper do Playground ("inputs esperados" / "inserir template"): resolve o agente de
ENTRADA (raiz) do pipeline via _build_subgraph (a MESMA do invoke) e reusa o schema
do agente (## Inputs + variáveis dos API Bindings). Read-only.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.catalog.pipeline_defs as pdefs
import app.core.database as db
import app.routes.agents as agents_mod
from app.routes import pipelines as pl


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl.router)
    return TestClient(app, raise_server_exceptions=False)


def test_resolve_raiz_e_delega_pro_schema_do_agente(monkeypatch):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "root-1", "nodes": [], "edges": []}))

    async def fake_schema(aid):
        assert aid == "root-1", "deve introspectar o agente RAIZ resolvido"
        return {"agent": {"id": aid, "name": "Raiz"},
                "inputs_schema": {"properties": {"uf": {"type": "string"}}, "required": ["uf"]},
                "inputs_referenced": [], "api_bindings": []}

    monkeypatch.setattr(agents_mod, "get_agent_inputs_schema", fake_schema)
    r = _client().get("/api/v1/pipelines/p1/inputs-schema")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["pipeline_id"] == "p1" and d["root_agent_id"] == "root-1"
    assert d["agent"]["name"] == "Raiz"
    assert "uf" in d["inputs_schema"]["properties"]


def test_sem_raiz_retorna_schema_vazio(monkeypatch):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": None, "nodes": [], "edges": []}))
    r = _client().get("/api/v1/pipelines/p1/inputs-schema")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["root_agent_id"] is None and d["inputs_schema"] is None


def test_pipeline_inexistente_404(monkeypatch):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(None))
    r = _client().get("/api/v1/pipelines/nope/inputs-schema")
    assert r.status_code == 404, r.text
