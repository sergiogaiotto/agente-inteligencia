"""D4 — contrato de args SELADO/versionado.

Ao PUBLICAR (rascunho→publicado), o ## Inputs do agente-raiz é CONGELADO no pipeline
(schema + hash + versão). O invoke de um pipeline PUBLICADO valida contra o SELO —
estável mesmo que o autor edite o skill depois. Rascunho valida ao vivo. Re-publicar
re-sela (versão sobe só quando o hash muda). GET /inputs-schema expõe selo + drift.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
import app.routes.agents as agents_routes
from app.routes import pipelines as pl


def _async(v):
    async def f(*a, **k):
        return v
    return f


def _stateful(state):
    async def find_by_id(pid):
        return dict(state) if pid == state["id"] else None
    async def update(pid, patch):
        state.update(patch)
        return dict(state)
    return find_by_id, update


def _client():
    app = FastAPI()
    app.include_router(pl.router)
    app.dependency_overrides[pl.require_user] = lambda: {"id": "u", "role": "admin"}
    return TestClient(app, raise_server_exceptions=False)


SCHEMA = {"type": "object", "properties": {"cd_cliente": {"type": "integer"}}, "required": ["cd_cliente"]}
SCHEMA2 = {"type": "object", "properties": {"uf": {"type": "string"}}}


class TestSealHelpers:
    def test_schema_hash_stable_and_order_independent(self):
        a = {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "string"}}}
        b = {"properties": {"y": {"type": "string"}, "x": {"type": "integer"}}, "type": "object"}
        assert pl._schema_hash(a) == pl._schema_hash(b)          # sort_keys → ordem não importa
        assert pl._schema_hash(a) != pl._schema_hash(SCHEMA)

    def test_parse_contract(self):
        assert pl._parse_contract(json.dumps(SCHEMA)) == SCHEMA   # str JSONB
        assert pl._parse_contract(SCHEMA) == SCHEMA               # já dict
        assert pl._parse_contract("") is None and pl._parse_contract(None) is None


class TestSealOnPublish:
    def test_publish_seals_contract(self, monkeypatch):
        state = {"id": "p1", "name": "P", "status": "rascunho", "contract_version": None, "contract_hash": None}
        fb, up = _stateful(state)
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", fb)
        monkeypatch.setattr(db.pipelines_repo, "update", up)
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}]}))
        monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": SCHEMA}))
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        monkeypatch.setattr(db.pipeline_membership, "agents_of", _async(["r"]))

        r = _client().post("/api/v1/pipelines/p1/status", json={"status": "publicado"})
        assert r.status_code == 200, r.text
        assert state["status"] == "publicado"
        assert state["contract_version"] == 1
        assert state["contract_hash"] == pl._schema_hash(SCHEMA)
        assert json.loads(state["args_contract"]) == SCHEMA

    @pytest.mark.asyncio
    async def test_version_bumps_only_on_hash_change(self, monkeypatch):
        state = {"id": "p1", "status": "publicado", "contract_version": None, "contract_hash": None}
        fb, up = _stateful(state)
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", fb)
        monkeypatch.setattr(db.pipelines_repo, "update", up)
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}]}))
        monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": SCHEMA}))

        await pl._seal_args_contract("p1")
        assert state["contract_version"] == 1
        h1 = state["contract_hash"]
        await pl._seal_args_contract("p1")           # mesmo schema → versão fica 1
        assert state["contract_version"] == 1
        monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": SCHEMA2}))
        await pl._seal_args_contract("p1")           # schema mudou → versão 2
        assert state["contract_version"] == 2
        assert state["contract_hash"] != h1


def _wire_invoke(monkeypatch, pipe, live_schema, capture):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(pipe))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
    monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": live_schema}))

    async def fake_exec(**k):
        capture.update(k)
        return {"status": "completed", "output": "ok", "interaction_id": "i", "completed_agents": 1, "pipeline_steps": []}
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))


class TestInvokeUsesSealed:
    def test_published_validates_against_sealed_not_live(self, monkeypatch):
        # SELADO exige cd_cliente; o skill VIVO mudou (agora exige 'foo'). O invoke
        # publicado valida contra o SELO → args {cd_cliente:1} passam (live seria 422).
        pipe = {"id": "p1", "name": "P", "status": "publicado",
                "contract_hash": pl._schema_hash(SCHEMA), "args_contract": json.dumps(SCHEMA)}
        live = {"type": "object", "properties": {"foo": {"type": "string"}}, "required": ["foo"]}
        cap = {}
        _wire_invoke(monkeypatch, pipe, live, cap)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"cd_cliente": 1}, "dry": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sealed"] is True
        assert body["resolved_args"] == {"cd_cliente": 1}

    def test_draft_validates_against_live(self, monkeypatch):
        # rascunho → valida ao vivo. Live exige 'foo' e não mando → 422.
        pipe = {"id": "p1", "name": "P", "status": "rascunho", "contract_hash": None, "args_contract": None}
        live = {"type": "object", "properties": {"foo": {"type": "string"}}, "required": ["foo"]}
        cap = {}
        _wire_invoke(monkeypatch, pipe, live, cap)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {}, "dry": True})
        assert r.status_code == 422, r.text
        codes = {(i["field"], i["code"]) for i in r.json()["detail"]["issues"]}
        assert ("foo", "required_missing") in codes


class TestInputsSchemaExposesSeal:
    def test_sealed_and_drift_flagged(self, monkeypatch):
        # publicado + selo de SCHEMA; skill vivo agora é SCHEMA2 → drift.
        pipe = {"id": "p1", "name": "P", "status": "publicado", "contract_version": 3,
                "contract_hash": pl._schema_hash(SCHEMA), "args_contract": json.dumps(SCHEMA)}
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(pipe))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}]}))
        monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async(
            {"inputs_schema": SCHEMA2, "agent": {"id": "r"}, "inputs_referenced": [], "api_bindings": []}))

        r = _client().get("/api/v1/pipelines/p1/inputs-schema")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sealed"] is True
        assert body["contract_version"] == 3
        assert body["inputs_schema"] == SCHEMA          # expõe o SELADO, não o vivo
        assert body["contract_drift"] is True           # skill vivo divergiu do selo

    def test_draft_not_sealed(self, monkeypatch):
        pipe = {"id": "p1", "name": "P", "status": "rascunho", "contract_hash": None}
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(pipe))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}]}))
        monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": SCHEMA}))
        r = _client().get("/api/v1/pipelines/p1/inputs-schema")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sealed"] is False
        assert body["inputs_schema"] == SCHEMA          # rascunho mostra o vivo
