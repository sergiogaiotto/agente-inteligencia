"""Verbosidade da resposta de invoke (full | summary | minimal).

Achado do teste E2E como usuário (2026-06-23): `POST /pipelines/{id}/invoke`
devolvia ~29 KB de debug — inadequado p/ UI externa, e expunha SQL renderado e
custo. Esta feature projeta a resposta server-side em 3 níveis, com default
CIENTE DE AUTH (sessão→full; X-API-Key→platform_settings, semente 'summary').

Cobre: helpers puros (normalize/resolve/project) + fiação na rota
(default sessão, body, query override, e default por API-key honrando o setting).
Ver `app/agents/result_view.py` e `docs/backlog-teste-e2e.md` (item C-verbosity).
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
from app.routes import pipelines as pl_routes
from app.agents.result_view import (
    normalize_verbosity,
    resolve_verbosity,
    project_pipeline_result,
)


# ───────────────────────── helpers puros ─────────────────────────

class TestNormalize:
    def test_valid_passa(self):
        assert normalize_verbosity("summary") == "summary"
        assert normalize_verbosity("MINIMAL") == "minimal"   # case-insensitive
        assert normalize_verbosity("  full  ") == "full"     # trim
    def test_invalido_cai_no_fallback(self):
        assert normalize_verbosity("xpto") == "full"
        assert normalize_verbosity(None) == "full"
        assert normalize_verbosity("") == "full"
        assert normalize_verbosity("bla", fallback="summary") == "summary"


class TestResolve:
    def test_sessao_default_full(self):
        assert resolve_verbosity(None, is_api_key=False) == "full"
    def test_apikey_default_usa_api_default(self):
        assert resolve_verbosity(None, is_api_key=True) == "summary"
        assert resolve_verbosity(None, is_api_key=True, api_default="minimal") == "minimal"
    def test_explicito_sempre_vence(self):
        assert resolve_verbosity("minimal", is_api_key=False) == "minimal"
        assert resolve_verbosity("full", is_api_key=True, api_default="minimal") == "full"
    def test_explicito_invalido_cai_no_fallback_por_auth(self):
        # sessão: explícito inválido → full
        assert resolve_verbosity("nope", is_api_key=False) == "full"
        # X-API-Key: inválido (typo) NÃO escala pra full (vazaria debug) → api_default
        assert resolve_verbosity("nope", is_api_key=True, api_default="minimal") == "minimal"
        assert resolve_verbosity("summry", is_api_key=True, api_default="summary") == "summary"


# resultado "full" rico p/ provar o recorte
_FULL_STEP = {
    "agent_id": "r", "agent_name": "Raiz", "agent_kind": "aobd", "agent_model": "m",
    "status": "completed", "status_message": "Pesquisando…", "output": "saida do step",
    "final_state": "Recommend", "duration_ms": 10, "evidence_score": 1.0,
    "transitions": [{"to": "X"}],
    "trace": {"execution_log": [], "sql_rendered": 'SELECT "cd_cliente" FROM data'},
    "cost_usd": 0.001, "tokens_used": 100,
}
_FULL = {
    "pipeline_id": "p1", "status": "completed", "output": "resposta final",
    "final_state": "Recommend", "interaction_id": "int1",
    "total_agents": 1, "completed_agents": 1,
    "pipeline_steps": [_FULL_STEP], "duration_ms": 42,
}


class TestProject:
    def test_full_eh_passthrough(self):
        out = project_pipeline_result(_FULL, "full")
        # full preserva TODO o payload legado (verbatim) + campos ADITIVOS do
        # envelope (schema_version/verbosity/data). Não muta o dict de entrada.
        for k, v in _FULL.items():
            assert out[k] == v
        assert out["schema_version"] == "1" and out["verbosity"] == "full"

    def test_minimal_so_resposta(self):
        out = project_pipeline_result(_FULL, "minimal")
        assert out == {
            "schema_version": "1",
            "pipeline_id": "p1", "interaction_id": "int1",
            "status": "completed", "output": "resposta final",
            "data": None, "output_is_json": False, "verbosity": "minimal",
        }
        assert "pipeline_steps" not in out and "steps" not in out

    def test_summary_tem_narrativa_sem_tripa_interna(self):
        out = project_pipeline_result(_FULL, "summary")
        assert out["output"] == "resposta final"
        assert out["completed_agents"] == 1 and out["total_agents"] == 1
        # narrativa (processing_message) exposta por step
        st = out["steps"][0]
        assert st["status_message"] == "Pesquisando…"
        assert st["agent_name"] == "Raiz" and st["output"] == "saida do step"
        # NÃO vaza tripa interna no step
        for leak in ("trace", "cost_usd", "tokens_used", "transitions", "agent_id", "agent_model", "evidence_score"):
            assert leak not in st, f"summary vazou '{leak}' no step"
        # nem no topo
        assert "pipeline_steps" not in out and "trace" not in out

    def test_summary_preserva_erro_de_step(self):
        # step que falhou no engine ({status:'error', error:...}): o motivo NÃO pode
        # sumir no summary (senão a integração vê 'error' sem explicação nenhuma).
        result = {
            "status": "completed", "output": "x", "interaction_id": "i",
            "pipeline_steps": [{"agent_name": "A", "status": "error", "error": "boom: timeout"}],
        }
        st = project_pipeline_result(result, "summary")["steps"][0]
        assert st["status"] == "error" and st["error"] == "boom: timeout"


# ───────────────────────── fiação na rota ─────────────────────────

def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client(as_api_key=False):
    app = FastAPI()
    app.include_router(pl_routes.router)
    if as_api_key:
        def _ovr(request: Request):
            request.state.api_key_id = "key-1"   # simula chamada via X-API-Key
            return {"id": "u-test", "role": "admin"}
    else:
        def _ovr():
            return {"id": "u-test", "role": "admin"}  # sessão (sem api_key_id)
    app.dependency_overrides[pl_routes.require_user] = _ovr
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stub_engine(monkeypatch):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "F", "status": "publicado"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
    monkeypatch.setattr(engine, "execute_pipeline", _async(dict(_FULL)))
    monkeypatch.setattr(db.audit_repo, "create", _async({}))


class TestRouteVerbosity:
    def test_sessao_default_full(self, stub_engine):
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 200, r.text
        body = r.json()
        # full mantém o contrato legado (pipeline_steps com agent_id + trace)
        assert body["pipeline_steps"][0]["agent_id"] == "r"
        assert "trace" in body["pipeline_steps"][0]
        # Envelope auto-descritivo (P1-B): full também carrega schema_version +
        # verbosity + data (aditivo; o payload legado segue verbatim).
        assert body["verbosity"] == "full"
        assert body["schema_version"] == "1"

    def test_body_minimal(self, stub_engine):
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi", "verbosity": "minimal"})
        body = r.json()
        assert body["verbosity"] == "minimal"
        assert body["output"] == "resposta final"
        assert "pipeline_steps" not in body and "steps" not in body

    def test_body_summary_expoe_narrativa_e_esconde_sql(self, stub_engine):
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi", "verbosity": "summary"})
        body = r.json()
        assert body["verbosity"] == "summary"
        assert body["steps"][0]["status_message"] == "Pesquisando…"
        # NENHUM campo de tripa interna pode aparecer no payload serializado inteiro
        for leak in ("sql_rendered", "cost_usd", "tokens_used", "transitions",
                     '"trace"', "evidence_score", "agent_model"):
            assert leak not in r.text, f"summary vazou '{leak}'"

    def test_query_override_vence_body(self, stub_engine):
        r = _client().post("/api/v1/pipelines/p1/invoke?verbosity=minimal",
                           json={"message": "oi", "verbosity": "summary"})
        body = r.json()
        assert body["verbosity"] == "minimal"   # query > body
        assert "steps" not in body

    def test_apikey_default_honra_platform_setting(self, stub_engine, monkeypatch):
        # via X-API-Key, sem verbosity explícito → lê platform_settings; aqui 'minimal'.
        # spy (em vez do stub que ignora args) prova que lê a CHAVE certa.
        from unittest.mock import AsyncMock
        spy = AsyncMock(return_value="minimal")
        monkeypatch.setattr(db.settings_store, "get", spy)
        r = _client(as_api_key=True).post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        body = r.json()
        assert body["verbosity"] == "minimal"
        assert "pipeline_steps" not in body and "steps" not in body
        spy.assert_awaited_with("api_invoke_default_verbosity", "summary")

    def test_apikey_typo_nao_vaza_full(self, stub_engine, monkeypatch):
        # achado HIGH da revisão: ?verbosity=<typo> via X-API-Key NÃO pode virar full.
        monkeypatch.setattr(db.settings_store, "get", _async("summary"))
        r = _client(as_api_key=True).post("/api/v1/pipelines/p1/invoke?verbosity=summry",
                                          json={"message": "oi"})
        body = r.json()
        assert body.get("verbosity") == "summary"      # caiu no default, não em full
        assert "pipeline_steps" not in body
        assert "sql_rendered" not in r.text and "cost_usd" not in r.text
