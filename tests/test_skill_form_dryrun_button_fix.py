"""Fix: dry-run button no skill_form.html não ativava em algumas situações.

User reportou (2026-05-30) screenshot do preview/validação da SKILL
"Consulta a Documentação e Código via Context7 MCP" com OPERATION=prompt
e QUERY=python com ia preenchidos — botão "Testar tool" aparecia faded
sem nenhuma explicação.

Causa raiz provável: mutação de state (`this.dryRunInputs[id] = {params:{}}`)
DENTRO do getter `toolsDeclaredInSkill()` — anti-pattern de Alpine que
dispara re-renders sutis e pode deixar a UI em estado inconsistente.

Fix:
1. toolsDeclaredInSkill() agora é getter PURO (zero mutações)
2. _ensureDryRunState() init explícito via x-init + $watch em raw_content
3. _dryRunCanRun(tool) consolida pré-condições (in-flight, raw_content,
   parseResult, required fields)
4. _dryRunDisabledReason(tool) devolve hint humano sob o botão
5. UI mostra hint amber explicando porque botão está disabled

Cobertura (smoke estático — Alpine não roda em pytest):
- Helpers presentes
- x-init wireado pra _ensureDryRunState
- $watch em form.raw_content
- Botão usa _dryRunCanRun em vez de só dryRunInFlight
- Hint amber renderiza
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def html():
    return Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────
# Anti-pattern fix: getter sem mutação
# ────────────────────────────────────────────────────────────────


class TestPureGetter:
    def test_tools_declared_no_longer_mutates_state(self, html):
        """toolsDeclaredInSkill deve ser PURO — sem mutação de
        this.dryRunInputs (causa raiz do bug)."""
        # Extrai a função inteira pra checar
        start = html.find("toolsDeclaredInSkill()")
        end = html.find("_ensureDryRunState()", start)
        body = html[start:end]
        # NÃO deve atribuir a dryRunInputs[id] dentro do getter
        assert "this.dryRunInputs[id] =" not in body, (
            "toolsDeclaredInSkill ainda muta state — bug não corrigido"
        )

    def test_ensure_dry_run_state_helper_exists(self, html):
        """Init de dryRunInputs agora vive em helper explícito."""
        assert "_ensureDryRunState()" in html
        assert "this.dryRunInputs[t.id] = { params: {} }" in html


# ────────────────────────────────────────────────────────────────
# Disabled state + feedback claro
# ────────────────────────────────────────────────────────────────


class TestDisabledFeedback:
    def test_can_run_helper_exists(self, html):
        assert "_dryRunCanRun(tool)" in html
        # Cobre 4 pré-condições
        assert "this.dryRunInFlight[tool.id]" in html
        assert "this.form.raw_content" in html
        assert "this.parseResult" in html
        assert "_dryRunValidate(tool)" in html

    def test_validate_returns_missing_fields(self, html):
        """_dryRunValidate retorna {ok, missing} pra UI listar fields vazios."""
        assert "_dryRunValidate(tool)" in html
        assert "missing.push(name)" in html
        # Required check + empty string check
        assert "meta.required" in html

    def test_disabled_reason_helper_exists(self, html):
        """_dryRunDisabledReason devolve mensagem humana pra o tooltip + hint."""
        assert "_dryRunDisabledReason(tool)" in html
        # Mensagens previsíveis pro user entender
        assert "'Testando...'" in html
        assert "'Edite a SKILL primeiro'" in html
        assert "'Aguardando preview/validação'" in html
        assert "'Preencha: '" in html


# ────────────────────────────────────────────────────────────────
# Wiring no HTML: x-init + $watch + button disabled
# ────────────────────────────────────────────────────────────────


class TestWiring:
    def test_x_init_calls_ensure_state(self, html):
        """x-init no container chama _ensureDryRunState() pra garantir
        state disponível no primeiro render."""
        assert "x-init=" in html
        assert "_ensureDryRunState()" in html

    def test_watch_form_raw_content(self, html):
        """$watch reage a mudanças em form.raw_content pra re-init quando
        user edita o markdown (tools podem aparecer/sumir)."""
        assert "$watch('form.raw_content'" in html

    def test_button_uses_can_run_not_just_in_flight(self, html):
        """Botão Testar tool agora usa _dryRunCanRun (4 pré-condições)
        em vez de só dryRunInFlight (1 condição)."""
        # Procura a linha do botão "Testar tool"
        button_section = html[html.find("@click=\"runDryRunTool"):html.find("Hint:")]
        assert ":disabled=\"!_dryRunCanRun(t)\"" in button_section

    def test_button_has_cursor_not_allowed_when_disabled(self, html):
        """Tailwind disabled:cursor-not-allowed dá feedback visual claro
        ao hover sobre o botão disabled."""
        button_section = html[html.find("@click=\"runDryRunTool"):html.find("Hint:")]
        assert "disabled:cursor-not-allowed" in button_section

    def test_button_has_dynamic_title_tooltip(self, html):
        """title= no botão muda pra mensagem de _dryRunDisabledReason
        quando disabled — fallback hover explica o estado."""
        assert ":title=\"_dryRunDisabledReason(t)" in html


class TestVisualHint:
    def test_amber_hint_below_button(self, html):
        """Span amber italic abaixo do botão mostra _dryRunDisabledReason
        quando aplicável. Garante que user nunca veja botão faded sem
        saber o porquê."""
        assert "text-amber-600 italic" in html
        # x-show condicionado a !canRun && tem reason
        assert "!_dryRunCanRun(t) && _dryRunDisabledReason(t)" in html
