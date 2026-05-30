"""UI: paleta vermelho+branco no slash invoke form (a pedido do user).

User reportou (2026-05-30) screenshot do modal de invocação direta com
tons violet/fuchsia, pedindo troca pra "tons de vermelho e branco".

Escopo: mudança APENAS no bloco do form inline de invocação de binding
(`activeBindingForm`). Outros lugares com violet/fuchsia (slash popover
items binding, badges Recente, modal expand do textarea) preservados
pra manter brand consistency em outras áreas.

Smoke estático: confirma que (a) violeta/fuchsia removidos do bloco do
form, (b) red-* aplicados nos elementos certos.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture
def html():
    return Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")


def _form_block(html: str) -> str:
    """Extrai o bloco do form inline de invocação (entre os marcadores)."""
    start = html.find("FORM INLINE DE INVOCAÇÃO DE BINDING")
    end = html.find("SLASH COMMAND POPOVER")
    assert start >= 0 and end > start
    return html[start:end]


# ────────────────────────────────────────────────────────────────
# Violet/Fuchsia removidos do bloco
# ────────────────────────────────────────────────────────────────


class TestPurpleRemovedFromForm:
    def test_no_violet_classes_in_form_block(self, html):
        """Bloco do form NÃO deve ter mais classes violet-*."""
        form = _form_block(html)
        # Busca qualquer match `violet-NNN` (Tailwind)
        violet_matches = re.findall(r"violet-\d+", form)
        assert violet_matches == [], (
            f"Bloco do form ainda tem violet-*: {violet_matches}"
        )

    def test_no_fuchsia_classes_in_form_block(self, html):
        form = _form_block(html)
        fuchsia_matches = re.findall(r"fuchsia-\d+", form)
        assert fuchsia_matches == [], (
            f"Bloco do form ainda tem fuchsia-*: {fuchsia_matches}"
        )

    def test_no_violet_or_fuchsia_in_invoke_button_gradient(self, html):
        """Botão Invocar (gradient principal do form) NÃO usa mais violet/fuchsia."""
        # Procura o botão @click="invokeBindingDirect" e suas classes
        form = _form_block(html)
        button_start = form.find('@click="invokeBindingDirect')
        button_end = form.find("</button>", button_start)
        assert button_start >= 0
        button = form[button_start:button_end]
        assert "violet" not in button
        assert "fuchsia" not in button


# ────────────────────────────────────────────────────────────────
# Red+white aplicados
# ────────────────────────────────────────────────────────────────


class TestRedPaletteApplied:
    def test_border_red(self, html):
        form = _form_block(html)
        # Border principal do container + borders dos sub-elementos
        assert "border-red-300" in form  # container
        assert "border-red-100" in form  # divider top/bottom
        assert "border-red-200" in form  # badge interno

    def test_header_gradient_red_to_white(self, html):
        """Header usa red→white em vez de violet→fuchsia.
        Implementa o pedido "tons de vermelho E branco" literalmente."""
        form = _form_block(html)
        assert "from-red-50 to-white" in form

    def test_kind_badge_text_red(self, html):
        form = _form_block(html)
        assert "text-red-700" in form  # badge "MCP"

    def test_fonte_text_red(self, html):
        """'fonte: skill_inputs' agora em red-500."""
        form = _form_block(html)
        # text-red-500 no span do "fonte: ..."
        assert "text-red-500" in form

    def test_focus_borders_red(self, html):
        """Inputs/selects ao focar usam red em vez de violet."""
        form = _form_block(html)
        assert "focus:border-red-400" in form
        # Sem focus:border-violet- restante
        assert "focus:border-violet" not in form

    def test_invoke_button_uses_red_gradient(self, html):
        """Botão Invocar: gradient red-500→red-600 com hover mais escuro."""
        form = _form_block(html)
        # Bloco do botão Invocar
        button_start = form.find('@click="invokeBindingDirect')
        button_end = form.find("</button>", button_start)
        button = form[button_start:button_end]
        assert "from-red-500 to-red-600" in button
        assert "hover:from-red-600 hover:to-red-700" in button

    def test_required_marker_uses_red(self, html):
        """Asterisco de required usa red-500 (era rose-500 antes)."""
        form = _form_block(html)
        # Span com title="Obrigatório" agora em red-500
        idx = form.find('title="Obrigatório"')
        assert idx >= 0
        # 200 chars antes desse marker têm a classe text-red-500
        surrounding = form[max(0, idx-200):idx+50]
        assert "text-red-500" in surrounding

    def test_checkbox_uses_red(self, html):
        form = _form_block(html)
        assert "text-red-600" in form  # checkbox tick
        assert "focus:ring-red-400" in form  # focus ring


# ────────────────────────────────────────────────────────────────
# Escopo: NÃO afeta outras áreas
# ────────────────────────────────────────────────────────────────


class TestScopeIsolation:
    def test_slash_popover_still_uses_fuchsia(self, html):
        """Slash command popover (separado do form) preserva paleta
        violet/fuchsia — escopo da mudança é APENAS o form de invocação."""
        # Busca a região do slash popover
        slash_start = html.find("SLASH COMMAND POPOVER")
        slash_end = html.find("</div>", slash_start + 5000)  # janela razoável
        assert slash_start >= 0
        slash_block = html[slash_start:slash_end]
        # Confirma que ainda há violet/fuchsia no popover
        has_violet = "violet-" in slash_block
        has_fuchsia = "fuchsia-" in slash_block
        # Pelo menos um (códigos /name fuchsia + bordas violet)
        assert has_violet or has_fuchsia, (
            "Escopo isolado: slash popover deveria manter violet/fuchsia"
        )

    def test_textarea_expand_modal_preserved(self, html):
        """Modal expand do textarea (UX A.5) não toca."""
        modal_start = html.find("MODAL EXPANDIDO DO TEXTAREA")
        modal_end = html.find("</div>", modal_start + 3000)
        if modal_start < 0:
            pytest.skip("Modal expand não está no main yet")
        # Brand/color do modal preservado
        modal = html[modal_start:modal_end]
        # Modal usa brand- tones, não red — escopo isolado
        assert "brand-500" in modal or "brand-900" in modal
