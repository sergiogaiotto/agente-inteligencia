"""Tradutor NL→args (item 2 do plano, 38.0.0) — POST /pipelines/{pid}/suggest-args.

DNA "IA sugere → sistema PROVA": LLM (mockado aqui) propõe o JSON, repairs
determinísticos consertam grafia de enum e podam nulls, e a PROVA é o mesmo
_resolve_args do invoke/dry — contra o schema SELADO quando publicado.
Nunca executa, nunca persiste; LLM que divaga vira sugestão-com-erro (200),
não 500. Moldes: test_mesh_rule_translator + test_pipeline_invoke_args_plan.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
from app.routes import pipelines as pl_routes
from app.agents.args_suggest import (
    build_args_messages, extract_args_json, repair_suggested_args,
)


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl_routes.router)
    app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test"}
    return TestClient(app, raise_server_exceptions=False)


_SCHEMA = {
    "type": "object",
    "required": ["cd_cliente"],
    "properties": {
        "cd_cliente": {"type": "integer", "description": "código do cliente"},
        "segmento": {"type": "string", "enum": ["varejo", "premium"]},
        "urgente": {"type": "boolean", "default": False},
    },
}


def _wire(monkeypatch, *, llm_reply: str, pipe: dict | None = None,
          schema: dict | None = _SCHEMA):
    seen = {}
    monkeypatch.setattr(
        db.pipelines_repo, "find_by_id",
        _async(pipe if pipe is not None
               else {"id": "p1", "name": "P", "status": "rascunho"}),
    )
    monkeypatch.setattr(
        "app.catalog.pipeline_defs._build_subgraph",
        _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}),
    )
    monkeypatch.setattr(pl_routes, "_fetch_root_schema", _async(schema))
    monkeypatch.setattr(
        "app.llm_routing.resolve_llm_for_task", _async(("azure", "gpt-4o"))
    )

    async def _fake_llm(messages, provider, model, **kw):
        seen["messages"] = messages
        seen["kwargs"] = kw
        return llm_reply, provider, model

    monkeypatch.setattr("app.routes.wizard._wizard_llm_complete", _fake_llm)
    return seen


def _post(payload):
    return _client().post("/api/v1/pipelines/p1/suggest-args", json=payload)


class TestSuggestArgs:
    def test_sugestao_valida_com_repair_e_prova(self, monkeypatch):
        """LLM manda número como string e enum com grafia errada — repairs +
        coerção da prova entregam args canônicos, com proveniência."""
        _wire(monkeypatch,
              llm_reply='{"cd_cliente": "1031", "segmento": "Varejo"}')
        r = _post({"description": "consulta o limite do cliente 1031 do varejo"})
        body = r.json()
        assert r.status_code == 200, r.text
        assert body["valid"] is True, body
        assert body["resolved_args"]["cd_cliente"] == 1031      # coerção (prova)
        assert body["resolved_args"]["segmento"] == "varejo"    # repair de enum
        assert body["resolved_args"]["urgente"] is False        # default aplicado
        assert body["provenance"]["urgente"] == "default"
        assert body["provenance"]["cd_cliente"] == "caller"
        assert body["sealed"] is False and body["has_schema"] is True

    def test_unknown_field_vira_issue_com_did_you_mean(self, monkeypatch):
        _wire(monkeypatch, llm_reply='{"cd_client": 1031}')
        body = _post({"description": "cliente 1031"}).json()
        assert body["valid"] is False
        issue = next(i for i in body["issues"] if i["code"] == "unknown_field")
        assert issue["did_you_mean"] == "cd_cliente"
        assert body["resolved_args"] is None
        assert body["args"] == {"cd_client": 1031}  # eco p/ o card da UI

    def test_required_ausente_vira_issue(self, monkeypatch):
        _wire(monkeypatch, llm_reply='{"segmento": "premium"}')
        body = _post({"description": "consulta do premium"}).json()
        assert body["valid"] is False
        assert any(i["code"] == "required_missing" and i["field"] == "cd_cliente"
                   for i in body["issues"])

    def test_cercas_e_prosa_sao_toleradas(self, monkeypatch):
        _wire(monkeypatch, llm_reply=(
            'Claro! Aqui está:\n```json\n{"cd_cliente": 7}\n```\nEspero ter ajudado.'
        ))
        body = _post({"description": "cliente 7"}).json()
        assert body["valid"] is True
        assert body["resolved_args"]["cd_cliente"] == 7

    def test_llm_divagando_vira_sugestao_com_erro_200(self, monkeypatch):
        _wire(monkeypatch, llm_reply="Desculpe, não entendi o pedido.")
        r = _post({"description": "cliente 7"})
        body = r.json()
        assert r.status_code == 200  # nunca 500 por divagação
        assert body["args"] is None and body["valid"] is False
        assert "JSON" in body["error"]

    def test_publicado_prova_contra_o_selo(self, monkeypatch):
        """Pipeline publicado: a prova usa o contrato SELADO — o schema vivo
        NÃO é consultado (a sugestão que passa aqui passa no invoke)."""
        pipe = {"id": "p1", "name": "P", "status": "publicado",
                "contract_hash": "h", "contract_version": 3,
                "args_contract": json.dumps(_SCHEMA)}

        async def _boom(*a, **k):
            raise AssertionError("schema vivo não deveria ser consultado")

        _wire(monkeypatch, llm_reply='{"cd_cliente": 1}', pipe=pipe)
        monkeypatch.setattr(pl_routes, "_fetch_root_schema", _boom)
        body = _post({"description": "cliente 1"}).json()
        assert body["valid"] is True
        assert body["sealed"] is True and body["contract_version"] == 3

    def test_sem_inputs_declarado_explica(self, monkeypatch):
        _wire(monkeypatch, llm_reply="{}", schema=None)
        body = _post({"description": "qualquer"}).json()
        assert body["has_schema"] is False
        assert "## Inputs" in body["error"]

    def test_sem_description_pede_descricao(self, monkeypatch):
        _wire(monkeypatch, llm_reply="{}")
        body = _post({}).json()
        assert "Descreva" in body["error"]

    def test_description_nao_string_nao_500(self, monkeypatch):
        _wire(monkeypatch, llm_reply='{"cd_cliente": 123}')
        r = _post({"description": 123})
        assert r.status_code == 200, r.text  # coagido a "123", segue o fluxo

    def test_partial_args_do_integrador_vencem(self, monkeypatch):
        """O LLM contradisse um campo já preenchido — o valor humano prevalece
        (e o prompt lista os campos travados)."""
        seen = _wire(monkeypatch,
                     llm_reply='{"cd_cliente": 999, "segmento": "varejo"}')
        body = _post({"description": "consulta do varejo",
                      "partial_args": {"cd_cliente": 1031}}).json()
        assert body["valid"] is True
        assert body["resolved_args"]["cd_cliente"] == 1031
        assert "cd_cliente" in seen["messages"][0]["content"].split(
            "JÁ preencheu")[1]

    def test_null_do_llm_e_podado(self, monkeypatch):
        _wire(monkeypatch, llm_reply='{"cd_cliente": 5, "segmento": null}')
        body = _post({"description": "cliente 5"}).json()
        assert body["valid"] is True
        assert "segmento" not in body["args"]

    def test_prompt_carrega_catalogo_do_schema(self, monkeypatch):
        seen = _wire(monkeypatch, llm_reply='{"cd_cliente": 1}')
        _post({"description": "cliente 1"})
        sys_msg = seen["messages"][0]["content"]
        assert "cd_cliente" in sys_msg and "OBRIGATÓRIO" in sys_msg
        assert "varejo | premium" in sys_msg
        assert "NUNCA invente" in sys_msg
        # temperatura determinística
        assert seen["kwargs"]["temperature"] == 0


class TestReviewFixes:
    """Achados da revisão adversarial do 38.0.0."""

    def test_api_key_recebe_403_acionavel(self, monkeypatch):
        """require_user aceita X-API-Key — sem o guard, 'cookie-only' era
        ficção (key queimava LLM e lia ## Inputs de rascunho mesmo com
        published_only=ON)."""
        _wire(monkeypatch, llm_reply='{"cd_cliente": 1}')
        app = FastAPI()

        @app.middleware("http")
        async def _fake_key(request, call_next):
            request.state.api_key_id = "k1"
            return await call_next(request)

        app.include_router(pl_routes.router)
        app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-key"}
        r = TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/pipelines/p1/suggest-args", json={"description": "cliente 1"}
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "suggest_args_ui_only"
        assert "dry" in r.json()["detail"]["message"]

    def test_partial_null_nao_clobba_o_llm(self, monkeypatch):
        """Form da UI manda toda chave com null nas vazias — null não pode
        sobrescrever o valor extraído pelo LLM."""
        _wire(monkeypatch, llm_reply='{"cd_cliente": 1031}')
        body = _post({"description": "cliente 1031",
                      "partial_args": {"cd_cliente": None,
                                       "segmento": "varejo"}}).json()
        assert body["valid"] is True
        assert body["resolved_args"]["cd_cliente"] == 1031
        assert body["resolved_args"]["segmento"] == "varejo"

    def test_partial_humano_nao_e_reescrito_pelo_repair(self, monkeypatch):
        """Valor humano com grafia fora do enum NÃO é normalizado em silêncio
        — vira issue da prova (a resposta não pode alegar que o valor do
        integrador prevaleceu depois de alterá-lo)."""
        _wire(monkeypatch, llm_reply='{"cd_cliente": 1}')
        body = _post({"description": "cliente 1",
                      "partial_args": {"segmento": "Premium"}}).json()
        assert body["valid"] is False
        assert body["args"]["segmento"] == "Premium"  # intacto
        assert any(i["code"] == "enum_mismatch" for i in body["issues"])

    def test_envelope_estavel_em_todos_os_ramos(self, monkeypatch):
        _wire(monkeypatch, llm_reply="prosa sem json")
        keys = {"args", "valid", "issues", "resolved_args", "provenance",
                "uso", "has_schema", "sealed", "contract_version", "error"}
        for payload in ({}, {"description": "cliente 1"}):
            body = _post(payload).json()
            assert keys <= set(body), f"faltam chaves no ramo {payload}: {keys - set(body)}"

    def test_bool_nao_fura_enum_numerico_na_prova(self, monkeypatch):
        """True == 1 em Python: sem a guarda, `true` passava num enum [1, 2]
        e o envelope selado carregava bool onde a integração espera int."""
        schema = {"type": "object", "properties": {"nivel": {"enum": [1, 2]}}}
        _wire(monkeypatch, llm_reply='{"nivel": true}', schema=schema)
        body = _post({"description": "nível máximo"}).json()
        assert body["valid"] is False
        assert any(i["code"] == "enum_mismatch" for i in body["issues"])

    def test_extract_ignora_chaves_de_prosa_antes_do_objeto(self):
        obj, err = extract_args_json(
            'Os campos {obrigatorios} sao: {"cd_cliente": 1031}')
        assert obj == {"cd_cliente": 1031}, err

    def test_extract_cerca_de_linha_unica(self):
        assert extract_args_json('```json {"cd_cliente": 1}```')[0] == {"cd_cliente": 1}
        assert extract_args_json('```{"a": 1}```')[0] == {"a": 1}

    def test_extract_chave_dentro_de_aspas_de_prosa(self):
        obj, _ = extract_args_json(
            'Use o formato "{campo: valor}" assim: {"cd_cliente": 7}')
        assert obj == {"cd_cliente": 7}


class TestHelpers:
    def test_repair_enum_por_grafia(self):
        schema = {"properties": {"s": {"enum": ["não", "sim"]}}}
        assert repair_suggested_args({"s": "NAO"}, schema) == {"s": "não"}
        # exato não muda; fora do enum fica p/ a PROVA rejeitar (não silencia)
        assert repair_suggested_args({"s": "sim"}, schema) == {"s": "sim"}
        assert repair_suggested_args({"s": "talvez"}, schema) == {"s": "talvez"}

    def test_extract_variantes(self):
        assert extract_args_json('{"a": 1}')[0] == {"a": 1}
        assert extract_args_json('```json\n{"a": 1}\n```')[0] == {"a": 1}
        assert extract_args_json('Segue: {"a": {"b": "}"}} ok')[0] == {"a": {"b": "}"}}
        assert extract_args_json("[1, 2]")[0] is None   # lista não é args
        assert extract_args_json("")[0] is None

    def test_build_messages_sem_partial_nao_menciona_trava(self):
        msgs = build_args_messages("x", _SCHEMA, None)
        assert "JÁ preencheu" not in msgs[0]["content"]
