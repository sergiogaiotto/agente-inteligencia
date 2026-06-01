"""Context Scope — Wizard UI no modal de Edição de Conexão (Fase 3).

Smoke tests sobre o source HTML/JS de `app/templates/pages/mesh.html`
seguindo o padrão de `test_mesh_conditional_wizard.py::TestMeshUiWizard`
— validam que os hooks principais (state, métodos, tabs, pattern cards,
serialização) estão presentes. Não exercitam o navegador.

PRs encadeados: Foundation (#256) → API (#258) → UI Wizard (este).
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _mesh_html() -> str:
    p = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh.html"
    return p.read_text(encoding="utf-8")


# ─── State Alpine ────────────────────────────────────────────────────


class TestScopeState:
    def test_data_has_scope_form_fields(self):
        src = _mesh_html()
        # State principal — campos esperados no scopeForm
        assert "scopeForm:" in src
        assert "scopeOpen:" in src
        assert "scopeTab:" in src
        assert "scopeVars:" in src
        assert "scopeModes:" in src
        assert "scopeSim:" in src

    def test_scope_form_has_mode_template_max_chars(self):
        src = _mesh_html()
        # Default state pra evitar form em estado inválido na primeira render
        assert "mode: 'inherit'" in src
        assert "template: ''" in src
        assert "max_chars: null" in src

    def test_scope_sim_has_diff_fields(self):
        """scopeSim precisa carregar campos pra mostrar diff antes/depois."""
        src = _mesh_html()
        assert "chars_before:" in src
        assert "chars_after:" in src
        assert "reduction_pct:" in src
        assert "output_filtered:" in src
        assert "applied:" in src


# ─── Tabs + Pattern cards ────────────────────────────────────────────


class TestScopeWizardTabs:
    def test_4_subtabs_present(self):
        """Mesmo padrão de tabs do conditional: 1.Modo / 2.Padrão / 3.Refinar / 4.Simular."""
        src = _mesh_html()
        for label in ["1. Modo", "2. Padrão", "3. Refinar", "4. Simular"]:
            assert label in src, f"sub-tab faltando: {label}"

    def test_tab_iterator_uses_canonical_names(self):
        """O x-for que cria os botões de tab usa as 4 strings canônicas."""
        src = _mesh_html()
        assert "['mode','pattern','refine','simulate']" in src

    def test_three_mode_cards_via_x_for(self):
        """Modo é renderizado a partir de scopeModes (que vem da API) — não
        hardcoded — pra ficar single-source-of-truth com /context-scope-vars."""
        src = _mesh_html()
        assert "x-for=\"m in scopeModes\"" in src
        assert "scopeForm.mode = m.id" in src

    def test_pattern_cards_present(self):
        """4 pattern cards no passo 2 — guiam o operador sem precisar saber Jinja."""
        src = _mesh_html()
        for pat in [
            "Truncar em N chars",
            "Só primeira linha",
            "Só bloco JSON",
            "Expressão livre (Jinja)",
        ]:
            assert pat in src, f"pattern card faltando: {pat}"

    def test_apply_scope_pattern_handlers(self):
        """Pattern cards chamam `applyScopePattern(<name>)`."""
        src = _mesh_html()
        for name in ["truncate", "first_line", "json_only", "custom"]:
            assert f"applyScopePattern('{name}')" in src


# ─── Métodos JS ──────────────────────────────────────────────────────


class TestScopeMethods:
    def test_load_scope_vars_method(self):
        src = _mesh_html()
        assert "async loadScopeVars()" in src
        assert "'/api/v1/mesh/context-scope-vars'" in src

    def test_apply_scope_pattern_method(self):
        src = _mesh_html()
        assert "applyScopePattern(name)" in src
        # Pattern definitions
        assert "this.scopeForm.mode = 'scoped'" in src

    def test_insert_scope_var_method(self):
        """Vars panel insere no template — não no condition_expr — para não
        cruzar wires com o wizard conditional."""
        src = _mesh_html()
        assert "insertScopeVar(varname)" in src
        assert "this.scopeForm.template" in src

    def test_run_scope_simulation_method(self):
        src = _mesh_html()
        assert "async runScopeSimulation()" in src
        assert "'/api/v1/mesh/connections/test-context-scope'" in src

    def test_build_scope_payload_method(self):
        """buildScopePayload retorna null quando estado é default — evita
        poluir configs com inherit redundante."""
        src = _mesh_html()
        assert "buildScopePayload()" in src
        assert "if (!m || m === 'inherit') return null" in src

    def test_scope_status_line_method(self):
        """Status line aparece no header collapsível pra operador entender
        o estado atual sem expandir."""
        src = _mesh_html()
        assert "scopeStatusLine()" in src


# ─── Save: serialização no config ────────────────────────────────────


class TestSaveSerializesScope:
    def test_save_uses_build_scope_payload(self):
        src = _mesh_html()
        assert "buildScopePayload()" in src
        assert "cfg.context_scope = scopePayload" in src

    def test_save_combines_expr_and_scope_in_single_config(self):
        """`config` carrega DOIS eixos ortogonais — expr (conditional) e
        context_scope (sempre). Refator do save reflete isso."""
        src = _mesh_html()
        # O construtor do cfg começa vazio e ambos são opcionais
        assert "const cfg = {}" in src
        assert "if (expr) cfg.expr = expr" in src
        assert "JSON.stringify(cfg)" in src


# ─── Reset on open + parsing on edit ─────────────────────────────────


class TestOpenEditorResetAndParse:
    def test_open_conn_editor_resets_scope_state(self):
        src = _mesh_html()
        # Reset no início (não vazar estado entre aberturas)
        assert "this.scopeTab = 'mode'" in src
        assert "this.scopeForm = { mode: 'inherit'" in src

    def test_open_conn_editor_parses_context_scope_from_config(self):
        src = _mesh_html()
        # Lê o sub-objeto context_scope quando editando uma conexão existente
        assert "cfg.context_scope" in src
        assert "this.scopeForm = {" in src

    def test_auto_expand_when_non_default_mode(self):
        """Quando o operador abre uma conexão com scope já configurado,
        expandir o bloco automaticamente — evita esconder o estado real."""
        src = _mesh_html()
        assert "if (this.scopeForm.mode !== 'inherit') this.scopeOpen = true" in src

    def test_load_scope_vars_called_on_open(self):
        """Vars panel + modes precisam estar populados antes do user
        chegar nos passos correspondentes — lazy load on open."""
        src = _mesh_html()
        assert "this.loadScopeVars()" in src


# ─── Simulator UI: diff antes/depois ─────────────────────────────────


class TestSimulatorDiffUI:
    def test_diff_three_panels_before_after_reduction(self):
        """O simulador mostra 3 painéis: antes / depois / redução%."""
        src = _mesh_html()
        assert "scopeSim.chars_before + ' chars'" in src
        assert "scopeSim.chars_after + ' chars'" in src
        assert "scopeSim.reduction_pct" in src

    def test_token_estimate_chars_div_4(self):
        """Estimativa rough de tokens (chars/4) é fundamental pra operador
        entender o ganho real em $$ — não apenas % de redução de chars."""
        src = _mesh_html()
        assert "Math.ceil(scopeSim.chars_before/4)" in src
        assert "Math.ceil(scopeSim.chars_after/4)" in src

    def test_filtered_output_preview(self):
        """Mostrar o output filtrado dentro de um <pre> — operador valida
        que o resultado faz sentido antes de salvar."""
        src = _mesh_html()
        assert "scopeSim.output_filtered" in src

    def test_simulate_debounce_400ms(self):
        """Auto-sim no input com debounce — mesmo padrão do conditional
        (validado lá como UX confortável)."""
        src = _mesh_html()
        assert "@input.debounce.400ms=\"runScopeSimulation()\"" in src
