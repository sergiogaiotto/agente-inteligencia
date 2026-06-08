"""Serialização do schema descoberto via MCP tools/list (Per-tool D — F1).

`serialize_discovered_tools` normaliza o resultado de `tools/list` para o JSON
que persiste em `tools.discovered_tools` — a fundação do D per-tool: a descoberta
salva o `inputSchema` REAL de cada tool, e a geração lê isso depois (sem rede na
hora de gerar). Aditivo e fail-safe: por enquanto nada consome a coluna (F2+).
"""
from __future__ import annotations

import json

from app.mcp.runtime import serialize_discovered_tools


def test_serializes_normalized_tools():
    discovered = [
        {"name": "tavily_search", "description": "search web",
         "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
        {"name": "tavily_extract", "description": "",
         "inputSchema": {"type": "object", "properties": {"urls": {"type": "array"}}}},
    ]
    parsed = json.loads(serialize_discovered_tools(discovered))
    assert [t["name"] for t in parsed] == ["tavily_search", "tavily_extract"]
    assert parsed[0]["inputSchema"]["properties"]["query"]["type"] == "string"
    assert parsed[1]["inputSchema"]["properties"]["urls"]["type"] == "array"


def test_skips_entries_without_name():
    discovered = [
        {"name": "", "inputSchema": {}},
        {"description": "sem nome"},
        {"name": "ok", "inputSchema": {"type": "object", "properties": {}}},
    ]
    parsed = json.loads(serialize_discovered_tools(discovered))
    assert [t["name"] for t in parsed] == ["ok"]


def test_input_schema_coerced_to_dict():
    # tools/list pode vir com inputSchema ausente/None — vira {} (nunca explode).
    parsed = json.loads(serialize_discovered_tools([{"name": "x"}, {"name": "y", "inputSchema": None}]))
    assert all(isinstance(t["inputSchema"], dict) for t in parsed)


def test_empty_or_none_returns_empty_list():
    assert serialize_discovered_tools([]) == "[]"
    assert serialize_discovered_tools(None) == "[]"
