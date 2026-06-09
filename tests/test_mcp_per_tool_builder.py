"""Builder per-tool + gating do build_openai_tools (Per-tool D — F2).

Modelo per-tool: cada tool MCP descoberta vira SUA função OpenAI, com seu
inputSchema real (resolve a limitação do `{operation, query}` p/ tools de args
estruturados). Gated por `MCP_PER_TOOL_ENABLED` (default OFF): com flag OFF, o
`build_openai_tools` roda byte-idêntico ao legado — a prova de "nada quebra".
"""
from __future__ import annotations

import json

from app.mcp.runtime import build_per_tool_openai_functions, build_openai_tools

_DISCOVERED = [
    {"name": "tavily_search", "description": "Busca web",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "tavily_extract", "description": "Extrai conteúdo",
     "inputSchema": {"type": "object", "properties": {"urls": {"type": "array"}},
                     "required": ["urls"]}},
]


def test_builder_one_function_per_tool_with_real_schema():
    funcs = build_per_tool_openai_functions({"name": "Tavily MCP Server"}, _DISCOVERED)
    assert [f["function"]["name"] for f in funcs] == ["tavily_search", "tavily_extract"]
    # schema REAL por tool (não o {operation, query} genérico)
    assert set(funcs[0]["function"]["parameters"]["properties"]) == {"query", "max_results"}
    assert set(funcs[1]["function"]["parameters"]["properties"]) == {"urls"}
    # metadata p/ o forward (F3): nome real + servidor
    assert funcs[0]["_mcp_real_name"] == "tavily_search"
    assert funcs[0]["_schema_origin"] == "discovered_per_tool"


def test_flag_off_is_legacy_single_function(monkeypatch):
    """A REDE DE SEGURANÇA: flag OFF + discovered presente → caminho legado
    (1 função/servidor), idêntico ao de hoje. Nada de per-tool."""
    monkeypatch.delenv("MCP_PER_TOOL_ENABLED", raising=False)
    tool = {"name": "Tavily MCP Server", "operations": ["search", "extract"],
            "discovered_tools": json.dumps(_DISCOVERED)}
    out = build_openai_tools([tool])
    assert len(out) == 1
    assert out[0]["_schema_origin"] != "discovered_per_tool"


def test_flag_on_expands_per_tool(monkeypatch):
    monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "true")
    tool = {"name": "Tavily MCP Server", "discovered_tools": json.dumps(_DISCOVERED)}
    out = build_openai_tools([tool])
    assert [f["function"]["name"] for f in out] == ["tavily_search", "tavily_extract"]
    assert all(f["_schema_origin"] == "discovered_per_tool" for f in out)


def test_flag_on_but_no_discovered_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "true")
    tool = {"name": "Tavily MCP Server", "operations": ["search"]}  # sem discovered_tools
    out = build_openai_tools([tool])
    assert len(out) == 1
    assert out[0]["_schema_origin"] != "discovered_per_tool"
