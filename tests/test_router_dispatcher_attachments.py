"""Router como dispatcher + roteamento ciente de anexo (2026-06-06, PR B).

Sequência do bug "Doc Analise" (continuação do #299): mesmo depois de o gate
condicional virar ciente de anexo (has_document/has_image), DUAS lacunas restavam
e são cobertas aqui:

1) GATE DEPENDENTE DE EXPR: o gate só roteava pelo arquivo se a EXPR do operador
   referenciasse has_document/has_image. Exprs keyword-only (antigas ou editadas à
   mão) continuavam pulando um especialista quando caía o documento. PR B adiciona
   um override por CAPACIDADE (`_target_handles_attachment`): um SA que DECLARA
   accepts_documents/accepts_images NUNCA é pulado quando chega o tipo declarado —
   irmão do override "o roteador mandou" (_output_names_target). Opt-in (default 0).

2) ESPECIALISTA CEGO AO ARQUIVO: no pipeline multi-agente só o entry (i==0) recebia
   os anexos; todo SA downstream recebia None → via apenas o TEXTO do upstream.
   `_filter_attachments_by_agent` despacha a cada SA downstream só o subconjunto que
   ele aceita — o handler de documentos finalmente enxerga o arquivo bruto.

Testes a nível de helper (igual test_mesh_default_and_attachments.py): a lógica
vive em funções puras/assíncronas mockáveis; a integração no loop de
execute_pipeline depende de DB+LLM e é exercida no smoke manual / homolog.
"""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

_ENGINE_SRC = Path(__file__).resolve().parents[1] / "app" / "agents" / "engine.py"


# ─── _filter_attachments_by_agent (dispatcher / forwarding) ──────────


class TestFilterAttachmentsByAgent:
    DOC = {"name": "rel.pdf", "type": "application/pdf"}
    IMG = {"name": "foto.png", "type": "image/png"}
    OTHER = {"name": "pacote.zip", "type": "application/zip"}

    def _f(self, agent, atts):
        from app.agents.engine import _filter_attachments_by_agent
        return _filter_attachments_by_agent(agent, atts)

    def test_accepts_documents_passes_doc(self):
        assert self._f({"accepts_documents": 1}, [self.DOC]) == [self.DOC]

    def test_accepts_documents_drops_image(self):
        # SA de documentos não recebe imagem (não sabe tratar) → None.
        assert self._f({"accepts_documents": 1}, [self.IMG]) is None

    def test_accepts_images_passes_image(self):
        assert self._f({"accepts_images": 1}, [self.IMG]) == [self.IMG]

    def test_accepts_images_drops_doc(self):
        assert self._f({"accepts_images": 1}, [self.DOC]) is None

    def test_both_capabilities_pass_both(self):
        out = self._f({"accepts_documents": 1, "accepts_images": 1}, [self.DOC, self.IMG])
        assert out == [self.DOC, self.IMG]

    def test_opt_in_neither_returns_none(self):
        # Defaults 0 → forwarding é opt-in: SA sem capacidade não recebe nada.
        assert self._f({"accepts_documents": 0, "accepts_images": 0}, [self.DOC]) is None
        assert self._f({}, [self.DOC]) is None

    def test_other_counts_as_document(self):
        # 'other' (markitdown não classificou) segue p/ handler de documentos.
        assert self._f({"accepts_documents": 1}, [self.OTHER]) == [self.OTHER]

    def test_mixed_filters_to_accepted_only(self):
        # accepts_documents só → recebe doc, NÃO recebe imagem.
        assert self._f({"accepts_documents": 1}, [self.DOC, self.IMG]) == [self.DOC]

    def test_empty_and_none_return_none(self):
        assert self._f({"accepts_documents": 1}, []) is None
        assert self._f({"accepts_documents": 1}, None) is None

    def test_returns_none_not_empty_list_when_nothing_passes(self):
        # Contrato: None (não []) p/ casar a assinatura de execute_interaction
        # e o caminho legado (sem anexos → sem attachment_context).
        assert self._f({"accepts_images": 1}, [self.DOC]) is None


# ─── _target_handles_attachment (override de capability no gate) ─────


class TestTargetHandlesAttachment:
    DOC = {"name": "rel.pdf", "type": "application/pdf"}
    IMG = {"name": "foto.png", "type": "image/png"}
    OTHER = {"name": "pacote.zip", "type": "application/zip"}

    def _h(self, *, doc=False, img=False, atts):
        from app.agents.engine import _target_handles_attachment
        return _target_handles_attachment(
            accepts_documents=doc, accepts_images=img, attachments=atts
        )

    @pytest.mark.parametrize(
        "doc,img,atts,expected",
        [
            (True, False, [DOC], True),     # handler de doc + doc → roda
            (True, False, [IMG], False),    # handler de doc + imagem → não
            (False, True, [IMG], True),     # handler de img + imagem → roda
            (False, True, [DOC], False),    # handler de img + doc → não
            (True, True, [DOC], True),      # ambos + doc → roda
            (False, False, [DOC], False),   # sem capacidade → não dispara (opt-in)
            (True, False, [OTHER], True),   # 'other' conta como documento
            (True, True, [], False),        # sem anexo → não dispara
            (True, True, None, False),      # None → não dispara
        ],
    )
    def test_truth_table(self, doc, img, atts, expected):
        assert self._h(doc=doc, img=img, atts=atts) is expected


# ─── Override de capability dentro de _should_skip_conditional ───────


class TestConditionalCapabilityOverride:
    DOC = {"name": "x.pdf", "type": "application/pdf"}
    IMG = {"name": "x.png", "type": "image/png"}

    def _patch(self, monkeypatch, expr, target="docs", ctype="conditional"):
        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router",
                "target_agent_id": target,
                "connection_type": ctype,
                "config": json.dumps({"expr": expr}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

    @pytest.mark.asyncio
    async def test_capability_override_runs_despite_false_expr(self, monkeypatch):
        """REGRESSÃO-RAIZ: expr keyword-only (false) MAS o SA declara
        accepts_documents e caiu um documento → NÃO pula (override de capability)."""
        from app.agents import engine as eng
        self._patch(monkeypatch, "'rentab' in input_lower")  # não casa
        out = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="o que temos aqui",  # sem keyword
            attachments=[self.DOC],
            target_accepts_documents=True,
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_no_capability_still_skips_on_false_expr(self, monkeypatch):
        """Sem a capacidade declarada (default), comportamento legado: expr false
        com documento → pula. O override é opt-in."""
        from app.agents import engine as eng
        self._patch(monkeypatch, "has_image")  # false p/ documento
        out = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="", attachments=[self.DOC],
            target_accepts_documents=False, target_accepts_images=False,
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_image_capability_override(self, monkeypatch):
        from app.agents import engine as eng
        self._patch(monkeypatch, "'nada' in input_lower", target="img")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="img",
            last_output="...", last_final_state="",
            user_input="qualquer", attachments=[self.IMG],
            target_accepts_images=True,
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_capability_does_not_fire_without_attachment(self, monkeypatch):
        """Sem anexo, o override não opina → a expr governa (false → pula)."""
        from app.agents import engine as eng
        self._patch(monkeypatch, "has_document")  # false sem anexo
        out = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="oi", attachments=None,
            target_accepts_documents=True,
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_capability_override_logs_event(self, monkeypatch, caplog):
        from app.agents import engine as eng
        self._patch(monkeypatch, "'zzz' in input_lower")
        with caplog.at_level(logging.INFO, logger="app.agents.engine"):
            await eng._should_skip_conditional(
                source_id="router", target_id="docs",
                last_output="...", last_final_state="",
                user_input="sem keyword", attachments=[self.DOC],
                target_accepts_documents=True,
            )
        reasons = [getattr(r, "reason", None) for r in caplog.records]
        assert "capability" in reasons


# ─── Default/else reusa o override de capability dos irmãos ──────────


class TestDefaultGateCapability:
    DOC = {"name": "a.pdf", "type": "application/pdf"}

    def _patch_conns(self, monkeypatch, conns, agents=None):
        agents = agents or {}

        async def fake_find_all(source_agent_id=None, **_):
            return [c for c in conns if c["source_agent_id"] == source_agent_id]

        async def fake_find_by_id(aid):
            return agents.get(aid, {"id": aid, "name": aid})

        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)

    @pytest.mark.asyncio
    async def test_doc_handler_sibling_matches_via_capability_skips_default(self, monkeypatch):
        """Irmão condicional é handler de doc (accepts_documents=1) com expr que NÃO
        casa por keyword; com documento presente, o override de capability faz o
        irmão 'casar' → o default (else) NÃO roda."""
        from app.agents import engine as eng
        self._patch_conns(
            monkeypatch,
            [
                {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
                {"source_agent_id": "r", "target_agent_id": "docs", "connection_type": "conditional", "config": json.dumps({"expr": "has_image"})},
            ],
            agents={
                "docs": {"id": "docs", "name": "Documentos", "accepts_documents": 1},
                "else": {"id": "else", "name": "Fallback"},
            },
        )
        out = await eng._should_skip_default(
            source_id="r", target_id="else",
            last_output="x", last_final_state="",
            attachments=[self.DOC],
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_without_capability_default_runs(self, monkeypatch):
        """Mesmo cenário, mas o irmão NÃO declara accepts_documents → não casa
        (expr has_image false) → nenhum irmão casou → o default (else) RODA."""
        from app.agents import engine as eng
        self._patch_conns(
            monkeypatch,
            [
                {"source_agent_id": "r", "target_agent_id": "else", "connection_type": "default", "config": "{}"},
                {"source_agent_id": "r", "target_agent_id": "docs", "connection_type": "conditional", "config": json.dumps({"expr": "has_image"})},
            ],
            agents={
                "docs": {"id": "docs", "name": "Documentos", "accepts_documents": 0},
                "else": {"id": "else", "name": "Fallback"},
            },
        )
        out = await eng._should_skip_default(
            source_id="r", target_id="else",
            last_output="x", last_final_state="",
            attachments=[self.DOC],
        )
        assert out is False


# ─── Wiring no execute_pipeline (source-smoke, sem rodar o motor) ────


class TestDispatchWiring:
    def _src(self) -> str:
        return _ENGINE_SRC.read_text(encoding="utf-8")

    def test_signature_has_capability_params(self):
        from app.agents.engine import _should_skip_conditional
        sig = inspect.signature(_should_skip_conditional)
        assert "target_accepts_documents" in sig.parameters
        assert "target_accepts_images" in sig.parameters
        assert sig.parameters["target_accepts_documents"].default is False
        assert sig.parameters["target_accepts_images"].default is False

    def test_forwarding_replaces_none_for_downstream(self):
        src = self._src()
        # O forwarding por capability substituiu o antigo 'else None'.
        assert "_filter_attachments_by_agent(agent, attachments)" in src
        assert "attachments=_forwarded_atts," in src
        assert "attachments if i == 0 else None" not in src

    def test_entry_still_receives_all_attachments(self):
        # Entry (i==0) inalterado: recebe TODOS os anexos.
        assert "attachments if i == 0 else _filter_attachments_by_agent" in self._src()

    def test_capability_override_wired_into_gate_call(self):
        src = self._src()
        assert "target_accepts_documents=bool(agent.get(\"accepts_documents\") or 0)" in src
        assert "target_accepts_images=bool(agent.get(\"accepts_images\") or 0)" in src

    def test_observability_present(self):
        src = self._src()
        assert "mesh.dispatch.attachments_forwarded" in src
        assert "mesh.conditional.target_handles_attachment" in src
        assert '"type": "attachments_dispatched"' in src
        assert '"dispatched_attachments": _fwd_names' in src

    def test_helpers_defined(self):
        from app.agents import engine as eng
        assert callable(eng._filter_attachments_by_agent)
        assert callable(eng._target_handles_attachment)
