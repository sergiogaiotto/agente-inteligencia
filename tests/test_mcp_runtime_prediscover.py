"""Onda B.2 — pre_discover_input_schemas: auto-discovery de tool.inputSchema
via MCP tools/list em paralelo.

Onda B fechou o gap PRA SKILLs com `## Inputs` explícito. Onda B.2 fecha
PRA SKILLs SEM `## Inputs` quando o servidor MCP expõe inputSchema via
tools/list — engine usa o schema REAL do servidor em vez do legacy fixed.

Cobertura:
- Populates inputSchema quando server retorna
- Skip respeitando override manual (não sobrescreve inputSchema existente)
- Skip endpoints stdio (não-HTTP)
- Skip auth oauth2/mTLS (fora do escopo B.2)
- Falhas silenciosas (conn refused, timeout, json malformado)
- Paralelismo (asyncio.gather) — 1 endpoint lento não bloqueia os outros
- Integração com build_openai_tools: após pre_discover, origin vira
  tool_input_schema (em vez de legacy)
- Regressão arquitetural: SKILL sem ## Inputs + tool com inputSchema descoberto
  = LLM vê schema real (não comprime mais)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


CTX7_TOOL_BASE = {
    "name": "Context 7",
    "mcp_server": "https://mcp.context7.com/mcp",
    "operations": ["docs", "code", "prompt"],
    "auth_requirements": "",
    "auth_token": "",
}

# Servidor MCP retorna tools list — _resolve_tool_name fará o match com
# o nome local da tool. Aqui simulamos o caso comum: servidor expõe um
# tool com o mesmo nome que a SKILL referencia.
CTX7_SERVER_RESPONSE = [
    {
        "name": "Context 7",  # match exato com nome da tool registrada
        "description": "Fetch docs",
        "inputSchema": {
            "type": "object",
            "required": ["libraryName"],
            "properties": {
                "libraryName": {"type": "string"},
                "topic": {"type": "string"},
                "tokens": {"type": "number"},
            },
        },
    },
]


# ────────────────────────────────────────────────────────────────
# Unit: skip conditions
# ────────────────────────────────────────────────────────────────


class TestSkipConditions:
    @pytest.mark.asyncio
    async def test_skips_when_input_schema_already_set(self):
        """Manual override (autor declarou ## Inputs OU populou direto)
        é respeitado — pre_discover não sobrescreve."""
        from app.mcp.runtime import pre_discover_input_schemas
        original_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        tools = [{**CTX7_TOOL_BASE, "inputSchema": original_schema}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=CTX7_SERVER_RESPONSE)):
            await pre_discover_input_schemas(tools)
        # Mantido — não sobrescreveu
        assert tools[0]["inputSchema"] == original_schema

    @pytest.mark.asyncio
    async def test_skips_stdio_endpoints(self):
        """Endpoints stdio (não-HTTP) não suportam tools/list HTTP."""
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE, "mcp_server": "/usr/local/bin/mcp-stdio"}]
        # Não deveria nem tentar conexão
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock()) as mock:
            await pre_discover_input_schemas(tools)
            mock.assert_not_called()
        assert "inputSchema" not in tools[0]

    @pytest.mark.asyncio
    async def test_skips_oauth2_tools(self):
        """Auth oauth2 envolve async token fetch — fora do escopo B.2."""
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE, "auth_requirements": "oauth2"}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock()) as mock:
            await pre_discover_input_schemas(tools)
            mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_mtls_tools(self):
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE, "auth_requirements": "mTLS"}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock()) as mock:
            await pre_discover_input_schemas(tools)
            mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_tools_list_no_op(self):
        from app.mcp.runtime import pre_discover_input_schemas
        # Não levanta
        await pre_discover_input_schemas([])


# ────────────────────────────────────────────────────────────────
# Unit: populates inputSchema
# ────────────────────────────────────────────────────────────────


class TestPopulatesInputSchema:
    @pytest.mark.asyncio
    async def test_populates_when_server_returns_schema(self):
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=CTX7_SERVER_RESPONSE)):
            await pre_discover_input_schemas(tools)
        # tool["inputSchema"] copiado do server
        assert "inputSchema" in tools[0]
        assert tools[0]["inputSchema"]["properties"]["libraryName"]["type"] == "string"
        assert "topic" in tools[0]["inputSchema"]["properties"]

    @pytest.mark.asyncio
    async def test_resolves_fuzzy_tool_name(self):
        """Quando SKILL declara nome curto (ex: 'docs') e server expõe
        nome prefixado (ex: 'context7_docs'), _resolve_tool_name mapeia."""
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE, "name": "docs"}]  # nome curto
        server_response = [{
            "name": "context7_docs",  # nome prefixado no server
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
        }]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=server_response)):
            await pre_discover_input_schemas(tools)
        # _resolve_tool_name fez o match — schema copiado
        assert "inputSchema" in tools[0]
        assert "q" in tools[0]["inputSchema"]["properties"]

    @pytest.mark.asyncio
    async def test_does_not_populate_when_server_returns_empty(self):
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=[])):
            await pre_discover_input_schemas(tools)
        # Nenhum schema descoberto — tool fica como estava
        assert "inputSchema" not in tools[0]

    @pytest.mark.asyncio
    async def test_skips_server_tool_without_properties(self):
        """Server retornou inputSchema sem properties — não vira function spec útil."""
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE}]
        server_response = [{
            "name": "Context 7",
            "inputSchema": {"type": "object"},  # sem properties
        }]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=server_response)):
            await pre_discover_input_schemas(tools)
        assert "inputSchema" not in tools[0]


# ────────────────────────────────────────────────────────────────
# Resilience: failures don't break the harness
# ────────────────────────────────────────────────────────────────


class TestResilience:
    @pytest.mark.asyncio
    async def test_connection_error_silenced(self):
        """Servidor MCP down → log warning, tool fica como estava."""
        from app.mcp.runtime import pre_discover_input_schemas
        tools = [{**CTX7_TOOL_BASE}]

        async def raise_conn(*args, **kwargs):
            raise ConnectionError("server down")

        with patch("app.mcp.runtime._discover_server_tools", new=raise_conn):
            # Não levanta — pre_discover engole o erro
            await pre_discover_input_schemas(tools)
        assert "inputSchema" not in tools[0]

    @pytest.mark.asyncio
    async def test_one_endpoint_failure_does_not_block_others(self):
        """asyncio.gather com return_exceptions — falha de 1 endpoint não derruba outros."""
        from app.mcp.runtime import pre_discover_input_schemas
        good_tool = {**CTX7_TOOL_BASE, "mcp_server": "https://good.com/mcp", "name": "good"}
        bad_tool = {**CTX7_TOOL_BASE, "mcp_server": "https://bad.com/mcp", "name": "bad"}
        tools = [good_tool, bad_tool]

        good_response = [{
            "name": "good",
            "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
        }]

        async def discover_mock(client, endpoint, headers):
            if "bad.com" in endpoint:
                raise ConnectionError("bad endpoint")
            return good_response

        with patch("app.mcp.runtime._discover_server_tools", side_effect=discover_mock):
            await pre_discover_input_schemas(tools)
        # good_tool ganhou schema; bad_tool ficou sem
        assert "inputSchema" in good_tool
        assert "inputSchema" not in bad_tool


# ────────────────────────────────────────────────────────────────
# Integration: pre_discover + build_openai_tools
# ────────────────────────────────────────────────────────────────


class TestIntegrationWithBuildOpenaiTools:
    @pytest.mark.asyncio
    async def test_after_prediscover_origin_is_tool_input_schema(self):
        """SKILL sem ## Inputs + pre_discover OK + build_openai_tools:
        origin agora é tool_input_schema (não legacy)."""
        from app.mcp.runtime import pre_discover_input_schemas, build_openai_tools
        tools = [{**CTX7_TOOL_BASE}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=CTX7_SERVER_RESPONSE)):
            await pre_discover_input_schemas(tools)
        # Sem skill_md — usa o que pre_discover populou
        openai = build_openai_tools(tools)
        spec = openai[0]
        # LLM agora vê {libraryName, topic, tokens} em vez de {operation, query}
        props = spec["function"]["parameters"]["properties"]
        assert set(props.keys()) == {"libraryName", "topic", "tokens"}
        assert spec["_schema_origin"] == "tool_input_schema"

    @pytest.mark.asyncio
    async def test_skill_inputs_still_wins_over_discovered_schema(self):
        """Precedência Onda B preservada: SKILL ## Inputs (explícito do autor)
        prevalece sobre tool.inputSchema (descoberto pelo servidor)."""
        from app.mcp.runtime import pre_discover_input_schemas, build_openai_tools
        tools = [{**CTX7_TOOL_BASE}]
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=CTX7_SERVER_RESPONSE)):
            await pre_discover_input_schemas(tools)
        # Agora SKILL declara ## Inputs com schema diferente
        skill_md = """---
id: x
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---
# X

## Inputs
```json
{"type":"object","required":["action"],"properties":{"action":{"type":"string"},"subject":{"type":"string"}}}
```
"""
        openai = build_openai_tools(tools, skill_md=skill_md)
        spec = openai[0]
        # SKILL prevalece — props são {action, subject}, não {libraryName, topic, tokens}
        props = spec["function"]["parameters"]["properties"]
        assert set(props.keys()) == {"action", "subject"}
        assert spec["_schema_origin"] == "skill_inputs"


# ────────────────────────────────────────────────────────────────
# Regressão arquitetural — SKILL sem ## Inputs + servidor com schema
# ────────────────────────────────────────────────────────────────


class TestRegressionContext7WithoutExplicitInputs:
    """O caso mais comum (e mais doloroso): SKILL gerada pelo Wizard SEM
    ## Inputs explícito. Pré-Onda B.2: LLM via {operation, query} fixo →
    bug Context7. Pós-Onda B.2: pre_discover popula tool.inputSchema do
    servidor → LLM vê schema REAL."""

    @pytest.mark.asyncio
    async def test_skill_without_inputs_now_gets_real_schema_via_discovery(self):
        from app.mcp.runtime import pre_discover_input_schemas, build_openai_tools
        # SKILL sem ## Inputs declarado (autor não escreveu schema)
        skill_md_no_inputs = """---
id: x
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---
# X

## Inputs
texto livre, sem JSON schema parseável

## Tool Bindings
- `t` (Context 7) — desc.
"""
        tools = [{**CTX7_TOOL_BASE}]

        # ANTES de B.2: build_openai_tools cai pra legacy
        legacy_spec = build_openai_tools(tools, skill_md=skill_md_no_inputs)
        assert legacy_spec[0]["_schema_origin"] == "legacy_operation_query"

        # APÓS pre_discover, tool ganha inputSchema; build_openai_tools usa
        with patch("app.mcp.runtime._discover_server_tools", new=AsyncMock(return_value=CTX7_SERVER_RESPONSE)):
            await pre_discover_input_schemas(tools)
        aware_spec = build_openai_tools(tools, skill_md=skill_md_no_inputs)
        assert aware_spec[0]["_schema_origin"] == "tool_input_schema"
        # LLM agora vê schema real do servidor — não mais comprime
        props = aware_spec[0]["function"]["parameters"]["properties"]
        assert "libraryName" in props
        # Causa raiz dos bugs Context7 NL agora resolvida MESMO sem ## Inputs explícito
