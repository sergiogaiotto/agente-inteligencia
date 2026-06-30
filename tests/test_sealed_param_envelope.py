"""Envelope param selado (`x-uso: param`) — Fase 1 (baldes) + Fase 2 (escopo selado).

- Os args marcados `x-uso: param` no ## Inputs viajam FORA da prosa, num envelope
  selado que acompanha toda a cadeia e chega intacto a qualquer agente declarativo.
- O caller é SOBERANO: o valor selado sobrepõe o que um roteador upstream (AOBD/SR)
  tenha emitido no bloco {"target","inputs"} — o determinismo sobrevive aos saltos LLM.
- Args sem `x-uso` (ou `x-uso: llm`) seguem na prosa (comportamento legado).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
import app.routes.agents as agents_routes
from app.routes import pipelines as pl_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl_routes.router)
    app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test", "role": "admin"}
    return TestClient(app, raise_server_exceptions=False)


def _schema(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


def _wire(monkeypatch, schema, capture):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P", "status": "publicado"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
    monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": schema}))

    async def fake_exec(**k):
        capture.update(k)
        return {"status": "completed", "output": "ok", "interaction_id": "i", "completed_agents": 1, "pipeline_steps": []}
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))


# ── Fase 2: o merge selado no consumo do agente declarativo ──────────────────
class TestSealedMerge:
    @pytest.mark.asyncio
    async def test_sealed_overrides_router_emitted_block(self, monkeypatch):
        # roteador upstream emitiu cd_cliente=999 + tom; envelope selado manda 4071.
        # caller é SOBERANO no campo param → 4071 vence; o llm do roteador (tom) fica.
        captured = {}
        async def fake_decl(**kw):
            captured.update(kw)
            return {"context": {"resposta": "ok"}, "bindings_executed": [{"status": 200}], "errors": [], "final_state": "completed"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_decl)
        router_block = '```json\n{"target": "SA", "inputs": {"cd_cliente": 999, "tom": "informal"}}\n```'
        await engine._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(), user_input=router_block,
            session_id=None, sealed_inputs={"cd_cliente": 4071},
        )
        assert captured["inputs"]["cd_cliente"] == 4071   # caller soberano
        assert captured["inputs"]["tom"] == "informal"    # llm do roteador preservado

    @pytest.mark.asyncio
    async def test_sealed_fills_when_no_block(self, monkeypatch):
        # texto puro (sem bloco) → {"question": texto} + envelope selado mesclado.
        captured = {}
        async def fake_decl(**kw):
            captured.update(kw)
            return {"context": {}, "bindings_executed": [], "errors": [], "final_state": "completed", "api_response": "ok"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_decl)
        await engine._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(), user_input="analise",
            session_id=None, sealed_inputs={"cd_cliente": 4071},
        )
        assert captured["inputs"]["cd_cliente"] == 4071
        assert captured["inputs"]["question"] == "analise"

    @pytest.mark.asyncio
    async def test_no_sealed_is_legacy(self, monkeypatch):
        # sealed_inputs=None → comportamento legado (só o extraído).
        captured = {}
        async def fake_decl(**kw):
            captured.update(kw)
            return {"context": {}, "bindings_executed": [], "errors": [], "final_state": "completed", "api_response": "ok"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_decl)
        await engine._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(), user_input='{"cep": "1"}',
            session_id=None, sealed_inputs=None,
        )
        assert captured["inputs"] == {"cep": "1"}


# ── Fase 1: a separação em baldes no invoke ──────────────────────────────────
class TestBucketing:
    def test_param_field_sealed_llm_field_prose(self, monkeypatch):
        cap = {}
        _wire(monkeypatch, schema=_schema({
            "cd_cliente": {"type": "integer", "x-uso": "param"},
            "tom": {"type": "string", "x-uso": "llm"},
        }), capture=cap)
        r = _client().post("/api/v1/pipelines/p1/invoke",
                           json={"args": {"cd_cliente": 4071, "tom": "formal"}})
        assert r.status_code == 200, r.text
        # param → envelope selado (out-of-band)
        assert cap["sealed_inputs"] == {"cd_cliente": 4071}
        # llm → prosa; e o param NÃO aparece na prosa (não passa pelo LLM)
        assert '"tom": "formal"' in cap["user_input"]
        assert "cd_cliente" not in cap["user_input"]

    def test_no_xuso_goes_to_prose_nothing_sealed(self, monkeypatch):
        cap = {}
        _wire(monkeypatch, schema=_schema({"x": {"type": "string"}}), capture=cap)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"x": "v"}})
        assert r.status_code == 200, r.text
        assert cap["sealed_inputs"] is None          # nada selado (default = prosa)
        assert '"x": "v"' in cap["user_input"]

    def test_all_param_no_prose_block(self, monkeypatch):
        # tudo param + sem mensagem → prosa vazia (sem bloco), tudo no envelope.
        cap = {}
        _wire(monkeypatch, schema=_schema({"cd": {"type": "integer", "x-uso": "param"}}), capture=cap)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"cd": 7}})
        assert r.status_code == 200, r.text
        assert cap["sealed_inputs"] == {"cd": 7}
        assert "## Parâmetros estruturados" not in cap["user_input"]

    def test_dry_exposes_uso_buckets(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({
            "cd": {"type": "integer", "x-uso": "param"},
            "tom": {"type": "string"},
        }), capture={})
        r = _client().post("/api/v1/pipelines/p1/invoke",
                           json={"args": {"cd": 1, "tom": "x"}, "dry": True})
        assert r.status_code == 200, r.text
        assert r.json()["uso"] == {"cd": "param", "tom": "llm"}
