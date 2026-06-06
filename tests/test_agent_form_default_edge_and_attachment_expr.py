"""Smoke do Composer (agent_form.html) — lado FRONTEND do fix geral do dead-end
de roteamento (2026-06-06, bug real "Doc Analise").

Dois contratos novos, complementares ao motor (provado em
tests/test_mesh_default_and_attachments.py):

1) EXPR CIENTE DE ANEXO (#68): `_deriveConditionalExpr` passou a casar contra
   `text_all` (pergunta + NOME/EXTENSÃO do arquivo) em vez de só `input_lower`,
   e liga `has_document`/`has_image` quando o "quando"/nome do alvo sinaliza
   documento/imagem. Assim "o que temos aqui" + EncontroLideranca-TI.pptx ainda
   roteia pro SA de documentos (antes: nenhuma keyword no texto digitado → ramo
   pulado → dead-end).

2) AGENTE-PADRÃO / ELSE (#64): um picker opcional que escolhe UM agente real
   como catch-all. Vira aresta `connection_type='default'` no AI Mesh — roda só
   quando NENHUM ramo condicional casa (rede de segurança que mata o dead-end).

Como Alpine/JS só roda no browser, travamos contratos ESTRUTURAIS no HTML
(mesmo padrão de test_agent_form_mission_mesh_sync.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest


_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "app" / "templates" / "pages" / "agent_form.html"
)


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


# ─── #68 — derivação de expr ciente de anexo ─────────────────────────


class TestAttachmentAwareExpr:
    def test_intent_helper_defined(self, html):
        assert "_attachmentIntent(text) {" in html

    def test_intent_is_accent_insensitive(self, html):
        # NFKD + remove diacríticos (espelha _output_names_target/engine)
        assert "normalize('NFKD')" in html

    def test_intent_doc_keywords(self, html):
        # radicais de documento — cobrem pptx/docx/xlsx/pdf/planilha que o
        # usuário cita no "quando" (lista incompleta era causa-raiz do bug).
        for kw in ("'pdf'", "'pptx'", "'docx'", "'xlsx'", "'planilha'", "'document'", "'csv'"):
            assert kw in html, f"keyword de documento {kw} ausente em _attachmentIntent"

    def test_intent_img_keywords(self, html):
        for kw in ("'imagem'", "'foto'", "'png'", "'jpg'", "'jpeg'"):
            assert kw in html, f"keyword de imagem {kw} ausente em _attachmentIntent"

    def test_expr_matches_text_all(self, html):
        # keywords casam contra text_all (pergunta + nome/ext do anexo)
        assert "`'${t}' in text_all`" in html

    def test_expr_ors_in_document_signal(self, html):
        assert "if (intent.doc) parts.push('has_document');" in html

    def test_expr_ors_in_image_signal(self, html):
        assert "if (intent.img) parts.push('has_image');" in html

    def test_intent_seeded_from_when_and_target(self, html):
        # o sinal vem do "quando" E do nome do alvo (ex.: agente "Documentos")
        assert "this._attachmentIntent(w + ' ' + (targetName || ''))" in html

    def test_empty_when_still_sequential(self, html):
        # contrato preservado: sem "quando" → '' → aresta sequential
        assert "if (!w) return '';" in html

    def test_no_parts_returns_empty(self, html):
        # sem keyword E sem intenção de anexo → '' (não vira conditional)
        assert "if (!parts.length) return '';" in html

    def test_target_name_seeded_first(self, html):
        # ordem preservada da PR anterior (nome do alvo antes do "quando")
        assert "[...this._kwTokens(targetName), ...this._kwTokens(w)]" in html


# ─── #64 — agente-padrão (aresta default / else) ─────────────────────


class TestDefaultAgentState:
    def test_default_agent_in_mission_state(self, html):
        # estado inicial do x-data
        assert "defaultAgent: ''," in html

    def test_default_agent_reset_in_open_composer(self, html):
        # openComposer reabre limpo (inclui defaultAgent)
        assert "fallbackMode: 'graceful', defaultAgent: '', goldenRule: true" in html

    def test_default_agent_hydrated_from_ai_draft(self, html):
        # compose IA pode sugerir um catch-all (defaultAgent/default_agent)
        assert "defaultAgent: (d.defaultAgent || d.default_agent || '')," in html


class TestDefaultAgentGetters:
    @pytest.mark.parametrize(
        "getter",
        [
            "get missionDefaultTarget() {",
            "get missionSyncTargets() {",
            "get missionDefaultDrift() {",
        ],
    )
    def test_getter_defined(self, html, getter):
        assert getter in html, f"getter {getter!r} ausente"

    def test_default_target_resolves_real_agent(self, html):
        # resolve via resolveAgentByName (texto livre não vira aresta) →
        # connection_type 'default', sem expr.
        assert "const a = this.resolveAgentByName(this.mission.defaultAgent || '');" in html
        assert "return { id: a.id, name: a.name, connection_type: 'default', expr: '' };" in html

    def test_sync_targets_concat_and_dedup(self, html):
        # regras + default, sem duplicar um alvo que já é regra
        assert "const out = (this.missionAgentTargets || []).slice();" in html
        assert "if (def && !out.some(t => t.id === def.id)) out.push(def);" in html

    def test_default_drift_detects_overlap_with_rule(self, html):
        # default coincide com alvo de regra → redundante (avisa, não bloqueia)
        assert "return (this.missionAgentTargets || []).some(t => t.id === def.id);" in html


class TestDefaultAgentSyncWiring:
    def test_create_handles_default_type(self, html):
        # default conta separado e usa config '{}' (não é conditional)
        assert "else if (connection_type === 'default') dflt++;" in html

    def test_summary_reports_default_count(self, html):
        assert "if (r.dflt) parts.push(r.dflt + ' default/else');" in html

    def test_sync_uses_combined_targets(self, html):
        # syncMissionToMesh passou a usar missionSyncTargets (regras + else)
        assert "const targets = this.missionSyncTargets;" in html

    def test_guard_message_mentions_default(self, html):
        assert "Nenhuma regra (nem agente-padrão) aponta para um agente real" in html

    def test_mesh_connected_considers_default(self, html):
        # o selo "conectado ao mesh" (agente novo) conta o else também
        assert "|| !!this.missionDefaultTarget;" in html


class TestDefaultAgentUI:
    def test_picker_label_present(self, html):
        assert "Agente-padrão (else)" in html

    def test_picker_input_bound_and_uses_datalist(self, html):
        assert 'x-model="mission.defaultAgent"' in html
        # reusa o MESMO datalist das regras (só agentes reais)
        assert html.count('list="composer-targets"') >= 2

    def test_panel_shows_default_row(self, html):
        # linha extra na "Verificação de roteamento" pro else
        assert 'x-text="missionDefaultTarget.name"' in html
        assert "default / else" in html

    def test_unresolved_default_warns(self, html):
        # nome digitado que não resolve pra agente real → aviso (não cria aresta)
        assert 'x-if="(mission.defaultAgent || \'\').trim() && !missionDefaultTarget"' in html

    def test_default_drift_warning_rendered(self, html):
        assert 'x-if="missionDefaultDrift"' in html

    def test_default_badge_uses_red_palette_no_purple(self, html):
        # paleta vermelho/branco do projeto: badge default = rose (família vermelho)
        assert "bg-rose-100 text-rose-700" in html
        for forbidden in ("bg-violet", "bg-fuchsia", "bg-purple", "text-violet", "text-fuchsia", "text-purple"):
            assert forbidden not in html, f"tom de roxo proibido: {forbidden}"
