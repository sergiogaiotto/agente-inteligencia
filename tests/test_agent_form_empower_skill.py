"""Smoke do template agent_form.html — botão 'Empoderar skill' (2026-05-27).

Feature pedida pelo user: na etapa Prompt da criação/edição de agente, botão
que substitui o System Prompt por um template delegando à skill vinculada.

Como Alpine.js executa só no browser, não dá pra testar o JS direto via pytest.
Mas dá pra travar contratos estruturais no HTML — se alguém refatorar e quebrar
um dos pontos abaixo, a feature deixa de funcionar silenciosamente.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html"


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


class TestEmpowerSkillButton:
    def test_button_exists_in_step_3(self, html):
        """Botão verde 'Empoderar skill' presente no template."""
        assert "Empoderar skill" in html

    def test_button_only_visible_with_skill_selected(self, html):
        """x-show garante que o botão aparece SÓ quando há skill no passo Básico.
        Sem isso o botão fica visível e clicar dispara toast de erro — UX ruim.
        """
        assert 'x-show="form.skill_id && selectedSkillPreview"' in html, (
            "Botão 'Empoderar skill' deve ter x-show condicionando à skill vinculada"
        )

    def test_button_calls_apply_method(self, html):
        assert '@click="applyEmpowerSkillTemplate()"' in html

    def test_method_defined_in_alpine_data(self, html):
        """O método precisa existir no x-data() — sem ele o @click vira no-op."""
        assert "applyEmpowerSkillTemplate()" in html
        # Lê do escopo Alpine (definição), não só uso (@click)
        assert "applyEmpowerSkillTemplate() {" in html or "applyEmpowerSkillTemplate () {" in html

    def test_template_uses_skill_name_and_user_input_placeholder(self, html):
        """Formato canônico do template:
            Use a skill <nome> para responder:

            {{USER_INPUT}}

        Sem o nome real da skill ou sem o placeholder de input, o agente não
        sabe nem o que delegar nem onde injetar a mensagem do user.
        """
        # JS string literal — confere fragmento canônico
        assert "Use a skill ${skill.name} para responder:" in html
        # Newline + placeholder runtime
        assert "{{USER_INPUT}}" in html

    def test_method_validates_skill_presence_before_applying(self, html):
        """Defensivo: se por algum motivo o método for chamado sem skill
        (ex: bug futuro removendo o x-show), ele deve sair limpo com toast
        de erro em vez de gerar 'Use a skill undefined ...'."""
        # Procura por uma checagem do tipo `if (!skill)` ou `selectedSkillPreview`
        # antes de aplicar
        assert "selectedSkillPreview" in html
        # E que há retorno antecipado quando skill ausente
        assert "if (!skill)" in html or "if (!this.selectedSkillPreview)" in html

    def test_button_in_prompt_step_not_other_steps(self, html):
        """Botão deve estar dentro do bloco do passo 3 (Prompt), não em outros.

        Estratégia robusta: encontra os marcadores de início (step === 2) e
        fim (próximo step === 3 ou step === 4) do bloco, confere que o botão
        cai dentro desse range.
        """
        # Marcador do início do step 3 (zero-indexed, então é step === 2)
        idx_step3_start = html.find('x-show="step === 2"')
        # Próxima step (Revisão) — marca o fim do bloco
        idx_step4_start = html.find('x-show="step === 3"')
        idx_button = html.find("Empoderar skill")
        assert idx_step3_start > 0
        assert idx_step4_start > idx_step3_start
        assert idx_button > 0
        assert idx_step3_start < idx_button < idx_step4_start, (
            "Botão 'Empoderar skill' precisa estar dentro do bloco do step Prompt "
            f"({idx_step3_start} < {idx_button} < {idx_step4_start})"
        )
