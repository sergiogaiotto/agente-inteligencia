"""Smoke do template workspace.html — visual rico para respostas JSON.

User pediu (2026-05-31): respostas JSON do agente / tool deveriam aparecer
com visual bonito de cara, não como paredão de JSON cru. A infra já existia
(`renderRichContent`, `_renderResultCards`, `_renderObjectCard`), mas tinha
3 gaps:

1. JSON dentro de fence markdown ``` ```json ... ``` ``` (caso típico do
   invoke-binding-direct → linha ~1802) NÃO era detectado como JSON.
2. Auto-ativação de rich view só rolava no fluxo de chat (`isStructuredContent`
   em msgs do agente), não no `invoke-binding-direct`.
3. Arrays grandes renderizavam tudo de uma vez — sem "mostrar mais N".

Este teste tranca os contratos das 3 correções.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    path = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html"
    return path.read_text(encoding="utf-8")


class TestUnwrapJsonFence:
    """Helper que extrai JSON de dentro de ```json ... ```."""

    def test_unwrap_helper_defined(self, html):
        assert "_unwrapJsonFence(text)" in html

    def test_regex_matches_json_fence(self, html):
        # Padrão captura ```json\n…\n``` (ou ```\n…\n```)
        assert "```(?:json)?" in html

    def test_render_rich_content_uses_unwrap(self, html):
        """renderRichContent chama _unwrapJsonFence antes de tentar JSON.parse."""
        assert "this._unwrapJsonFence(text)" in html

    def test_is_structured_content_detects_fence(self, html):
        """isStructuredContent reconhece ```json ... ``` como estruturado
        (faz o botão de toggle aparecer mesmo quando a resposta vem em fence)."""
        # Regex usado em isStructuredContent inclui o padrão de fence
        assert "/^```(?:json)?\\s*\\n[\\s\\S]*\\n```\\s*$/" in html


class TestAutoActivateRichViewOnBindingDirect:
    """Paridade com o fluxo de chat: invoke-binding-direct também ativa
    rich view automaticamente quando o conteúdo é estruturado."""

    def test_binding_direct_captures_index(self, html):
        assert "const _bindIdx=this.messages.length;" in html

    def test_binding_direct_auto_enables_rich_view(self, html):
        """Após push da mensagem, ativa richViewMsgs se isStructuredContent."""
        assert "if(this.isStructuredContent(content)) this.richViewMsgs[_bindIdx]=true;" in html


class TestArrayCollapseShowMore:
    """Arrays > 3 itens vêm colapsados por padrão (decisão UX 2026-05-31)."""

    def test_visible_constant_is_three(self, html):
        assert "const VISIBLE = 3;" in html

    def test_show_more_uses_native_details(self, html):
        """<details> nativo, sem Alpine x-data (que falha dentro de x-html)."""
        assert "Mostrar mais ${hidden.length} resultado(s)" in html
        # Bloco <details> com summary clicável
        assert "<details class=\"mt-2.5 group\">" in html

    def test_render_result_card_helper_extracted(self, html):
        """_renderResultCard isolado pra reuso entre visíveis e bloco 'mostrar mais'."""
        assert "_renderResultCard(r)" in html

    def test_content_field_also_used_as_snippet(self, html):
        """Tavily usa `content` em vez de snippet/description — ambos devem entrar."""
        # Lista de fallbacks de snippet inclui r.content
        assert "r.snippet || r.description || r.summary || r.abstract || r.text || r.content" in html


class TestJsonTreeCollapsible:
    """Tree colapsável recursiva para JSON genérico (substitui paredão pretty-print)."""

    def test_tree_helper_defined(self, html):
        assert "_renderJsonTree(value, depth = 0)" in html

    def test_tree_respects_max_depth(self, html):
        """Defesa contra ciclos / estruturas gigantes."""
        assert "MAX_DEPTH = 6" in html

    def test_tree_arrays_preview_three_items(self, html):
        """Mesmo padrão dos result cards: 3 visíveis + mostrar mais N."""
        assert "ARRAY_PREVIEW = 3" in html
        assert "mostrar mais ${hidden.length}" in html

    def test_tree_renders_string_as_quoted_emerald(self, html):
        """Strings curtas viram `\"x\"` em verde (padrão de visualização JSON)."""
        assert 'text-emerald-700">"${safe}"' in html

    def test_tree_renders_urls_as_clickable(self, html):
        """URL em string vira <a target=_blank> com hover-underline."""
        assert "/^https?:\\/\\/\\S+$/" in html

    def test_tree_renders_long_strings_as_pre_wrap(self, html):
        """Strings longas (>120 chars) ou com \\n viram <pre> wrap, não inline."""
        assert "value.length > 120" in html
        assert "whitespace-pre-wrap" in html

    def test_tree_used_in_parsed_json_fallback(self, html):
        """Fallback de _renderParsedJson (objeto com > 20 chaves) usa a tree."""
        # Substitui o antigo <pre>JSON.stringify(...) </pre>
        assert "return this._renderJsonTree(obj);" in html

    def test_tree_used_in_nested_object_field(self, html):
        """Objetos aninhados dentro de cards key-value também viram tree."""
        # _renderFieldValue: para typeof v === 'object' não-array
        assert "// Tree colapsável em vez de paredão JSON" in html


class TestRegressionExistingFeatures:
    """Confirma que features pré-existentes continuam intactas."""

    def test_rich_view_toggle_button_preserved(self, html):
        """Botão `</>` que alterna richViewMsgs[i] continua presente."""
        assert "richViewMsgs[i]=!richViewMsgs[i]" in html

    def test_existing_result_cards_renderer_still_called(self, html):
        """Cards de busca (Tavily-like) continuam disponíveis."""
        assert "_renderResultCards" in html

    def test_existing_object_card_renderer_still_called(self, html):
        """Object card (chip Estruturado) continua disponível para objetos pequenos."""
        assert "_renderObjectCard" in html

    def test_auto_rich_view_on_chat_path_preserved(self, html):
        """Auto-ativação no caminho `chat` (linha ~1998) não foi quebrada."""
        # Continua presente após nossa adição no invoke-binding-direct
        assert "this.richViewMsgs[idx] = true;" in html
