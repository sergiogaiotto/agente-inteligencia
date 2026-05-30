"""UI A.5: textarea workspace melhorada (multi-linha + JSON-aware + modal expandido).

User reportou (2026-05-30): "torne o campo texto mais adequado para
multiplas linhas, edição de json, etc". Screenshot mostrava textarea
pequeno demais (rows=2, max=360px) que dificultava colar/editar JSON
ou prompts longos.

Mudanças:
1. rows=2 → rows=3 (3 linhas visíveis iniciais)
2. max-height 360px → 50vh (50% viewport — adapta a tela)
3. Font monospace + tamanho menor quando detecta JSON
4. Botão "Formatar JSON" no canto superior do textarea quando aplicável
5. Botão expand (canto superior) abre modal fullscreen
6. Modal: editor 80vh, contador chars/linhas, atalhos Esc/Ctrl+Enter
7. Tab quando JSON: insere 2 espaços (não muda foco)
8. Ctrl/Cmd+E abre modal de qualquer lugar

Smoke estático (Alpine não roda em pytest — checa HTML/JS structure).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def html():
    return Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────
# State + helpers
# ────────────────────────────────────────────────────────────────


class TestState:
    def test_input_expanded_state(self, html):
        assert "inputExpanded:" in html

    def test_last_json_format_cache(self, html):
        """Cache evita re-format quando JSON não mudou (anti-flicker)."""
        assert "_lastJsonFormat:" in html


class TestJsonAwareness:
    def test_input_is_json_helper(self, html):
        """Detecção heurística: começa com `{` ou `[` após trim."""
        assert "_inputIsJson()" in html
        assert "startsWith('{')" in html
        assert "startsWith('[')" in html

    def test_format_json_helper(self, html):
        """Parse + stringify com indent 2. Falha silenciosa em JSON inválido."""
        assert "_formatJson()" in html
        assert "JSON.parse" in html
        assert "JSON.stringify(parsed, null, 2)" in html

    def test_format_json_shows_toast_feedback(self, html):
        assert "'JSON formatado'" in html
        assert "'JSON inválido: '" in html

    def test_handle_tab_inserts_two_spaces_when_json(self, html):
        """Tab no textarea quando JSON: insere indent em vez de mudar foco."""
        assert "_handleTab(ev)" in html
        assert "before + '  ' + after" in html
        # Só ativa em JSON pra não bagunçar input livre
        assert "if (!this._inputIsJson())" in html

    def test_tab_preserved_default_when_not_json(self, html):
        """Quando NÃO é JSON, Tab segue padrão (muda foco) — não bloqueia
        navegação por teclado."""
        body = html[html.find("_handleTab(ev)"):html.find("// Auto-grow")]
        # Early return sem preventDefault quando não é JSON
        assert "return;" in body


# ────────────────────────────────────────────────────────────────
# Auto-grow refactor
# ────────────────────────────────────────────────────────────────


class TestAutoGrow:
    def test_grow_textarea_extracted_as_function(self, html):
        """Antes era inline no @input; agora é função pra _formatJson e
        outras chamadas via JS poderem usar."""
        assert "_growTextarea(el)" in html

    def test_max_height_uses_viewport(self, html):
        """max-h 50vh em vez de 360px fixos — adapta a tela."""
        assert "innerHeight * 0.5" in html
        # Também no style inline do textarea
        assert "max-height:50vh" in html

    def test_initial_rows_increased(self, html):
        """rows=3 dá mais respiro inicial pra inputs grandes."""
        assert 'rows="3"' in html


# ────────────────────────────────────────────────────────────────
# Toolbar overlay (format JSON + expand)
# ────────────────────────────────────────────────────────────────


class TestToolbar:
    def test_format_json_button_in_toolbar(self, html):
        """Botão `{ }` aparece SÓ quando _inputIsJson() — não polui composer
        em mensagens normais."""
        # Posicionado absolute no canto do textarea
        assert 'x-show="_inputIsJson()"' in html
        # Texto monospace { }
        assert '>\n                            { }' in html or "{ }" in html

    def test_expand_button_present(self, html):
        """Botão de expandir sempre visível pra abrir modal."""
        assert "_expandInput()" in html
        # Tooltip explica atalho
        assert "Expandir editor (Ctrl+E)" in html

    def test_textarea_has_padding_for_toolbar(self, html):
        """pr-20 (padding-right grande) garante que toolbar não cobre texto."""
        # Procura no bloco do textarea principal — busca pelo class= mais próximo
        chat_input_pos = html.find('x-ref="chatInput"')
        assert chat_input_pos >= 0
        # Próximo 'class=' depois do x-ref tem o conjunto de classes
        body = html[chat_input_pos:chat_input_pos + 2000]
        assert "pr-20" in body


# ────────────────────────────────────────────────────────────────
# Modal expandido
# ────────────────────────────────────────────────────────────────


class TestExpandModal:
    def test_modal_present(self, html):
        assert "MODAL EXPANDIDO DO TEXTAREA" in html
        assert 'x-show="inputExpanded"' in html

    def test_modal_uses_same_input_model(self, html):
        """Modal reusa x-model='input' — mesma fonte de verdade. Sem
        sincronização manual entre composer inline e modal."""
        # Conta ocorrências de x-model="input" — deve ter no inline E no modal
        count = html.count('x-model="input"')
        assert count >= 2, f"Esperava >= 2 x-model='input', achou {count}"

    def test_modal_close_via_escape(self, html):
        """Esc fecha modal."""
        assert "@keydown.escape.window=\"_collapseInput()\"" in html

    def test_modal_close_via_click_away(self, html):
        """Click fora do modal fecha."""
        assert '@click.away="_collapseInput()"' in html

    def test_modal_has_ctrl_enter_to_send(self, html):
        """Ctrl+Enter (e Cmd+Enter) envia mensagem."""
        assert "@keydown.ctrl.enter.prevent=\"_sendFromModal()\"" in html
        assert "@keydown.meta.enter.prevent=\"_sendFromModal()\"" in html

    def test_modal_has_char_and_line_counter(self, html):
        """Footer do modal mostra chars + linhas pro user ter feedback."""
        # Span com x-text length + texto " chars" / " linhas" depois
        assert "(input||'').length" in html
        assert "(input||'').split('\\n').length" in html or "split('\\n').length" in html
        # Labels visíveis
        assert " chars" in html
        assert " linhas" in html

    def test_modal_height_is_80vh(self, html):
        """Altura razoável: 80vh deixa margem pra ver mensagens atrás."""
        assert "height: 80vh" in html

    def test_modal_focus_textarea_on_open(self, html):
        """_expandInput foca o textarea do modal e coloca cursor no fim."""
        assert "data-modal-textarea" in html
        assert "el.selectionStart = el.selectionEnd = this.input.length" in html


# ────────────────────────────────────────────────────────────────
# Shortcuts
# ────────────────────────────────────────────────────────────────


class TestKeyboardShortcuts:
    def test_ctrl_e_opens_modal(self, html):
        """Ctrl+E (e Cmd+E pro Mac) abre modal — atalho power user."""
        assert "@keydown.ctrl.e.prevent=\"_expandInput()\"" in html
        assert "@keydown.meta.e.prevent=\"_expandInput()\"" in html

    def test_enter_still_sends_in_main_composer(self, html):
        """Comportamento legacy preservado: Enter envia, Shift+Enter quebra
        linha no composer inline."""
        # @keydown.enter.prevent="onEnterKey($event)" continua presente
        assert "@keydown.enter.prevent=\"onEnterKey($event)\"" in html


# ────────────────────────────────────────────────────────────────
# Visual: monospace quando JSON
# ────────────────────────────────────────────────────────────────


class TestMonospaceOnJson:
    def test_textarea_class_conditional(self, html):
        """font-mono quando _inputIsJson() — leitura melhor de chaves/
        colchetes. Tamanho menor (12px) pra caber mais texto."""
        # Bind condicional no class
        assert "_inputIsJson() ? 'font-mono text-[12px]' : 'text-[13px]'" in html

    def test_modal_textarea_also_monospace_on_json(self, html):
        """Modal usa mesma heurística pra consistência visual."""
        # Procura no bloco do modal
        modal_block = html[html.find("MODAL EXPANDIDO"):html.find("</aside>")]
        assert "_inputIsJson() ? 'font-mono" in modal_block
