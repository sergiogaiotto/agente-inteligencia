"""Smoke do template agent_form.html — "Mentor de Agentes" (MVP).

MELHORIA UI: para cada CAMADA escolhida, especializar e tornar o guia
interativo — um mentor na própria página que ajuda qualquer iniciante a
criar seu super agente. O MVP tem três peças que se costuram:

1. CARDS DE CAMADA — substituem o <select> de Tipo por 3 cards ricos
   (🎯 Especialista / 🧭 Triagem / 🎼 Maestro): metáfora + quando usar +
   exemplo dos DADOS do próprio usuário (availableAgents). Clicar seta
   form.kind e re-contextualiza o Mentor.

2. PAINEL MENTOR (rail lateral, recolhível) — "Você está criando um …"
   dinâmico por camada, intro em 2 frases, e um Tradutor de jargão (RAG,
   pass-through, task_type, AI Mesh) em linguagem simples.

3. CHECKLIST "PRONTIDÃO" viva e ACIONÁVEL — medidor por camada computado
   do estado real (system_prompt, isPassthrough, skill/RAG, mesh) onde
   cada item pendente é um atalho que pula pro campo / abre a ferramenta
   certa (Estrutura/Compor missão/Sincronizar Mesh), costurando PR1-4.

Alpine.js só roda no browser; como nos demais smokes, travamos contratos
ESTRUTURAIS no HTML: se um refactor quebrar um ponto, a feature deixa de
funcionar silenciosamente e o teste pega.
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


# ────────────────────────────────────────────────────────────────
# Layout: rail lateral persistente
# ────────────────────────────────────────────────────────────────
class TestMentorLayout:
    def test_two_column_wrapper(self, html):
        """Wizard vira coluna esquerda; Mentor é o rail direito."""
        assert "lg:flex lg:items-start lg:gap-6" in html
        assert "flex-1 min-w-0 lg:max-w-2xl" in html

    def test_mentor_is_sticky_aside(self, html):
        assert "<aside" in html
        assert "lg:sticky lg:top-6" in html

    def test_mentor_lives_outside_the_steps(self, html):
        """O rail é persistente — vem DEPOIS do último passo (não dentro dele)."""
        assert html.find("<aside") > html.find('x-show="step === 3"') > 0


class TestMentorState:
    def test_mentor_open_default_true(self, html):
        assert "mentorOpen: true," in html

    def test_panel_is_collapsible(self, html):
        assert '@click="mentorOpen = !mentorOpen"' in html
        assert 'x-show="mentorOpen"' in html


# ────────────────────────────────────────────────────────────────
# Cards de camada (substituem o <select>)
# ────────────────────────────────────────────────────────────────
class TestLayerCardsReplaceSelect:
    def test_old_kind_select_is_gone(self, html):
        """O <select x-model="form.kind"> foi substituído pelos cards."""
        assert 'x-model="form.kind"' not in html
        assert "Subagente (SA)" not in html
        assert "Orquestrador (AOBD)" not in html

    def test_cards_iterate_layer_cards(self, html):
        assert 'x-for="card in layerCards"' in html

    def test_clicking_card_sets_kind(self, html):
        assert '@click="form.kind = card.kind"' in html

    def test_card_renders_all_fields(self, html):
        for binding in (
            'x-text="card.icon"', 'x-text="card.title"', 'x-text="card.tech"',
            'x-text="card.metaphor"', 'x-text="card.when"', 'x-text="card.example"',
        ):
            assert binding in html, f"card sem binding {binding!r}"

    def test_selected_card_has_ring(self, html):
        assert "border-brand-500 bg-brand-50/60 ring-2 ring-brand-100" in html

    def test_cards_are_in_basico_step(self, html):
        idx_step0 = html.find('x-show="step === 0"')
        idx_step1 = html.find('x-show="step === 1"')
        idx_cards = html.find('x-for="card in layerCards"')
        assert 0 < idx_step0 < idx_cards < idx_step1


class TestLayerMetaData:
    @pytest.mark.parametrize("method", [
        "get layerMeta() {",
        "get currentLayer() {",
        "get layerCards() {",
        "cardExample(kind) {",
    ])
    def test_method_defined(self, html, method):
        assert method in html, f"método {method!r} ausente no x-data()"

    def test_three_layers_present(self, html):
        for k in ("subagent: {", "router: {", "aobd: {"):
            assert k in html, f"layerMeta sem camada {k!r}"

    def test_icons_titles_techs(self, html):
        for token in (
            "icon: '🎯'", "icon: '🧭'", "icon: '🎼'",
            "title: 'Especialista'", "title: 'Triagem'", "title: 'Maestro'",
            "tech: 'SA'", "tech: 'AR'", "tech: 'AOBD'",
        ):
            assert token in html, f"layerMeta sem {token!r}"

    def test_example_grounded_in_user_agents(self, html):
        """O exemplo do card é aterrado nos dados reais (availableAgents)."""
        assert "this.availableAgents || []" in html


# ────────────────────────────────────────────────────────────────
# Painel Mentor: header dinâmico + intro + jargão
# ────────────────────────────────────────────────────────────────
class TestMentorPanelCopy:
    def test_you_are_creating_a(self, html):
        assert "Você está criando um" in html

    def test_header_is_dynamic_by_layer(self, html):
        assert 'x-text="currentLayer.icon"' in html
        assert 'x-text="currentLayer.title"' in html
        assert "'(' + currentLayer.tech + ')'" in html

    def test_intro_is_dynamic(self, html):
        assert 'x-text="mentorIntroForKind()"' in html
        assert "mentorIntroForKind() {" in html

    def test_aobd_intro_mirrors_golden_rule(self, html):
        """Intro do Maestro reforça que ele NUNCA executa — só delega."""
        assert "NUNCA executa a tarefa final" in html


class TestJargonTranslator:
    def test_glossary_is_collapsible_details(self, html):
        assert "<details" in html
        assert "Tradutor de jargão" in html

    def test_translates_key_terms(self, html):
        for term in ("RAG / Exigir Evidência", "Pass-through", "task_type", "AI Mesh"):
            assert term in html, f"jargão sem termo {term!r}"


# ────────────────────────────────────────────────────────────────
# Prontidão: medidor vivo
# ────────────────────────────────────────────────────────────────
class TestReadinessMeter:
    def test_getter_defined(self, html):
        assert "get mentorReady() {" in html

    def test_pct_is_computed_from_done_over_total(self, html):
        assert "pct: total ? Math.round((done / total) * 100) : 0" in html

    def test_bar_width_bound_to_pct(self, html):
        assert ":style=\"'width:' + mentorReady.pct + '%'\"" in html

    def test_pct_and_counts_displayed(self, html):
        assert "mentorReady.pct + '%'" in html
        assert 'x-text="mentorReady.done"' in html
        assert 'x-text="mentorReady.total"' in html

    def test_celebrates_at_100(self, html):
        assert "mentorReady.pct === 100" in html


# ────────────────────────────────────────────────────────────────
# Checklist viva e acionável
# ────────────────────────────────────────────────────────────────
class TestMentorChecklist:
    def test_getter_defined(self, html):
        assert "get mentorChecklist() {" in html

    def test_branches_per_kind(self, html):
        assert "if (this.form.kind === 'aobd') {" in html
        assert "if (this.form.kind === 'router') {" in html

    def test_renders_items(self, html):
        assert 'x-for="(item, i) in mentorChecklist"' in html
        assert "item.done ? '✓' : '⚠'" in html
        assert 'x-text="item.label"' in html

    def test_action_button_wired_and_guarded(self, html):
        assert 'x-show="!item.done && item.act"' in html
        assert '@click="mentorAct(item.act)"' in html
        assert 'x-text="item.actLabel"' in html

    def test_aobd_items(self, html):
        for label in (
            "Defina a missão",
            "Crie ≥2 rotas de delegação",
            "Política de fallback",
            "Regra de ouro (só delega, nunca executa)",
            "Conecte ≥1 agente no AI Mesh",
        ):
            assert label in html, f"checklist AOBD sem item {label!r}"

    def test_router_items(self, html):
        for label in (
            "Defina a missão de triagem",
            "Liste ≥2 categorias e destinos",
            "Trate entradas ambíguas",
        ):
            assert label in html, f"checklist AR sem item {label!r}"

    def test_subagent_items(self, html):
        for label in (
            "Escreva instruções reais (não genéricas)",
            "Dê conhecimento (Skill ou RAG)",
            "Defina o formato de saída",
        ):
            assert label in html, f"checklist SA sem item {label!r}"


class TestReadinessSignals:
    """Os 'done' reaproveitam o estado REAL — não inventam fonte de verdade."""

    def test_route_count_helper(self, html):
        assert "_routeCount() {" in html
        assert "(t.match(/→/g) || []).length" in html

    def test_mesh_connected_reads_diagnostics_in_edit(self, html):
        assert "get meshConnected() {" in html
        assert "this.diagnostics?.capabilities?.mesh_downstream_count" in html

    def test_mesh_connected_reads_staged_in_new(self, html):
        assert "this.pendingMeshTargets?.length" in html
        assert "this.missionAgentTargets?.length" in html

    def test_prompt_quality_reuses_passthrough(self, html):
        assert "done: !this.isPassthrough" in html

    def test_knowledge_reuses_skill_or_rag(self, html):
        assert "!!this.form.skill_id || !!this.form.require_evidence" in html


class TestMentorActions:
    """O dispatcher leva ao lugar certo / abre a ferramenta da jornada (PR1-4)."""

    def test_dispatcher_defined(self, html):
        assert "mentorAct(act) {" in html

    def test_composer_action_opens_composer_guarded(self, html):
        assert "if (!this.composerEnabledForKind()) { this.step = 2; return; }" in html
        assert "this.openComposer();" in html

    def test_scaffold_action(self, html):
        assert "if (act === 'scaffold') { this.step = 2; this.applyScaffold(); return; }" in html

    def test_field_jump_actions(self, html):
        assert "if (act === 'prompt') { this.step = 2; return; }" in html
        assert "if (act === 'skill' || act === 'basic') { this.step = 0; return; }" in html


class TestNoBannedPalette:
    """Reforço local da regra red+white: sem roxo no template do Mentor."""

    def test_no_violet_fuchsia_purple(self, html):
        import re
        assert not re.search(r"\b(violet|fuchsia|purple)-\d", html)
