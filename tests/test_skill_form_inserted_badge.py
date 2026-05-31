"""Smoke do template skill_form.html — badge 'INSERIDO' nos dropdowns.

User pediu (2026-05-28): ao abrir o dropdown "Inserir MCP" (e similares),
itens que já estão no SKILL.md atual deveriam vir marcados visualmente.
Antes, nada distinguia tools já inseridas das ainda disponíveis — operador
tinha que ler o markdown pra saber.

Fix: helper Alpine `isInsertedInRaw(identifier)` busca o identifier no
form.raw_content. Cada dropdown (MCP, API, Tabela) marca o item com
badge "INSERIDO" + cor levemente destacada.

Não cobre RAG porque ele já tem checkbox sincronizado via boundSourceIds.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "skill_form.html").read_text(encoding="utf-8")


class TestHelperPresent:
    def test_is_inserted_in_raw_helper_defined(self, html):
        """Método Alpine isInsertedInRaw existe no x-data."""
        assert "isInsertedInRaw(identifier)" in html

    def test_helper_is_case_insensitive(self, html):
        """Busca case-insensitive — UUID vem com letras minúsculas mas
        nome legível pode estar capitalizado no markdown."""
        assert ".toLowerCase()" in html

    def test_helper_returns_false_for_empty_identifier(self, html):
        """Guarda contra falso positivo quando identifier vazio
        (chamadas tipo isInsertedInRaw(undefined) ou '')."""
        assert "if (!identifier) return false" in html


class TestMcpDropdownBadge:
    def test_mcp_item_has_inserted_check_class(self, html):
        """Cada item do dropdown MCP usa isInsertedInRaw(t.id)."""
        assert "isInsertedInRaw(t.id)" in html

    def test_mcp_button_has_count_badge(self, html):
        """Botão 'Inserir MCP' mostra contador 'N/total' de tools inseridas."""
        # Filter expression presente
        assert "mcpTools.filter(t => isInsertedInRaw(t.id))" in html

    def test_mcp_badge_text_says_inserido(self, html):
        """Badge visual cita 'INSERIDO' pra clareza imediata."""
        # Pelo menos um lugar tem o texto
        assert "INSERIDO" in html


class TestApiDropdownBadge:
    def test_api_item_checks_ep_id(self, html):
        """Endpoint identificado por ep_id no raw_content."""
        assert "isInsertedInRaw(item.ep_id)" in html


class TestTableDropdownBadge:
    def test_table_item_checks_urn(self, html):
        """Tabela identificada pelo URN (formato urn:table:...)."""
        assert "isInsertedInRaw(t.urn)" in html

    def test_table_badge_uses_feminine_form(self, html):
        """Português: 'INSERIDA' (feminino) pra 'Tabela'."""
        assert "INSERIDA" in html


class TestRagDropdownUnchanged:
    """RAG não foi modificado — já tem checkbox sincronizado via
    boundSourceIds. Confirma que não regredimos esse fluxo."""

    def test_rag_keeps_checkbox_pattern(self, html):
        assert "boundSourceIds.includes(src.id)" in html
        assert 'type="checkbox"' in html


class TestUnifiedCountersXOverN:
    """Padrão unificado X/N (escolhidos/disponíveis) nos 4 botões do toolbar.

    User pediu (2026-05-31): os contadores de Inserir API e Inserir Tabela
    mostravam só `disponíveis no catálogo` (3 endpoints, 2 tabelas), enquanto
    Inserir MCP já mostrava `X/N`. Unificamos tudo no formato `X/N` para que
    a contagem reflita imediatamente o estado do SKILL.md.
    """

    def test_api_badge_uses_x_over_n_pattern(self, html):
        """Inserir API: inseridos no SKILL.md / endpoints disponíveis."""
        assert "apiEndpointsFlat.filter(i => isInsertedInRaw(i.ep_id)).length + '/' + apiEndpointsFlat.length" in html

    def test_table_badge_uses_x_over_n_pattern(self, html):
        """Inserir Tabela: tabelas referenciadas / tabelas disponíveis."""
        assert "availableTables.filter(t => isInsertedInRaw(t.urn)).length + '/' + availableTables.length" in html

    def test_mcp_badge_uses_x_over_n_pattern(self, html):
        """Inserir MCP: tools inseridas / tools disponíveis (padrão preexistente)."""
        assert "mcpTools.filter(t => isInsertedInRaw(t.id)).length + '/' + mcpTools.length" in html

    def test_rag_badge_uses_x_over_n_pattern(self, html):
        """Fontes RAG: ids vinculados / fontes disponíveis."""
        assert "boundSourceIds.length + '/' + availableSources.length" in html

    def test_mcp_badge_visible_whenever_catalog_has_items(self, html):
        """Badge X/N aparece mesmo com 0 inseridos (ex.: '0/2'), desde que haja catálogo."""
        # x-show passou a depender de mcpTools.length > 0, não da contagem de inseridos.
        assert 'x-show="mcpTools.length > 0"' in html

    def test_rag_badge_visible_whenever_catalog_has_items(self, html):
        """RAG: badge aparece se há fontes disponíveis (mesmo com 0 vinculadas)."""
        assert 'x-show="availableSources.length > 0"' in html
