"""Smoke do template agent_form.html — "Estrutura" por camada (scaffold).

PR "scaffolds adaptativos no passo 3": no passo Prompt, um botão insere um
ESQUELETO de system prompt adequado ao tipo (form.kind) do agente:

- aobd (Orquestrador) → missão / critérios de roteamento / fallback / regra de ouro
- router (Roteador/AR) → missão de triagem / categorias / entradas ambíguas
- subagent (SA)        → objetivo / entradas / passos / formato de saída / guardrails

Os títulos do esqueleto AOBD/AR espelham as personas do /refine
(refine-por-camada) — estruturar → preencher → "IA, refine".

Alpine.js só roda no browser, então (como no smoke de "Empoderar skill")
travamos contratos ESTRUTURAIS no HTML: se um refactor quebrar um ponto,
a feature deixa de funcionar silenciosamente e o teste pega.
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


class TestScaffoldButton:
    def test_button_calls_apply_scaffold(self, html):
        assert '@click="applyScaffold()"' in html

    def test_label_and_hint_are_dynamic_by_kind(self, html):
        assert '<span x-text="scaffoldLabelForKind()"></span>' in html
        assert ':title="scaffoldHintForKind()"' in html

    def test_button_in_prompt_step_not_other_steps(self, html):
        """Botão precisa estar dentro do bloco do passo 3 (Prompt)."""
        idx_step3_start = html.find('x-show="step === 2"')
        idx_step4_start = html.find('x-show="step === 3"')
        idx_button = html.find('@click="applyScaffold()"')
        assert idx_step3_start > 0
        assert idx_step4_start > idx_step3_start
        assert idx_button > 0
        assert idx_step3_start < idx_button < idx_step4_start, (
            "Botão 'Estrutura' precisa estar dentro do bloco do step Prompt"
        )

    def test_scaffold_comes_before_refine(self, html):
        """Fluxo pretendido: estruturar (scaffold) → lapidar (IA, refine).
        O botão Estrutura deve aparecer ANTES do botão de refino."""
        idx_scaffold = html.find('@click="applyScaffold()"')
        idx_refine = html.find("refineField('system_prompt'")
        assert 0 < idx_scaffold < idx_refine


class TestScaffoldMethods:
    @pytest.mark.parametrize(
        "method",
        [
            "scaffoldForKind() {",
            "scaffoldLabelForKind() {",
            "scaffoldHintForKind() {",
            "promptPlaceholderForKind() {",
            "applyScaffold() {",
        ],
    )
    def test_method_defined_in_alpine_data(self, html, method):
        assert method in html, f"método {method!r} ausente no x-data()"

    def test_kind_branches_present(self, html):
        """scaffoldForKind precisa ramificar por aobd e router (else = SA)."""
        assert "this.form.kind === 'aobd'" in html
        assert "this.form.kind === 'router'" in html

    def test_confirms_before_overwriting_real_content(self, html):
        """Defensivo: não clobberar prompt real sem confirmar (igual Empoderar)."""
        assert "O System Prompt atual será substituído pela estrutura inicial" in html
        # guard reusa lista de genéricos + confirm()
        assert "Você é um agente inteligente." in html


class TestAobdSkeleton:
    def test_has_orchestration_sections(self, html):
        for marker in (
            "## Missão",
            "## Critérios de roteamento",
            "## Política de fallback",
            "## Regra de ouro",
        ):
            assert marker in html, f"esqueleto AOBD sem seção {marker!r}"

    def test_golden_rule_mirrors_refine_persona(self, html):
        """Consistência com a persona do /refine (AOBD nunca executa, só delega)."""
        assert "NUNCA executa a tarefa final" in html


class TestRouterSkeleton:
    def test_has_routing_sections(self, html):
        for marker in (
            "## Missão de triagem",
            "## Categorias",
            "## Entradas ambíguas ou fora de escopo",
        ):
            assert marker in html, f"esqueleto AR sem seção {marker!r}"


class TestSubagentSkeleton:
    def test_has_executor_sections(self, html):
        for marker in (
            "## Objetivo",
            "## Entradas esperadas",
            "## Passos",
            "## Formato de saída",
            "## Restrições (guardrails)",
        ):
            assert marker in html, f"esqueleto SA sem seção {marker!r}"


class TestDynamicPlaceholder:
    def test_textarea_placeholder_is_bound_by_kind(self, html):
        """Placeholder do system prompt vira dinâmico por camada."""
        assert ':placeholder="promptPlaceholderForKind()"' in html

    def test_placeholder_method_branches_by_kind(self, html):
        # AOBD/AR têm dica específica; SA mantém o texto histórico.
        assert "Estrutura de orquestração" in html
        assert "Estrutura de roteamento" in html
        assert "Instruções detalhadas para o agente..." in html
