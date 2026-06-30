"""D1/D2 — campo `args` estruturado no invoke de pipeline.

POST /api/v1/pipelines/{id}/invoke aceita `args` (dict) OPCIONAL. Os args são
validados/coagidos contra o ## Inputs do agente-raiz (o mesmo schema que o
/inputs-schema publica) ANTES de executar: 422 nomeando cada campo (required
ausente, tipo, enum, chave fora do contrato com did-you-mean). Quando válidos,
são DOBRADOS na entrada como bloco "## Parâmetros estruturados" (a raiz LLM lê
como contexto; a raiz declarativa extrai via _extract_inputs_from_text).

Texto livre em `message` continua o caminho primário e intacto.
"""
import json

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


def _pipe(status="publicado"):
    return {"id": "p1", "name": "Folha", "status": status}


def _schema(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


def _wire(monkeypatch, schema=None, capture=None):
    """Monkeypatcha o caminho do invoke: pipeline existe, subgrafo com raiz,
    schema do agente-raiz, execução fake que captura kwargs, audit no-op."""
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({
        "root_agent_id": "r", "nodes": [{"id": "r"}, {"id": "a"}], "edges": [],
    }))
    monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", _async({"inputs_schema": schema}))

    captured = capture if capture is not None else {}

    async def fake_exec(**k):
        captured.update(k)
        return {"status": "completed", "output": "ok", "final_state": "Recommend",
                "interaction_id": "int1", "total_agents": 2, "completed_agents": 2,
                "pipeline_steps": [], "duration_ms": 1}
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))
    return captured


class TestInvokeArgs:
    def test_args_only_no_message_executes(self, monkeypatch):
        cap = _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"cd_cliente": 4071}})
        assert r.status_code == 200, r.text
        # Args dobrados na entrada como bloco estruturado.
        ui = cap["user_input"]
        assert "## Parâmetros estruturados" in ui
        assert '"cd_cliente": 4071' in ui

    def test_message_plus_args_keeps_both(self, monkeypatch):
        cap = _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke",
                           json={"message": "Analise o risco", "args": {"cd_cliente": 4071}})
        assert r.status_code == 200, r.text
        ui = cap["user_input"]
        assert ui.startswith("Analise o risco")
        assert "## Parâmetros estruturados" in ui

    def test_coercion_string_to_int(self, monkeypatch):
        cap = _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"cd_cliente": "4071"}})
        assert r.status_code == 200, r.text
        # Coagido para int — no JSON dobrado sai SEM aspas.
        assert '"cd_cliente": 4071' in cap["user_input"]

    def test_422_required_missing(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}}, required=["cd_cliente"]))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"uf": "RS"}})
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        codes = {(i["field"], i["code"]) for i in detail["issues"]}
        assert ("cd_cliente", "required_missing") in codes
        assert detail["schema_url"].endswith("/inputs-schema")

    def test_422_unknown_field_with_did_you_mean(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({"uf": {"type": "string"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"ufe": "RS"}})
        assert r.status_code == 422, r.text
        issues = {i["field"]: i for i in r.json()["detail"]["issues"]}
        assert issues["ufe"]["code"] == "unknown_field"
        assert issues["ufe"]["did_you_mean"] == "uf"

    def test_422_type_mismatch_uncoercible(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"cd_cliente": "abc"}})
        assert r.status_code == 422, r.text
        issues = {i["field"]: i for i in r.json()["detail"]["issues"]}
        assert issues["cd_cliente"]["code"] == "type_mismatch"
        assert issues["cd_cliente"]["expected"] == "integer"

    def test_422_enum_mismatch(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({"uf": {"type": "string", "enum": ["RS", "SP"]}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"uf": "XX"}})
        assert r.status_code == 422, r.text
        issues = {i["field"]: i for i in r.json()["detail"]["issues"]}
        assert issues["uf"]["code"] == "enum_mismatch"
        assert issues["uf"]["allowed"] == ["RS", "SP"]

    def test_enum_integer_without_type_accepts_string(self, monkeypatch):
        # enum [1,2] SEM `type`: o valor chega como "1" (form/JSON) e NÃO pode dar 422
        # falso (comparação tolerante a tipo, casando com a validação do cliente).
        cap = _wire(monkeypatch, schema=_schema({"nivel": {"enum": [1, 2]}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"nivel": "1"}})
        assert r.status_code == 200, r.text
        assert '"nivel": "1"' in cap["user_input"]

    def test_422_required_empty_string(self, monkeypatch):
        # required satisfeito por "  " (vazio) é inválido — alinha com o cliente.
        _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "string"}}, required=["cd_cliente"]))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"cd_cliente": "  "}})
        assert r.status_code == 422, r.text
        codes = {(i["field"], i["code"]) for i in r.json()["detail"]["issues"]}
        assert ("cd_cliente", "required_missing") in codes

    def test_400_when_args_only_empty_optionals(self, monkeypatch):
        # args só com opcionais vazios (coerção poda tudo) + sem mensagem → 400; NÃO
        # executa o pipeline (não gasta LLM com entrada vazia).
        cap = _wire(monkeypatch, schema=_schema({"uf": {"type": "string"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"uf": ""}})
        assert r.status_code == 400, r.text
        assert "user_input" not in cap  # execute_pipeline não foi chamado

    def test_no_schema_passes_args_raw(self, monkeypatch):
        # Raiz sem ## Inputs → schema None → args passam crus (sem validação),
        # ainda dobrados na entrada. Mantém o "texto livre" como contrato.
        cap = _wire(monkeypatch, schema=None)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"qualquer": "coisa"}})
        assert r.status_code == 200, r.text
        assert '"qualquer": "coisa"' in cap["user_input"]

    def test_orphan_root_404_treated_as_no_schema(self, monkeypatch):
        # get_agent_inputs_schema lançando 404 (raiz órfã) NÃO vaza 404 de "agente"
        # num invoke de pipeline válido — trata como sem-contrato.
        from fastapi import HTTPException
        cap = _wire(monkeypatch, schema=None)

        async def boom(*a, **k):
            raise HTTPException(404, "Agente não encontrado")
        monkeypatch.setattr(agents_routes, "get_agent_inputs_schema", boom)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"args": {"x": 1}})
        assert r.status_code == 200, r.text
        assert '"x": 1' in cap["user_input"]

    def test_arg_keys_audited(self, monkeypatch):
        _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}, "uf": {"type": "string"}}))
        seen = {}

        async def cap_audit(row):
            seen.update(row)
            return {}
        monkeypatch.setattr(db.audit_repo, "create", cap_audit)
        r = _client().post("/api/v1/pipelines/p1/invoke",
                           json={"args": {"uf": "RS", "cd_cliente": 1}})
        assert r.status_code == 200, r.text
        details = json.loads(seen["details"])
        assert details["arg_keys"] == ["cd_cliente", "uf"]

    def test_400_when_no_message_and_no_args(self, monkeypatch):
        # Guard rápido ANTES de montar subgrafo: sem message E sem args → 400.
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={})
        assert r.status_code == 400, r.text

    def test_message_only_still_works_unchanged(self, monkeypatch):
        # Regressão: texto livre sem args nunca toca o caminho de validação.
        cap = _wire(monkeypatch, schema=_schema({"cd_cliente": {"type": "integer"}}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 200, r.text
        assert cap["user_input"] == "oi"
        assert "## Parâmetros estruturados" not in cap["user_input"]
