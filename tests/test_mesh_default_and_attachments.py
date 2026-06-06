"""Fix do dead-end de roteamento (2026-06-06, bug real "Doc Analise").

Cenário reportado: usuário soltou um .pptx com texto vago ("o que temos aqui").
O roteador decidiu CERTO ("encaminhando … análise de documentos"), mas o gate
condicional avaliava a expr só contra `input_lower` (o texto digitado) — que não
tinha keyword de documento — então TODOS os SAs eram pulados (1/3 executados,
dead-end). Duas causas-raiz gerais, cobertas aqui:

1) ANEXO INVISÍVEL AO GATE: `_build_conditional_context` agora expõe sinais do
   arquivo (`has_document`, `has_image`, `attachment_names/exts/types`,
   `text_all` = pergunta + nome/extensão). Exprs passam a rotear pelo ARQUIVO,
   não só pelo texto digitado.

2) SEM ELSE: nova aresta `default` (catch-all). `_should_skip_default` roda o
   alvo default SOMENTE quando NENHUM irmão condicional casou — reusando
   `_should_skip_conditional` (que já honra o override "o roteador mandou").
   Garante que a mensagem sempre cai num agente real (mata o dead-end).

Testes a nível de helper (igual test_mesh_conditional_routing.py): a lógica vive
em funções puras/assíncronas mockáveis; a integração no loop de execute_pipeline
depende de DB+LLM e é exercida no smoke manual / homolog.
"""
from __future__ import annotations

import json
import logging

import pytest


# ─── Classificação de anexo (helper isolado) ─────────────────────────


class TestAttachmentClassification:
    @pytest.mark.parametrize(
        "att,expected",
        [
            ({"name": "x.pptx", "type": ""}, "document"),
            ({"name": "x.pdf", "type": "application/pdf"}, "document"),
            (
                {
                    "name": "x.docx",
                    "type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                },
                "document",
            ),
            ({"name": "dados.csv", "type": "text/csv"}, "document"),
            ({"name": "notas.txt", "type": "text/plain"}, "document"),
            ({"name": "foto.PNG", "type": ""}, "image"),  # case-insensitive ext
            ({"name": "sem-ext", "type": "image/jpeg"}, "image"),  # só MIME
            ({"name": "x.zip", "type": "application/zip"}, "other"),
            ({"name": "", "type": ""}, "other"),
            (None, "other"),
        ],
    )
    def test_classify(self, att, expected):
        from app.agents.engine import _classify_attachment_kind
        assert _classify_attachment_kind(att) == expected

    def test_ext_helper(self):
        from app.agents.engine import _attachment_ext
        assert _attachment_ext("Relatório-2026.PDF") == "pdf"
        assert _attachment_ext("sem_extensao") == ""
        assert _attachment_ext(None) == ""


# ─── Sinais de anexo no contexto condicional ─────────────────────────


class TestAttachmentContext:
    def test_document_signal_and_text_all(self):
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(
            user_input="o que temos aqui",
            attachments=[{"name": "EncontroLideranca-TI.pptx", "type": ""}],
        )
        assert ctx["has_document"] is True
        assert ctx["has_image"] is False
        assert ctx["has_attachments"] is True
        assert ctx["attachment_count"] == 1
        assert "pptx" in ctx["attachment_exts"]
        # text_all reúne pergunta + nome do arquivo (em lowercase)
        assert "o que temos aqui" in ctx["text_all"]
        assert "encontrolideranca-ti.pptx" in ctx["text_all"]

    def test_image_signal(self):
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(attachments=[{"name": "foto.png", "type": "image/png"}])
        assert ctx["has_image"] is True
        assert ctx["has_document"] is False

    def test_no_attachments_defaults_false(self):
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(user_input="oi")
        assert ctx["has_attachments"] is False
        assert ctx["has_document"] is False
        assert ctx["has_image"] is False
        assert ctx["attachment_names"] == ""
        assert ctx["attachment_exts"] == ""
        assert ctx["text_all"] == "oi"

    def test_meta_lists_attachment_vars(self):
        """A UI do vars panel lê CONDITIONAL_VARS_META — sem drift com o runtime."""
        from app.agents.engine import CONDITIONAL_VARS_META
        names = {v["name"] for v in CONDITIONAL_VARS_META}
        for v in (
            "has_document",
            "has_image",
            "has_attachments",
            "attachment_names",
            "attachment_exts",
            "attachment_types",
            "attachment_count",
            "text_all",
        ):
            assert v in names, f"{v!r} ausente em CONDITIONAL_VARS_META"


# ─── Roteamento condicional pelo ANEXO (a regressão central) ─────────


class TestConditionalRoutingByAttachment:
    def _patch(self, monkeypatch, expr, target="docs"):
        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router",
                "target_agent_id": target,
                "connection_type": "conditional",
                "config": json.dumps({"expr": expr}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

    @pytest.mark.asyncio
    async def test_has_document_runs_on_pptx_drop(self, monkeypatch):
        """REGRESSÃO: pptx + texto vago → has_document=True → NÃO pula Documentos."""
        from app.agents import engine as eng
        self._patch(monkeypatch, "has_document")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="o que temos aqui",
            attachments=[{"name": "EncontroLideranca-TI.pptx", "type": ""}],
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_text_all_matches_filename(self, monkeypatch):
        """keyword casa no NOME do arquivo via text_all (input digitado vazio)."""
        from app.agents import engine as eng
        self._patch(monkeypatch, "'rentab' in text_all")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="",
            attachments=[{"name": "rentabilidade-2026.pdf", "type": "application/pdf"}],
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_image_expr_skips_when_document_dropped(self, monkeypatch):
        """Sem falso positivo: SA de imagem é pulado quando o drop é documento."""
        from app.agents import engine as eng
        self._patch(monkeypatch, "has_image", target="img")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="img",
            last_output="...", last_final_state="",
            user_input="", attachments=[{"name": "x.pdf", "type": "application/pdf"}],
        )
        assert out is True


# ─── Aresta default / "else" ─────────────────────────────────────────


class TestShouldSkipDefault:
    def _patch_conns(self, monkeypatch, conns, agent_names=None):
        agent_names = agent_names or {}

        async def fake_find_all(source_agent_id=None, **_):
            return [c for c in conns if c["source_agent_id"] == source_agent_id]

        async def fake_find_by_id(aid):
            return {"id": aid, "name": agent_names.get(aid, aid)}

        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)

    @pytest.mark.asyncio
    async def test_non_default_connection_never_skips(self, monkeypatch):
        from app.agents import engine as eng
        self._patch_conns(monkeypatch, [
            {"source_agent_id": "r", "target_agent_id": "d", "connection_type": "conditional", "config": "{}"},
        ])
        out = await eng._should_skip_default(source_id="r", target_id="d", last_output="x", last_final_state="")
        assert out is False  # não é default → este gate não opina

    @pytest.mark.asyncio
    async def test_default_without_conditional_siblings_runs(self, monkeypatch):
        """default sem irmão condicional = roda sempre (equivale a sequencial)."""
        from app.agents import engine as eng
        self._patch_conns(monkeypatch, [
            {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
            {"source_agent_id": "r", "target_agent_id": "seq", "connection_type": "sequential", "config": "{}"},
        ])
        out = await eng._should_skip_default(source_id="r", target_id="else", last_output="x", last_final_state="")
        assert out is False

    @pytest.mark.asyncio
    async def test_default_skips_when_sibling_matches(self, monkeypatch):
        """Um irmão condicional casou (has_document via pptx) → pula o default."""
        from app.agents import engine as eng
        self._patch_conns(monkeypatch, [
            {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
            {"source_agent_id": "r", "target_agent_id": "docs", "connection_type": "conditional", "config": json.dumps({"expr": "has_document"})},
        ], agent_names={"docs": "Documentos", "else": "Fallback"})
        out = await eng._should_skip_default(
            source_id="r", target_id="else", last_output="x", last_final_state="",
            attachments=[{"name": "a.pdf", "type": "application/pdf"}],
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_default_runs_when_no_sibling_matches(self, monkeypatch):
        """Nenhum irmão casou (nem doc nem imagem) → o else RODA (mata dead-end)."""
        from app.agents import engine as eng
        self._patch_conns(monkeypatch, [
            {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
            {"source_agent_id": "r", "target_agent_id": "docs", "connection_type": "conditional", "config": json.dumps({"expr": "has_document"})},
            {"source_agent_id": "r", "target_agent_id": "img", "connection_type": "conditional", "config": json.dumps({"expr": "has_image"})},
        ], agent_names={"docs": "Documentos", "img": "Imagem", "else": "Fallback"})
        out = await eng._should_skip_default(
            source_id="r", target_id="else", last_output="x", last_final_state="",
            user_input="como cozinhar arroz",  # fora de escopo, sem anexo
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_default_honors_router_named_sibling(self, monkeypatch):
        """O override 'o roteador mandou' propaga: se o roteador NOMEIA um irmão
        no output, esse irmão 'casa' mesmo com expr false → pula o default."""
        from app.agents import engine as eng
        self._patch_conns(monkeypatch, [
            {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
            {"source_agent_id": "r", "target_agent_id": "docs", "connection_type": "conditional", "config": json.dumps({"expr": "'nada' in input_lower"})},
        ], agent_names={"docs": "Documentos", "else": "Fallback"})
        out = await eng._should_skip_default(
            source_id="r", target_id="else",
            last_output="Encaminhando ao agente Documentos.", last_final_state="",
            user_input="qualquer coisa",
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_logs_decision(self, monkeypatch, caplog):
        from app.agents import engine as eng
        self._patch_conns(monkeypatch, [
            {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
            {"source_agent_id": "r", "target_agent_id": "docs", "connection_type": "conditional", "config": json.dumps({"expr": "has_document"})},
        ], agent_names={"docs": "Documentos", "else": "Fallback"})
        with caplog.at_level(logging.INFO, logger="app.agents.engine"):
            await eng._should_skip_default(
                source_id="r", target_id="else", last_output="x", last_final_state="",
                user_input="oi sem anexo",
            )
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.default" in events
