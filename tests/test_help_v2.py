"""Testes do endpoint V2 do help contextual (PR 5 — Guia Interativo).

Cobre:
- HelpAskContextV2Request: shape Pydantic aceita sections com items
- _render_v2_context: serializa sections em texto otimizado para LLM
  (cabeçalhos por kind, metadados required/default/severity/options)
- POST /api/v1/help/ask-context-v2: endpoint retorna 200 mockando provider
- Endpoint legacy /ask-context continua funcionando (sem regressão)
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.help import (
    HelpAskContextV2Request,
    HelpSection,
    HelpSectionItem,
    _render_v2_context,
    router as help_router,
)


class TestSchemaV2:
    def test_aceita_sections_com_items(self):
        req = HelpAskContextV2Request(
            title="Agentes",
            summary="Resumo curto",
            sections=[
                HelpSection(kind="concept", title="O que é", body="<p>Um agent é...</p>"),
                HelpSection(kind="campos", title="Campos da tela", items=[
                    HelpSectionItem(name="Nome", body="Como aparece em listas", required=True),
                    HelpSectionItem(name="Tipo", body="Camada do agent", options=["aobd", "router", "subagent"], default="subagent"),
                ]),
                HelpSection(kind="pegadinhas", title="Pegadinhas", items=[
                    HelpSectionItem(title="Agent não é Skill", body="...", severity="info"),
                    HelpSectionItem(title="Edit em produção", body="...", severity="danger"),
                ]),
            ],
            related=["skills", "workspace"],
            question="O que é o campo Tipo?",
        )
        assert req.title == "Agentes"
        assert len(req.sections) == 3
        assert req.sections[1].items[1].options == ["aobd", "router", "subagent"]
        assert req.sections[2].items[1].severity == "danger"

    def test_sections_vazio_ok(self):
        req = HelpAskContextV2Request(title="X", question="?")
        assert req.sections == []
        assert req.summary == ""

    def test_question_obrigatoria(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            HelpAskContextV2Request(title="X", sections=[])


class TestRenderContext:
    def test_render_inclui_titulo_e_summary(self):
        req = HelpAskContextV2Request(
            title="Agentes", summary="Trabalhadores", question="?",
        )
        out = _render_v2_context(req)
        assert "Agentes" in out
        assert "Trabalhadores" in out

    def test_render_section_concept_body(self):
        req = HelpAskContextV2Request(
            title="X", question="?",
            sections=[HelpSection(kind="concept", title="O que é", body="<p>Texto</p>")],
        )
        out = _render_v2_context(req)
        assert "CONCEITO" in out
        assert "Texto" in out
        assert "<p>" not in out

    def test_render_campos_metadados(self):
        req = HelpAskContextV2Request(
            title="X", question="?",
            sections=[HelpSection(kind="campos", title="Campos", items=[
                HelpSectionItem(name="Nome", body="explicação", required=True, default="—", example="Ex foo"),
                HelpSectionItem(name="Tipo", body="opção", options=["a", "b", "c"]),
            ])],
        )
        out = _render_v2_context(req)
        assert "CAMPOS DA TELA" in out
        assert "Nome" in out
        assert "OBRIGATÓRIO" in out
        assert "default=—" in out
        assert "Ex foo" in out
        assert "Tipo" in out
        assert "a, b, c" in out

    def test_render_pegadinhas_severity(self):
        req = HelpAskContextV2Request(
            title="X", question="?",
            sections=[HelpSection(kind="pegadinhas", title="Pegadinhas", items=[
                HelpSectionItem(title="P1", body="cuidado X", severity="warning"),
                HelpSectionItem(title="P2", body="critico Y", severity="danger"),
            ])],
        )
        out = _render_v2_context(req)
        assert "PEGADINHAS" in out
        assert "sev=warning" in out
        assert "sev=danger" in out

    def test_render_casos_de_uso(self):
        req = HelpAskContextV2Request(
            title="X", question="?",
            sections=[HelpSection(kind="casos_de_uso", title="Casos", items=[
                HelpSectionItem(title="Caso A", body="descrição A"),
                HelpSectionItem(title="Caso B", body="descrição B"),
            ])],
        )
        out = _render_v2_context(req)
        assert "CASOS DE USO" in out
        assert "Caso A" in out
        assert "descrição A" in out

    def test_render_related_pages(self):
        req = HelpAskContextV2Request(
            title="X", question="?",
            related=["skills", "catalog"],
        )
        out = _render_v2_context(req)
        assert "PÁGINAS RELACIONADAS" in out
        assert "skills" in out
        assert "catalog" in out

    def test_render_kind_desconhecido_usa_uppercase(self):
        req = HelpAskContextV2Request(
            title="X", question="?",
            sections=[HelpSection(kind="kind_novo", title="Algo", body="conteudo")],
        )
        out = _render_v2_context(req)
        assert "KIND_NOVO" in out


class TestEndpointV2:
    def _app(self, monkeypatch):
        class FakeProvider:
            model = "fake"
            async def generate(self, messages, max_tokens=600):
                user_msg = next((m for m in messages if m["role"] == "user"), {})
                FakeProvider.last_user = user_msg.get("content", "")
                return {"content": "Resposta mockada.", "model": "fake", "usage": {"total_tokens": 100}}

        FakeProvider.last_user = ""
        monkeypatch.setattr("app.routes.help.get_provider", lambda name: FakeProvider())

        app = FastAPI()
        app.include_router(help_router)
        return app, FakeProvider

    def test_endpoint_200_payload_minimo(self, monkeypatch):
        app, _ = self._app(monkeypatch)
        c = TestClient(app)
        r = c.post("/api/v1/help/ask-context-v2", json={
            "title": "Agentes", "question": "O que é?"
        })
        assert r.status_code == 200
        body = r.json()
        assert "answer" in body
        assert body["answer"] == "Resposta mockada."

    def test_endpoint_envia_contexto_rico_pro_llm(self, monkeypatch):
        app, FakeProvider = self._app(monkeypatch)
        c = TestClient(app)
        r = c.post("/api/v1/help/ask-context-v2", json={
            "title": "Agentes",
            "summary": "Trabalhadores da plataforma",
            "sections": [
                {"kind": "concept", "title": "O que é", "body": "Texto conceito"},
                {"kind": "campos", "title": "Campos", "items": [
                    {"name": "Nome", "body": "Nome do agent", "required": True}
                ]},
            ],
            "related": ["skills"],
            "question": "Qual o default do campo Nome?",
        })
        assert r.status_code == 200
        sent = FakeProvider.last_user
        assert "Agentes" in sent
        assert "Trabalhadores" in sent
        assert "CONCEITO" in sent
        assert "Nome do agent" in sent
        assert "OBRIGATÓRIO" in sent
        assert "Qual o default do campo Nome?" in sent

    def test_endpoint_provider_falha_retorna_503(self, monkeypatch):
        class BrokenProvider:
            async def generate(self, *a, **kw):
                raise RuntimeError("LLM offline")
        monkeypatch.setattr("app.routes.help.get_provider", lambda name: BrokenProvider())

        app = FastAPI()
        app.include_router(help_router)
        c = TestClient(app)
        r = c.post("/api/v1/help/ask-context-v2", json={
            "title": "X", "question": "?"
        })
        assert r.status_code == 503

    def test_endpoint_history_passado(self, monkeypatch):
        app, FakeProvider = self._app(monkeypatch)
        c = TestClient(app)
        r = c.post("/api/v1/help/ask-context-v2", json={
            "title": "X", "question": "Q3",
            "history": [
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "Q2"},
                {"role": "assistant", "content": "A2"},
            ],
        })
        assert r.status_code == 200


class TestRetrocompatLegacy:
    """O endpoint /ask-context (legacy) NÃO deve ter regressão por causa do V2."""

    def test_legacy_endpoint_ainda_existe(self, monkeypatch):
        class FakeProvider:
            async def generate(self, *a, **kw):
                return {"content": "ok legacy", "model": "fake", "usage": {}}
        monkeypatch.setattr("app.routes.help.get_provider", lambda name: FakeProvider())

        app = FastAPI()
        app.include_router(help_router)
        c = TestClient(app)
        r = c.post("/api/v1/help/ask-context", json={
            "title": "X", "section": "§Y",
            "what": "o que",
            "foundation": "fundamento",
            "usage": "usar",
            "question": "?",
        })
        assert r.status_code == 200
        assert r.json()["answer"] == "ok legacy"
