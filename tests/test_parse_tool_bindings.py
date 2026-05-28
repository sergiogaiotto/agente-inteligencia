"""Regressão crítica: parse_tool_bindings reconhece formato do Wizard.

Bug encontrado 2026-05-28 via auditoria SQL pedida pelo user:

    skills.tool_bindings:
      - `e46f1652-7918-4cc5-81a4-2920427d62b6` (Tavily MCP Server) — ...

    tools_registry: name='Tavily MCP Server', id='e46f1652-...'

    tool_calls registrados: zero pra essa interação.

Causa raiz: parse_tool_bindings só reconhecia formato legacy
`- **Name**` (com asteriscos) — toda skill gerada pelo Wizard nos
últimos meses, que usa formato `- \`<uuid>\` (Name) — desc`, ficava
com tool_bindings VAZIO depois do parse. Engine pulava direto pra
LLM solo. Resultado: cards de "pesquisa Tavily" eram alucinação
total a partir de memorização do treino.

Skills antigas criadas manualmente (formato **Name**) continuavam
funcionando — daí o bug ter passado despercebido até esta auditoria.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp.runtime import match_with_registry, parse_tool_bindings


# ─── parse_tool_bindings ────────────────────────────────────────────


class TestParseLegacyFormat:
    """Formato antigo com `- **Name**` continua funcionando (não-regressão)."""

    def test_single_legacy_tool(self):
        txt = "- **Tavily Search**"
        out = parse_tool_bindings(txt)
        assert len(out) == 1
        assert out[0]["name"] == "Tavily Search"

    def test_legacy_with_metadata_lines(self):
        txt = (
            "- **Tavily Search**\n"
            "  - Servidor MCP: https://mcp.tavily.com/mcp/\n"
            "  - Operações: search, extract\n"
            "  - Sensitivity: low\n"
        )
        out = parse_tool_bindings(txt)
        assert len(out) == 1
        t = out[0]
        assert t["name"] == "Tavily Search"
        assert t["mcp_server"] == "https://mcp.tavily.com/mcp/"
        assert t["operations"] == ["search", "extract"]
        assert t["sensitivity"] == "low"

    def test_multiple_legacy_tools(self):
        txt = "- **Search**\n- **Extract**\n"
        out = parse_tool_bindings(txt)
        names = [t["name"] for t in out]
        assert "Search" in names
        assert "Extract" in names


class TestParseWizardFormat:
    """Formato do Wizard (PR #145+) com backticks + UUID. **Bug 2026-05-28**:
    esses casos NÃO funcionavam antes do fix — todas as skills geradas pelo
    Wizard com MCP estavam com tools silenciosamente descartadas."""

    def test_uuid_in_backticks_with_display_name(self):
        """Caso EXATO do user: UUID nos backticks + nome amigável nos parênteses."""
        txt = (
            "- `e46f1652-7918-4cc5-81a4-2920427d62b6` (Tavily MCP Server) — "
            "Servidor MCP oficial da Tavily que fornece ferramentas de busca em tempo real"
        )
        out = parse_tool_bindings(txt)
        assert len(out) == 1, f"Esperava 1 tool, veio {out}"
        t = out[0]
        assert t["name"] == "e46f1652-7918-4cc5-81a4-2920427d62b6"
        assert t["display_name"] == "Tavily MCP Server"

    def test_name_in_backticks_without_display(self):
        """Sem display name nos parênteses — só o conteúdo dos backticks."""
        txt = "- `search_kb` — busca na base interna"
        out = parse_tool_bindings(txt)
        assert len(out) == 1
        assert out[0]["name"] == "search_kb"
        assert "display_name" not in out[0]

    def test_multiple_wizard_tools(self):
        """Skill com várias tools — todas extraídas."""
        txt = (
            "- `tool-1` (Search) — busca\n"
            "- `tool-2` (Extract) — extrai\n"
            "- `tool-3` (Map) — mapeia\n"
        )
        out = parse_tool_bindings(txt)
        assert len(out) == 3
        assert {t["name"] for t in out} == {"tool-1", "tool-2", "tool-3"}
        assert {t.get("display_name") for t in out} == {"Search", "Extract", "Map"}

    def test_no_em_dash_still_parses(self):
        """Linha sem o ' — desc' final ainda funciona."""
        txt = "- `tool-x` (Display X)"
        out = parse_tool_bindings(txt)
        assert len(out) == 1
        assert out[0]["name"] == "tool-x"
        assert out[0]["display_name"] == "Display X"


class TestParseEmptyOrInvalid:
    def test_empty_string(self):
        assert parse_tool_bindings("") == []

    def test_only_whitespace(self):
        assert parse_tool_bindings("   \n  \n") == []

    def test_prose_without_bullet_format(self):
        """Skill com declaração explícita de 'sem MCP' (do PR #159) não
        deve gerar tools fantasmas."""
        txt = (
            "(Nenhuma ferramenta MCP foi selecionada para esta skill. "
            "Esta seção DEVE permanecer com a declaração abaixo — NÃO invente nomes de tools.)\n"
            "\n"
            "_Esta skill não usa ferramentas MCP. Recursos disponíveis: RAG._"
        )
        out = parse_tool_bindings(txt)
        assert out == []


# ─── match_with_registry ────────────────────────────────────────────


@pytest.fixture
def fake_repo():
    """Mock tools_repo com 2 tools registradas (Tavily + Context7)."""
    registered = [
        {
            "id": "e46f1652-7918-4cc5-81a4-2920427d62b6",
            "name": "Tavily MCP Server",
            "mcp_server": "https://mcp.tavily.com/mcp/",
            "description": "Tavily search/extract/map/crawl",
            "operations": '["search","extract"]',
            "auth_requirements": "api_key",
            "auth_token": "",
            "auth_config": "{}",
        },
        {
            "id": "ctx7-uuid-aaaa-bbbb-cccc-dddddddddddd",
            "name": "Context 7 MCP Server",
            "mcp_server": "https://mcp.context7.com/mcp",
            "description": "Docs lookup",
            "operations": '["resolve","get_docs"]',
            "auth_requirements": "none",
            "auth_token": "",
            "auth_config": "{}",
        },
    ]
    repo = AsyncMock()
    repo.find_all = AsyncMock(return_value=registered)
    return repo


class TestMatchByUuid:
    @pytest.mark.asyncio
    async def test_match_exact_uuid_from_wizard_format(self, fake_repo):
        """Caso EXATO do user: skill referencia tool por UUID, registry tem
        registro com esse id → match deve ocorrer."""
        parsed = [{"name": "e46f1652-7918-4cc5-81a4-2920427d62b6", "display_name": "Tavily MCP Server"}]
        out = await match_with_registry(parsed, fake_repo)
        assert len(out) == 1
        assert out[0]["db_id"] == "e46f1652-7918-4cc5-81a4-2920427d62b6"
        assert out[0]["mcp_server"] == "https://mcp.tavily.com/mcp/"

    @pytest.mark.asyncio
    async def test_match_by_name_legacy_skills(self, fake_repo):
        """Skills antigas com nome textual continuam casando."""
        parsed = [{"name": "Tavily MCP Server"}]
        out = await match_with_registry(parsed, fake_repo)
        assert len(out) == 1
        assert out[0]["db_id"] == "e46f1652-7918-4cc5-81a4-2920427d62b6"

    @pytest.mark.asyncio
    async def test_match_case_insensitive_name(self, fake_repo):
        parsed = [{"name": "tavily mcp server"}]
        out = await match_with_registry(parsed, fake_repo)
        assert len(out) == 1
        assert out[0]["db_id"] == "e46f1652-7918-4cc5-81a4-2920427d62b6"

    @pytest.mark.asyncio
    async def test_fallback_to_display_name_when_uuid_doesnt_match(self, fake_repo):
        """UUID obsoleto no SKILL.md (tool foi recriada com novo id), mas
        display_name no parênteses ainda bate — fallback resolve."""
        parsed = [{"name": "obsolete-uuid-xxxx", "display_name": "Tavily MCP Server"}]
        out = await match_with_registry(parsed, fake_repo)
        assert len(out) == 1
        assert out[0]["db_id"] == "e46f1652-7918-4cc5-81a4-2920427d62b6"

    @pytest.mark.asyncio
    async def test_no_match_returns_entry_without_db_id(self, fake_repo):
        """Skill referencia tool inexistente: entry vai pro enriched MAS
        sem db_id — engine vai pular essa tool e mostrar warning."""
        parsed = [{"name": "ghost-tool-not-registered"}]
        out = await match_with_registry(parsed, fake_repo)
        # match_with_registry sempre devolve a entrada (mesmo unmatched)
        assert len(out) == 1
        # Sem db_id = engine identifica como unmatched
        assert not out[0].get("db_id")


class TestNameOverrideAfterMatch:
    """Bug crítico (2026-05-28): após match_with_registry, pt.name continuava
    sendo o UUID dos backticks. build_openai_tools sanitizava em
    `e46f1652_7918_4cc5_81a4_2920427d62b6` — LLM não reconhecia, não chamava,
    resposta saía vazia (caso real do _Qresearch_ com Tavily).

    Fix: match_with_registry sobrescreve pt.name com matched.name após casar.
    """

    @pytest.mark.asyncio
    async def test_uuid_in_pt_name_replaced_with_human_name(self, fake_repo):
        """Cenário do bug: parsed_tool veio do Wizard com UUID nos backticks.
        Após match, pt.name DEVE ser 'Tavily MCP Server' (não o UUID)."""
        parsed = [{"name": "e46f1652-7918-4cc5-81a4-2920427d62b6"}]
        out = await match_with_registry(parsed, fake_repo)
        assert len(out) == 1
        assert out[0]["name"] == "Tavily MCP Server", (
            f"name não foi sobrescrito após match — veio '{out[0]['name']}'. "
            "build_openai_tools vai gerar function name esquisito e o LLM não chama."
        )
        # Sanity: o UUID continua disponível como db_id pra outros usos
        assert out[0]["db_id"] == "e46f1652-7918-4cc5-81a4-2920427d62b6"

    @pytest.mark.asyncio
    async def test_legacy_skill_with_name_preserves_name(self, fake_repo):
        """Skill legacy (formato `- **Tavily MCP Server**`) já tinha o nome
        correto — após match, NÃO deve ser alterado (matched.name = mesma coisa)."""
        parsed = [{"name": "Tavily MCP Server"}]
        out = await match_with_registry(parsed, fake_repo)
        assert out[0]["name"] == "Tavily MCP Server"

    @pytest.mark.asyncio
    async def test_unmatched_tool_keeps_original_name(self, fake_repo):
        """Quando NÃO casa (tool órfã), name preserva o que veio do parser.
        Engine vai sinalizar como unmatched no execution_log (PR #169)."""
        parsed = [{"name": "ghost-tool-uuid"}]
        out = await match_with_registry(parsed, fake_repo)
        assert out[0]["name"] == "ghost-tool-uuid"
        assert not out[0].get("db_id")

    @pytest.mark.asyncio
    async def test_fallback_via_display_name_also_overrides(self, fake_repo):
        """UUID obsoleto + display_name correto: match resolve via display,
        e o name final é o do Registry (não display, não UUID)."""
        parsed = [{"name": "obsolete-uuid-xxxx", "display_name": "Tavily MCP Server"}]
        out = await match_with_registry(parsed, fake_repo)
        # Match resolveu via display_name; nome final vem do Registry.
        assert out[0]["name"] == "Tavily MCP Server"
