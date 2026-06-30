"""Plan/dry + defaults + proveniência — camada sobre os args (D1/D2).

- Defaults do ## Inputs (antes ignorados) agora são aplicados: o caller pode
  omitir campos com `default`, e um required-com-default é satisfeito.
- `dry: true` no /invoke RESOLVE os args (coage/defaults/valida) e devolve o
  payload resolvido + proveniência (caller|default) SEM executar (não gasta LLM).
"""
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


def _wire(monkeypatch, schema=None, capture=None):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P", "status": "publicado"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
    monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": schema}))
    captured = capture if capture is not None else {}

    async def fake_exec(**k):
        captured.update(k)
        return {"status": "completed", "output": "ok", "interaction_id": "i", "completed_agents": 1, "pipeline_steps": []}
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))
    return captured


class TestDefaults:
    def test_default_applied_when_caller_omits(self, monkeypatch):
        cap = _wire(monkeypatch, schema=_schema({
            "uf": {"type": "string"},
            "canal": {"type": "string", "default": "app"},
        }))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"uf": "RS"}})
        assert r.status_code == 200, r.text
        # default 'canal' entrou no payload dobrado mesmo o caller não mandando
        assert '"canal": "app"' in cap["user_input"]
        assert '"uf": "RS"' in cap["user_input"]

    def test_required_with_default_is_satisfied(self, monkeypatch):
        # required + default + caller omite → o default satisfaz (não dá 422).
        cap = _wire(monkeypatch, schema=_schema(
            {"tier": {"type": "string", "default": "free"}}, required=["tier"]))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {}, "message": "oi"})
        assert r.status_code == 200, r.text
        assert '"tier": "free"' in cap["user_input"]

    def test_caller_value_overrides_default(self, monkeypatch):
        cap = _wire(monkeypatch, schema=_schema({"canal": {"type": "string", "default": "app"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"canal": "web"}})
        assert r.status_code == 200, r.text
        assert '"canal": "web"' in cap["user_input"]


class TestDryPlan:
    def test_dry_returns_resolved_and_provenance_without_executing(self, monkeypatch):
        cap = _wire(monkeypatch, schema=_schema({
            "cd_cliente": {"type": "integer"},
            "canal": {"type": "string", "default": "app"},
        }))
        r = _client().post("/api/v1/pipelines/p1/invoke",
                           json={"args": {"cd_cliente": "4071"}, "dry": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry"] is True
        assert body["resolved_args"] == {"cd_cliente": 4071, "canal": "app"}
        assert body["provenance"] == {"cd_cliente": "caller", "canal": "default"}
        assert body["has_schema"] is True
        # NÃO executou o pipeline
        assert "user_input" not in cap

    def test_dry_validates_and_422s(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({"uf": {"type": "string", "enum": ["RS", "SP"]}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"uf": "XX"}, "dry": True})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["issues"][0]["code"] == "enum_mismatch"

    def test_dry_without_args_previews_defaults(self, monkeypatch):
        # dry sem args nem mensagem → não dá 400; mostra os defaults que entrariam.
        cap = _wire(monkeypatch, schema=_schema({"canal": {"type": "string", "default": "app"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"dry": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resolved_args"] == {"canal": "app"}
        assert body["provenance"] == {"canal": "default"}
        assert "user_input" not in cap

    def test_dry_no_schema_echoes_caller_args(self, monkeypatch):
        cap = _wire(monkeypatch, schema=None)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"x": 1}, "dry": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resolved_args"] == {"x": 1}
        assert body["provenance"] == {"x": "caller"}
        assert body["has_schema"] is False
        assert "user_input" not in cap
