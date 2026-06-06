"""Fix do drop de anexo na PORTA do pipeline (2026-06-06, bug real "Doc Analise").

Cenário reportado (persistia após o PR B): usuário soltou um .pptx num ROTEADOR
dispatcher ("Doc Analise"). O roteador é dispatcher — não ingere, só roteia — e
no DB tem accepts_documents=0. O filtro de porta (`_filter_attachments_by_agent`
no route layer) decidia SÓ pelas flags do agente de ENTRADA → podava o documento
ANTES de chegar ao pipeline. Sintoma: execute_pipeline recebia [], o roteador
rodava sem anexo (has_attachment:false, grounding.refused), o gate condicional não
via has_document e AMBOS os SAs (Documentos/Imagem) eram skipped_conditional →
dead-end / 0 evidências / Refuse.

Correção (este arquivo cobre):

1) UNIÃO DA CADEIA NA PORTA: `_chain_capabilities` une accepts_images/
   accepts_documents do agente de entrada + TODOS os agentes downstream do mesh
   (BFS, reusa `_resolve_ordered_chain`). `_filter_attachments_by_agent` ganha
   `include_chain` (default False = legado). Em pipeline, a porta deixa o anexo
   passar sse QUALQUER agente da cadeia aceita o tipo. O motor (forwarding por
   step, PR B) já entrega cada anexo só ao SA com capacidade → sem vazamento.

2) OBSERVABILIDADE: anexo podado nunca mais some em silêncio — log estruturado
   `event=workspace.attachment.rejected` + (no SSE) evento `attachments_rejected`
   pro trace, pra o usuário VER por que o arquivo não foi usado.

Testes a nível de helper (igual aos demais test_mesh_*): a lógica vive em funções
puras/assíncronas mockáveis; a integração no chat_stream depende de DB+LLM+SSE e é
exercida no smoke manual / homolog. Wiring dos call-sites é coberto por
source-grep (TestDoorWiring).
"""
from __future__ import annotations

import logging
import pathlib

import pytest


# MIMEs reais usados no bug
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF = "application/pdf"
PNG = "image/png"


def _agent(aid, *, img=0, doc=0, kind="subagent", name=None):
    return {"id": aid, "name": name or aid, "kind": kind,
            "accepts_images": img, "accepts_documents": doc}


def _patch_agents(monkeypatch, by_id: dict):
    async def fake_find_by_id(aid):
        return by_id.get(aid)
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)


def _patch_chain(monkeypatch, chain: list):
    async def fake_chain(entry_id):
        return chain
    monkeypatch.setattr("app.agents.engine._resolve_ordered_chain", fake_chain)


# ─── União de capacidades da cadeia (helper isolado) ─────────────────


class TestChainCapabilities:
    @pytest.mark.asyncio
    async def test_dispatcher_unions_both_downstream(self, monkeypatch):
        """Router(0,0) → [router, docs(doc=1), img(img=1)] → (True, True)."""
        from app.routes.workspace import _chain_capabilities
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "docs": _agent("docs", doc=1),
            "img": _agent("img", img=1),
        })
        _patch_chain(monkeypatch, ["router", "docs", "img"])
        assert await _chain_capabilities("router") == (True, True)

    @pytest.mark.asyncio
    async def test_dispatcher_doc_only(self, monkeypatch):
        """Só especialista de documento downstream → (img=False, doc=True)."""
        from app.routes.workspace import _chain_capabilities
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "docs": _agent("docs", doc=1),
        })
        _patch_chain(monkeypatch, ["router", "docs"])
        assert await _chain_capabilities("router") == (False, True)

    @pytest.mark.asyncio
    async def test_leaf_agent_uses_own_flags(self, monkeypatch):
        """Agente-folha (sem downstream) → união = as próprias flags."""
        from app.routes.workspace import _chain_capabilities
        _patch_agents(monkeypatch, {"solo": _agent("solo", doc=1)})
        _patch_chain(monkeypatch, ["solo"])
        assert await _chain_capabilities("solo") == (False, True)

    @pytest.mark.asyncio
    async def test_entry_accepts_both_shortcircuits(self, monkeypatch):
        """Entrada já aceita ambos → retorna (True,True) SEM traversar o mesh."""
        from app.routes.workspace import _chain_capabilities
        _patch_agents(monkeypatch, {"e": _agent("e", img=1, doc=1)})

        async def _boom(_):
            raise AssertionError("não deveria traversar quando entrada cobre ambos")
        monkeypatch.setattr("app.agents.engine._resolve_ordered_chain", _boom)
        assert await _chain_capabilities("e") == (True, True)

    @pytest.mark.asyncio
    async def test_traversal_failure_failopen_to_entry(self, monkeypatch):
        """Erro no traversal → fail-open pras flags do agente de entrada."""
        from app.routes.workspace import _chain_capabilities
        _patch_agents(monkeypatch, {"e": _agent("e", doc=1)})

        async def _boom(_):
            raise RuntimeError("mesh_repo down")
        monkeypatch.setattr("app.agents.engine._resolve_ordered_chain", _boom)
        assert await _chain_capabilities("e") == (False, True)

    @pytest.mark.asyncio
    async def test_entry_passed_in_avoids_refetch(self, monkeypatch):
        """`entry=` já buscado → não chama find_by_id pro próprio entry."""
        from app.routes.workspace import _chain_capabilities

        seen = []

        async def fake_find_by_id(aid):
            seen.append(aid)
            return {"docs": _agent("docs", doc=1)}.get(aid)
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)
        _patch_chain(monkeypatch, ["router", "docs"])
        out = await _chain_capabilities("router", entry=_agent("router", kind="router"))
        assert out == (False, True)
        assert "router" not in seen  # entry veio pronto; só busca downstream


# ─── Filtro de porta com união da cadeia (a regressão central) ───────


class TestDoorFilterChainUnion:
    @pytest.mark.asyncio
    async def test_pptx_survives_dispatcher_door(self, monkeypatch):
        """REGRESSÃO: pptx no router dispatcher(0,0) NÃO é podado porque o
        especialista downstream Documentos aceita doc."""
        from app.routes.workspace import _filter_attachments_by_agent
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "docs": _agent("docs", doc=1),
            "img": _agent("img", img=1),
        })
        _patch_chain(monkeypatch, ["router", "docs", "img"])
        accepted, rejected = await _filter_attachments_by_agent(
            [{"name": "Plano.pptx", "type": PPTX}], "router", include_chain=True,
        )
        assert len(accepted) == 1 and accepted[0]["name"] == "Plano.pptx"
        assert rejected == []

    @pytest.mark.asyncio
    async def test_pptx_rejected_when_no_chain_accepts(self, monkeypatch):
        """Nenhum agente da cadeia aceita doc → poda (correta) + motivo de pipeline."""
        from app.routes.workspace import _filter_attachments_by_agent
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "img": _agent("img", img=1),  # só imagem na cadeia
        })
        _patch_chain(monkeypatch, ["router", "img"])
        accepted, rejected = await _filter_attachments_by_agent(
            [{"name": "Plano.pptx", "type": PPTX}], "router", include_chain=True,
        )
        assert accepted == []
        assert len(rejected) == 1
        assert rejected[0]["kind"] == "document"
        assert "pipeline" in rejected[0]["reason"].lower()

    @pytest.mark.asyncio
    async def test_legacy_mode_keeps_entry_only(self, monkeypatch):
        """include_chain=False (legado): decide só pela entrada. Router(0,0) poda
        o pptx mesmo com Documentos downstream — comportamento antigo intacto."""
        from app.routes.workspace import _filter_attachments_by_agent
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "docs": _agent("docs", doc=1),
        })

        async def _boom(_):
            raise AssertionError("legado não deve traversar a cadeia")
        monkeypatch.setattr("app.agents.engine._resolve_ordered_chain", _boom)
        accepted, rejected = await _filter_attachments_by_agent(
            [{"name": "Plano.pptx", "type": PPTX}], "router",  # include_chain default False
        )
        assert accepted == []
        assert len(rejected) == 1
        # motivo legado fala do "Agente", não do "pipeline"
        assert "pipeline" not in rejected[0]["reason"].lower()

    @pytest.mark.asyncio
    async def test_image_routed_when_chain_has_image_acceptor(self, monkeypatch):
        """png passa porque Imagem(img=1) está na cadeia; pptx coexistindo passa
        por Documentos(doc=1). Os dois sobrevivem à porta do dispatcher."""
        from app.routes.workspace import _filter_attachments_by_agent
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "docs": _agent("docs", doc=1),
            "img": _agent("img", img=1),
        })
        _patch_chain(monkeypatch, ["router", "docs", "img"])
        accepted, rejected = await _filter_attachments_by_agent(
            [{"name": "foto.png", "type": PNG}, {"name": "x.pdf", "type": PDF}],
            "router", include_chain=True,
        )
        names = {a["name"] for a in accepted}
        assert names == {"foto.png", "x.pdf"}
        assert rejected == []

    @pytest.mark.asyncio
    async def test_unknown_entry_failopen_passes_all(self, monkeypatch):
        """Agente de entrada inexistente → fail-open: não poda nada."""
        from app.routes.workspace import _filter_attachments_by_agent
        _patch_agents(monkeypatch, {})  # find_by_id → None
        accepted, rejected = await _filter_attachments_by_agent(
            [{"name": "x.pdf", "type": PDF}], "ghost", include_chain=True,
        )
        assert len(accepted) == 1 and rejected == []

    @pytest.mark.asyncio
    async def test_empty_attachments_noop(self, monkeypatch):
        from app.routes.workspace import _filter_attachments_by_agent
        assert await _filter_attachments_by_agent([], "router", include_chain=True) == ([], [])

    @pytest.mark.asyncio
    async def test_rejection_is_logged(self, monkeypatch, caplog):
        """Anexo podado emite log estruturado (a observabilidade que faltava)."""
        from app.routes.workspace import _filter_attachments_by_agent
        _patch_agents(monkeypatch, {
            "router": _agent("router", kind="router"),
            "img": _agent("img", img=1),
        })
        _patch_chain(monkeypatch, ["router", "img"])
        with caplog.at_level(logging.INFO, logger="app.routes.workspace"):
            await _filter_attachments_by_agent(
                [{"name": "Plano.pptx", "type": PPTX}], "router", include_chain=True,
            )
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "workspace.attachment.rejected" in events


# ─── Wiring dos call-sites (source-grep, sem DB/LLM/SSE) ─────────────


class TestDoorWiring:
    def _ws_src(self) -> str:
        import app.routes.workspace as ws
        return pathlib.Path(ws.__file__).read_text(encoding="utf-8")

    def test_chat_stream_passes_include_chain_true(self):
        src = self._ws_src()
        assert "include_chain=True" in src, "chat_stream (SSE) deve unir a cadeia"

    def test_nonstream_chat_gates_include_chain_on_pipeline(self):
        src = self._ws_src()
        assert 'include_chain=(data.mode == "pipeline")' in src

    def test_sse_emits_attachments_rejected_event(self):
        src = self._ws_src()
        assert "attachments_rejected" in src

    def test_frontend_handles_attachments_rejected(self):
        import app
        tpl = (pathlib.Path(app.__file__).resolve().parent
               / "templates" / "pages" / "workspace.html")
        html = tpl.read_text(encoding="utf-8")
        assert "attachments_rejected" in html, "workspace.html deve tratar o evento"

    def test_filter_signature_has_include_chain(self):
        import inspect
        from app.routes.workspace import _filter_attachments_by_agent
        sig = inspect.signature(_filter_attachments_by_agent)
        assert "include_chain" in sig.parameters
        assert sig.parameters["include_chain"].default is False  # legado por padrão
