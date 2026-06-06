"""Smoke do template agent_form.html — "Compor missão" (Composer de Missão, PR3).

No passo Prompt, agentes ORQUESTRADORES (aobd) e ROTEADORES (router) ganham um
botão "Compor missão / Compor triagem" que abre um MODAL com campos estruturados:

- Missão (obrigatória)
- Regras de roteamento repetíveis: quando … → delegar a … (datalist com agentes
  REAIS + texto livre; skills NÃO são sugeridas — não viram aresta de mesh)
- Política de fallback / entradas ambíguas
- Regra de ouro (só AOBD, checkbox default ON)

Ao "Aplicar", os campos são SERIALIZADOS de forma determinística para o System
Prompt, espelhando os mesmos headers do scaffold (PR2). Não há parse reverso na
v1 — o textarea continua a fonte da verdade.

Alpine.js só roda no browser; como nos demais smokes (Empoderar skill / scaffold),
travamos contratos ESTRUTURAIS no HTML: se um refactor quebrar um ponto, a feature
deixa de funcionar silenciosamente e o teste pega.
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


class TestComposerButton:
    def test_button_calls_open_composer(self, html):
        assert '@click="openComposer()"' in html

    def test_button_only_for_aobd_and_router(self, html):
        """O botão só aparece nas camadas de orquestração/roteamento."""
        assert 'x-show="composerEnabledForKind()"' in html

    def test_label_and_hint_are_dynamic_by_kind(self, html):
        assert '<span x-text="composerLabelForKind()"></span>' in html
        assert ':title="composerHintForKind()"' in html

    def test_button_in_prompt_step_not_other_steps(self, html):
        """Botão precisa estar dentro do bloco do passo Prompt (step === 2)."""
        idx_step3_start = html.find('x-show="step === 2"')
        idx_step4_start = html.find('x-show="step === 3"')
        idx_button = html.find('@click="openComposer()"')
        assert idx_step3_start > 0
        assert idx_step4_start > idx_step3_start
        assert idx_button > 0
        assert idx_step3_start < idx_button < idx_step4_start, (
            "Botão 'Compor missão' precisa estar no bloco do step Prompt"
        )

    def test_composer_sits_between_scaffold_and_refine(self, html):
        """Fluxo do toolbar: Estrutura → Compor missão → IA, refine."""
        idx_scaffold = html.find('@click="applyScaffold()"')
        idx_composer = html.find('@click="openComposer()"')
        idx_refine = html.find("refineField('system_prompt'")
        assert 0 < idx_scaffold < idx_composer < idx_refine


class TestComposerState:
    @pytest.mark.parametrize(
        "marker",
        [
            "composerOpen: false,",
            "availableAgents: [],",
            "mission: {",
            "statement: '',",
            "rules: [{ when: '', target: '' }],",
            "goldenRule: true,",
        ],
    )
    def test_data_prop_defined(self, html, marker):
        assert marker in html, f"estado {marker!r} ausente no x-data()"


class TestComposerMethods:
    @pytest.mark.parametrize(
        "method",
        [
            "composerEnabledForKind() {",
            "composerLabelForKind() {",
            "composerHintForKind() {",
            "composerTitleForKind() {",
            "composerMissionLabel() {",
            "composerRulesLabel() {",
            "composerRuleWhenPlaceholder() {",
            "composerRuleTargetPlaceholder() {",
            "composerFallbackLabel() {",
            "openComposer() {",
            "addMissionRule() {",
            "removeMissionRule(i) {",
            "composeMissionPrompt() {",
            "applyMissionComposer() {",
        ],
    )
    def test_method_defined_in_alpine_data(self, html, method):
        assert method in html, f"método {method!r} ausente no x-data()"

    def test_open_composer_resets_state(self, html):
        """openComposer sempre abre limpo (v1 não faz parse reverso)."""
        assert "this.mission = { statement: '', rules: [{ when: '', target: '' }]" in html
        assert "this.composerOpen = true;" in html

    def test_kind_branches_present(self, html):
        """composeMissionPrompt ramifica por router (else = AOBD)."""
        assert "const isRouter = this.form.kind === 'router';" in html


class TestLoadAgents:
    def test_load_agents_method_defined(self, html):
        assert "async loadAgents() {" in html

    def test_fetches_agents_endpoint(self, html):
        assert "api.get('/api/v1/agents?limit=200')" in html

    def test_excludes_self_when_editing(self, html):
        """Não faz sentido delegar a si mesmo — filtra o agente em edição."""
        assert ".filter(a => a.id !== editId)" in html

    def test_load_agents_called_in_load(self, html):
        assert "await this.loadAgents();" in html


class TestApplyValidationAndGuard:
    def test_mission_is_required(self, html):
        assert "Informe a missão antes de aplicar" in html

    def test_confirms_before_overwriting_real_content(self, html):
        """Mesmo guard defensivo do scaffold/empoderar."""
        assert "O System Prompt atual será substituído pela missão composta" in html
        assert "Você é um agente inteligente." in html

    def test_applies_to_system_prompt(self, html):
        assert "this.form.system_prompt = this.composeMissionPrompt();" in html


class TestComposeAobdAssembly:
    def test_emits_orchestration_headers(self, html):
        for marker in (
            "## Missão",
            "## Critérios de roteamento",
            "## Política de fallback",
            "## Regra de ouro",
        ):
            assert marker in html, f"composeMissionPrompt AOBD sem header {marker!r}"

    def test_uses_delegar_a_verb(self, html):
        assert "delegar a " in html

    def test_golden_rule_line_mirrors_scaffold(self, html):
        assert "NUNCA executa a tarefa final" in html

    def test_golden_rule_emission_is_optional(self, html):
        """A regra de ouro só entra na serialização se o checkbox estiver ON."""
        assert "if (this.mission.goldenRule) {" in html


class TestComposeRouterAssembly:
    def test_emits_triage_headers(self, html):
        for marker in (
            "## Missão de triagem",
            "## Categorias",
            "## Entradas ambíguas ou fora de escopo",
        ):
            assert marker in html, f"composeMissionPrompt AR sem header {marker!r}"

    def test_uses_encaminhar_para_verb(self, html):
        assert "encaminhar para " in html


class TestComposerModal:
    def test_modal_uses_house_overlay_pattern(self, html):
        assert 'x-show="composerOpen"' in html
        assert "fixed inset-0 z-50" in html

    def test_modal_closes_on_escape_and_outside_click(self, html):
        assert '@keydown.escape.window="composerOpen = false"' in html
        assert '@click.outside="composerOpen = false"' in html

    def test_modal_title_is_dynamic(self, html):
        assert 'x-text="composerTitleForKind()"' in html

    def test_mission_textarea_bound(self, html):
        assert 'x-model="mission.statement"' in html

    def test_fallback_input_bound(self, html):
        assert 'x-model="mission.fallback"' in html

    def test_repeatable_rules_rendered(self, html):
        assert 'x-for="(rule, i) in mission.rules"' in html
        assert 'x-model="rule.when"' in html
        assert 'x-model="rule.target"' in html

    def test_add_and_remove_rule_buttons(self, html):
        assert '@click="addMissionRule()"' in html
        assert '@click="removeMissionRule(i)"' in html

    def test_apply_button_calls_applier(self, html):
        assert '@click="applyMissionComposer()"' in html


class TestRoutingTargetDatalist:
    """Requisito 'Só agentes' (2026-06-05): roteamento no AI Mesh é agente→agente,
    então o autocomplete de "delegar a" sugere APENAS agentes reais. Skill não é
    nó do Mesh — citá-la vira texto livre (a verificação marca como "skill", sem
    sincronizar). Isso evita a armadilha do "no-op silencioso": antes o usuário
    podia escolher uma skill achando que rotearia, e a regra nunca virava aresta.
    """

    def test_datalist_exists_and_wired(self, html):
        assert 'id="composer-targets"' in html
        assert 'list="composer-targets"' in html

    def test_datalist_omits_skills(self, html):
        """O datalist NÃO oferece skills (marcador específico da option de skill).
        Nota: 'x-for=\"sk in availableSkills\"' ainda existe no seletor de skill do
        próprio agente (linha ~131), por isso checamos o marcador do datalist."""
        assert 'label="skill"' not in html
        assert ":key=\"'sk-' + sk.id\"" not in html
        assert ':value="sk.name"' not in html

    def test_datalist_offers_real_agents(self, html):
        # marcador específico da option de agente no datalist (não a string genérica)
        assert ':value="ag.name" label="agente"' in html
        assert ":key=\"'ag-' + ag.id\"" in html


class TestGoldenRuleScopedToAobd:
    def test_checkbox_only_for_aobd(self, html):
        """Roteador não tem regra de ouro no scaffold — checkbox só para AOBD."""
        assert 'x-show="form.kind === \'aobd\'"' in html
        assert 'x-model="mission.goldenRule"' in html


class TestComposerTargetFilters:
    """2026-06-05 (Melhoria UI): filtros no alvo "delegar a"/"encaminhar para" dos
    DOIS modais (Missão do orquestrador + Triagem do roteador — mesmo markup):
    - checkbox "incluir inativos" (default OFF → sugere só ATIVOS; antes o datalist
      mostrava inativos sem querer, dava pra rotear pra agente desativado);
    - filtro de DOMÍNIOS multi-seleção em chips, derivado dos próprios agentes-alvo
      (não dos domínios globais), some quando há <2 domínios distintos.
    É filtro de SUGESTÃO: texto livre segue aceito e resolve normalmente — por isso
    o dry-run ganha um badge "inativo" quando o alvo resolvido está desativado
    (honestidade / sem falsa confiança). 100% frontend (Alpine só roda no browser →
    travamos contratos ESTRUTURAIS no HTML).
    """

    # --- estado ---
    @pytest.mark.parametrize("marker", ["composerIncludeInactive: false,", "composerDomainFilter: [],"])
    def test_state_props_defined(self, html, marker):
        assert marker in html, f"estado {marker!r} ausente no x-data()"

    # --- getters / helpers ---
    @pytest.mark.parametrize(
        "method",
        [
            "_agentDomains(a) {",
            "get composerDomains() {",
            "get composerTargetAgents() {",
            "toggleComposerDomain(d) {",
        ],
    )
    def test_methods_defined(self, html, method):
        assert method in html, f"método/getter {method!r} ausente no x-data()"

    def test_agent_domains_splits_csv(self, html):
        # domínio do agente é CSV ("Cobrança, Suporte") → lista
        assert "return ((a && a.domain) || '').split(',').map(d => d.trim()).filter(Boolean);" in html

    def test_domains_derived_from_target_agents_not_global(self, html):
        # composerDomains itera os agentes-alvo (não /api/v1/domains)
        assert "for (const d of this._agentDomains(a)) set.add(d);" in html

    # --- lógica de filtro ---
    def test_active_only_by_default(self, html):
        assert "if (!active && !this.composerIncludeInactive) return false;" in html
        assert "const active = (a.status || 'active') === 'active';" in html

    def test_domain_filter_is_or_match(self, html):
        # passa se casar ≥1 domínio selecionado (multi-seleção = OR)
        assert "if (!ds.some(d => dom.includes(d))) return false;" in html

    def test_resolution_unaffected_by_filter(self, html):
        # texto livre resolve contra TODOS os agentes (filtro é só de sugestão)
        assert "this.availableAgents.find(a => this._normName(a.name) === n)" in html

    # --- datalist consome a lista FILTRADA ---
    def test_datalist_iterates_filtered_list(self, html):
        assert 'x-for="ag in composerTargetAgents"' in html

    # --- UI: checkbox + chips ---
    def test_include_inactive_checkbox_wired(self, html):
        assert 'x-model="composerIncludeInactive"' in html

    def test_domain_chips_gated_by_count(self, html):
        # só renderiza com ≥2 domínios distintos (senão é ruído)
        assert 'x-if="composerDomains.length >= 2"' in html

    def test_domain_chip_toggles_and_highlights(self, html):
        assert '@click="toggleComposerDomain(d)"' in html
        assert "composerDomainFilter.includes(d) ? 'bg-brand-600 text-white border-brand-600'" in html

    def test_domain_clear_button(self, html):
        assert '@click="composerDomainFilter = []"' in html

    # --- dry-run: flag + badge "inativo" ---
    def test_checks_compute_inactive_flag(self, html):
        assert "const inactive = !!(agent && (agent.status || 'active') !== 'active');" in html
        assert "cls, routing, expr, inactive, kind };" in html

    def test_inactive_badge_rendered(self, html):
        assert 'x-show="chk.inactive"' in html
        assert ">inativo</span>" in html
        assert "bg-red-100 text-red-700" in html

    def test_palette_no_purple_tones(self, html):
        # constraint do projeto: roxo proibido (usar vermelho/branco)
        for banned in ("violet", "fuchsia", "purple"):
            assert banned not in html, f"tom proibido {banned!r} no template"


class TestComposerLayerBadge:
    """2026-06-05 (Melhoria UI): destino de regra é agente→agente, mas um agente
    pode ser ele próprio ORQUESTRADOR/ROTEADOR (re-delega = camada de decisão
    extra) em vez de SUBAGENTE (folha que executa). Antes a Verificação mostrava
    "✓ agente" igual pros dois → falsa confiança. Agora:
    - selo de CAMADA (subagente/orquestrador/roteador) em cada linha do dry-run;
    - checkbox "incluir orquestradores/roteadores" (default OFF → sugere só
      subagentes), gated por composerHasOrchestrators (some quando não há nenhum).
    É filtro de SUGESTÃO: texto livre/resolução seguem; o selo é a honestidade.
    100% frontend (Alpine só roda no browser → travamos contratos no HTML).
    """

    # --- estado (default OFF = sugere só subagentes) ---
    def test_state_include_orchestrators_default_false(self, html):
        assert "composerIncludeOrchestrators: false," in html

    # --- helpers de camada ---
    @pytest.mark.parametrize(
        "method",
        [
            "_isOrchestratorKind(a) {",
            "_kindLabel(kind) {",
            "_kindBadgeClass(kind) {",
            "get composerHasOrchestrators() {",
        ],
    )
    def test_layer_helpers_defined(self, html, method):
        assert method in html, f"helper {method!r} ausente no x-data()"

    def test_is_orchestrator_covers_aobd_and_router(self, html):
        # aobd/router = camada; subagent (default) = folha
        assert "return k === 'aobd' || k === 'router';" in html

    def test_kind_label_maps_three_layers(self, html):
        assert "k === 'aobd' ? 'orquestrador' : (k === 'router' ? 'roteador' : 'subagente')" in html

    def test_kind_badge_colors_no_purple(self, html):
        # orquestrador=âmbar, roteador=índigo, subagente=neutro (sem roxo)
        assert "if (k === 'aobd') return 'bg-amber-100 text-amber-700';" in html
        assert "if (k === 'router') return 'bg-indigo-100 text-indigo-700';" in html
        assert "return 'bg-surface-100 text-surface-600';" in html

    # --- filtro de SUGESTÃO: só subagentes por default ---
    def test_datalist_hides_orchestrators_by_default(self, html):
        assert "if (this._isOrchestratorKind(a) && !this.composerIncludeOrchestrators) return false;" in html

    # --- dry-run carrega kind do alvo resolvido ---
    def test_checks_compute_kind(self, html):
        assert "const kind = agent ? (agent.kind || 'subagent') : '';" in html

    # --- UI: checkbox (gated) ---
    def test_include_orchestrators_checkbox_wired(self, html):
        assert 'x-model="composerIncludeOrchestrators"' in html

    def test_orchestrators_checkbox_gated(self, html):
        # só aparece quando há orquestrador/roteador entre os agentes
        assert 'x-if="composerHasOrchestrators"' in html

    # --- UI: selo de camada no dry-run ---
    def test_layer_badge_rendered(self, html):
        assert 'x-show="chk.kind"' in html
        assert 'x-text="_kindLabel(chk.kind)"' in html
        assert ':class="_kindBadgeClass(chk.kind)"' in html
