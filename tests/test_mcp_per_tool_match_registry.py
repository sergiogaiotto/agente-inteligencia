"""Per-tool (D) — regressão do elo match_with_registry → build_openai_tools.

GAP (achado 2026-06-09 via smoke E2E real): `match_with_registry` enriquecia o
dict da tool com name/mcp_server/auth/operations… mas NÃO copiava
`discovered_tools`. Resultado: no fluxo REAL
engine → match_with_registry → build_openai_tools, o gate per-tool nunca
enxergava `discovered_tools` e caía no legado {operation, query} — mesmo com
`MCP_PER_TOOL_ENABLED` ON e o conector já descoberto.

Os unit tests da F2 exercitavam `build_openai_tools` com um dict que JÁ trazia
`discovered_tools`, então o gap passou batido. Estes testes cobrem o fluxo ponta
a ponta: sem o fix (uma linha em match_with_registry), os dois primeiros falham.
"""
from __future__ import annotations

import json

import pytest

from app.mcp.runtime import match_with_registry, build_openai_tools

_DISCOVERED = [
    {"name": "tavily_search", "description": "Busca web",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "max_results": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "tavily_extract", "description": "Extrai conteúdo",
     "inputSchema": {"type": "object", "properties": {"urls": {"type": "array"}},
                     "required": ["urls"]}},
]


class _FakeToolsRepo:
    """Espelha o Repository('tools').find_all (SELECT * → todas as colunas,
    inclusive discovered_tools)."""

    def __init__(self, rows):
        self._rows = rows

    async def find_all(self, limit=100, offset=0, **kw):
        return [dict(r) for r in self._rows]


def _registry_row():
    return {
        "id": "uuid-tavily",
        "name": "Tavily MCP Server",
        "mcp_server": "https://mcp.tavily.com/mcp/",
        "description": "Tavily web search",
        "discovered_tools": json.dumps(_DISCOVERED),
        "auth_requirements": "api_key",
        "auth_token": "",
        "auth_config": "{}",
        "operations": "[]",
    }


@pytest.mark.asyncio
async def test_match_with_registry_propagates_discovered_tools():
    """O elo que faltava: o dict enriquecido precisa carregar discovered_tools."""
    repo = _FakeToolsRepo([_registry_row()])
    enriched = await match_with_registry([{"name": "Tavily MCP Server"}], repo)
    assert len(enriched) == 1
    assert enriched[0].get("db_id") == "uuid-tavily"
    disc = enriched[0].get("discovered_tools")
    assert disc, "match_with_registry deve propagar discovered_tools (regressão E2E)"
    assert [d["name"] for d in json.loads(disc)] == ["tavily_search", "tavily_extract"]


@pytest.mark.asyncio
async def test_real_flow_expands_per_tool_when_flag_on(monkeypatch):
    """Fluxo real ponta a ponta: match_with_registry → build_openai_tools.
    Flag ON + conector descoberto → expande 1 função/tool com schema REAL."""
    monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "true")
    repo = _FakeToolsRepo([_registry_row()])
    enriched = await match_with_registry([{"name": "Tavily MCP Server"}], repo)
    out = build_openai_tools(enriched)
    assert [f["function"]["name"] for f in out] == ["tavily_search", "tavily_extract"]
    assert all(f["_schema_origin"] == "discovered_per_tool" for f in out)
    # schema REAL por tool — o ganho do per-tool vs {operation, query} comprimido
    assert set(out[0]["function"]["parameters"]["properties"]) == {"query", "max_results"}


@pytest.mark.asyncio
async def test_real_flow_legacy_when_flag_off(monkeypatch):
    """Rede de segurança: com a flag OFF, o mesmo fluxo segue legado (1 função),
    mesmo com discovered_tools propagado. Garante que o fix é inerte sob flag OFF."""
    monkeypatch.delenv("MCP_PER_TOOL_ENABLED", raising=False)
    repo = _FakeToolsRepo([_registry_row()])
    enriched = await match_with_registry([{"name": "Tavily MCP Server"}], repo)
    out = build_openai_tools(enriched)
    assert len(out) == 1
    assert out[0]["_schema_origin"] != "discovered_per_tool"
