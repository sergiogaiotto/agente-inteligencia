"""PR #235 — IA, me ajude no Novo Endpoint.

# Contexto

Operador leigo precisava cadastrar um endpoint REST mas não sabia traduzir
"/api/ddd/v1/{ddd}" em `method/name/category/description/sample_body`. Agora
um botão 🪄 "IA, me ajude" no topo do modal de Novo Endpoint chama o LLM
primário da plataforma com a entrada do operador + contexto do connector
(base_url, name) e devolve um JSON estruturado com os 5 campos.

# Endpoint

POST /api/v1/api-connectors/suggest-endpoint
  body: { free_text: str, method_hint?: str, connector_id?: str }
  → 200 { suggestion: {...}, model: str, provider: str }
  → 400 free_text vazio
  → 502 LLM falhou ou devolveu JSON inválido
  → 503 provider não disponível

# Cobertura

- happy path com mock LLM, valida shape e sanitização do output
- contexto do connector entra no prompt (base_url, name, description)
- 400 free_text vazio
- 502 LLM devolve não-JSON
- 502 LLM throw
- método inválido cai para GET (defesa)
- sample_body string ou dict suportado
- Sintetiza o log estruturado api_connector.suggest_endpoint.completed
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── Helpers ──────────────────────────────────────────────


class _FakeProvider:
    last_messages = None
    next_content = '{"name":"X","method":"GET","path":"/x","category":"geral","description":"d","sample_body":"{}"}'
    next_raise: Exception | None = None
    last_kwargs = None

    async def generate(self, messages, **kwargs):
        _FakeProvider.last_messages = messages
        _FakeProvider.last_kwargs = kwargs
        if _FakeProvider.next_raise:
            raise _FakeProvider.next_raise
        return {
            "content": _FakeProvider.next_content,
            "model": "fake-primary",
            "usage": {"total_tokens": 50},
        }


def _fake_get_provider(name, **kw):
    return _FakeProvider()


def _app(monkeypatch, connectors: dict | None = None):
    from app.routes import api_connectors

    monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)

    if connectors is not None:
        async def fake_find(cid):
            return connectors.get(cid)

        # Patch _repos para que find_by_id (no conn_repo) funcione
        class _ConnRepo:
            async def find_by_id(self, cid):
                return connectors.get(cid)

        def fake_repos():
            return _ConnRepo(), None, None

        monkeypatch.setattr(api_connectors, "_repos", fake_repos)

    app = FastAPI()
    app.include_router(api_connectors.router)
    return TestClient(app)


# ─── Suite ─────────────────────────────────────────────────


class TestSuggestEndpointHappyPath:

    def test_returns_suggestion_with_required_fields(self, monkeypatch):
        _FakeProvider.next_content = json.dumps({
            "name": "Consultar DDD",
            "method": "GET",
            "path": "/api/ddd/v1/{ddd}",
            "category": "telefonia",
            "description": "Retorna estado e cidades atendidas pelo DDD.",
            "sample_body": "{}",
        })
        client = _app(monkeypatch)
        r = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "/api/ddd/v1/{ddd}",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        s = body["suggestion"]
        assert s["name"] == "Consultar DDD"
        assert s["method"] == "GET"
        assert s["path"] == "/api/ddd/v1/{ddd}"
        assert s["category"] == "telefonia"
        assert "DDD" in s["description"]
        assert s["sample_body"] == "{}"
        assert body["model"] == "fake-primary"

    def test_strips_markdown_fence_from_response(self, monkeypatch):
        """Alguns LLMs envelopam JSON em ```json...``` — backend descasca."""
        _FakeProvider.next_content = (
            "```json\n"
            + json.dumps({"name": "X", "method": "POST", "path": "/x",
                          "category": "geral", "description": "d",
                          "sample_body": '{"x":1}'})
            + "\n```"
        )
        client = _app(monkeypatch)
        r = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "foo",
        })
        assert r.status_code == 200, r.text
        assert r.json()["suggestion"]["method"] == "POST"

    def test_sample_body_dict_is_serialized(self, monkeypatch):
        """LLM pode devolver sample_body como objeto JSON em vez de string —
        backend serializa antes de mandar pra UI."""
        _FakeProvider.next_content = json.dumps({
            "name": "X", "method": "POST", "path": "/x",
            "category": "g", "description": "d",
            "sample_body": {"nome": "joão", "idade": 30},
        })
        client = _app(monkeypatch)
        body = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "criar cliente",
        }).json()
        assert isinstance(body["suggestion"]["sample_body"], str)
        assert '"nome"' in body["suggestion"]["sample_body"]


class TestSuggestEndpointSanitization:

    def test_unknown_method_falls_back_to_get(self, monkeypatch):
        """Defesa contra LLM inventar método."""
        _FakeProvider.next_content = json.dumps({
            "name": "X", "method": "FETCH", "path": "/x",
            "category": "g", "description": "d", "sample_body": "{}",
        })
        client = _app(monkeypatch)
        body = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "x",
        }).json()
        assert body["suggestion"]["method"] == "GET"

    def test_oversize_fields_are_truncated(self, monkeypatch):
        big = "X" * 5000
        _FakeProvider.next_content = json.dumps({
            "name": big, "method": "GET", "path": big,
            "category": big, "description": big, "sample_body": big,
        })
        client = _app(monkeypatch)
        s = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "x",
        }).json()["suggestion"]
        assert len(s["name"]) <= 100
        assert len(s["path"]) <= 300
        assert len(s["category"]) <= 50
        assert len(s["description"]) <= 300
        assert len(s["sample_body"]) <= 2000


class TestSuggestEndpointContext:

    def test_connector_context_enters_prompt(self, monkeypatch):
        store = {
            "c1": {
                "id": "c1", "name": "Brasilapi",
                "base_url": "https://brasilapi.com.br",
                "description": "API pública brasileira de utilidades.",
            },
        }
        client = _app(monkeypatch, connectors=store)
        _FakeProvider.last_messages = None
        client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "/api/cep/v1/{cep}",
            "connector_id": "c1",
        })
        assert _FakeProvider.last_messages is not None
        user_msg = _FakeProvider.last_messages[1]["content"]
        # base_url + nome aparecem no contexto
        assert "Brasilapi" in user_msg
        assert "brasilapi.com.br" in user_msg

    def test_no_connector_context_when_id_absent(self, monkeypatch):
        client = _app(monkeypatch, connectors={})
        _FakeProvider.last_messages = None
        client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "/foo",
        })
        user_msg = _FakeProvider.last_messages[1]["content"]
        assert "Contexto do connector" not in user_msg


class TestSuggestEndpointErrors:

    def test_400_when_free_text_empty(self, monkeypatch):
        client = _app(monkeypatch)
        r = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "",
        })
        assert r.status_code == 400

    def test_502_when_llm_returns_non_json(self, monkeypatch):
        _FakeProvider.next_content = "isso aqui não é json"
        client = _app(monkeypatch)
        r = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "x",
        })
        assert r.status_code == 502

    def test_502_when_llm_throws(self, monkeypatch):
        _FakeProvider.next_raise = RuntimeError("llm exploded")
        client = _app(monkeypatch)
        r = client.post("/api/v1/api-connectors/suggest-endpoint", json={
            "free_text": "x",
        })
        assert r.status_code == 502
        _FakeProvider.next_raise = None  # cleanup


class TestSuggestEndpointStructuredLog:

    def test_emits_completed_event_with_metadata(self, monkeypatch, caplog):
        _FakeProvider.next_content = json.dumps({
            "name": "X", "method": "POST", "path": "/x",
            "category": "g", "description": "d", "sample_body": "{}",
        })
        # connectors={} mocka _repos pra evitar acesso ao pool real
        client = _app(monkeypatch, connectors={"c1": {"id": "c1", "name": "Test"}})
        with caplog.at_level(logging.INFO, logger="app.routes.api_connectors"):
            client.post("/api/v1/api-connectors/suggest-endpoint", json={
                "free_text": "criar X",
                "connector_id": "c1",
            })
        recs = [r for r in caplog.records
                if getattr(r, "event", "") == "api_connector.suggest_endpoint.completed"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec.suggested_method == "POST"
        assert rec.connector_id == "c1"
        assert rec.free_text_len > 0
