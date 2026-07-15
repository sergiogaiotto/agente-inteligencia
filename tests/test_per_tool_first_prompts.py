"""Wizard/system-prompt PER-TOOL-FIRST (39.2.0 — item 3 PR3 do plano).

Antes, mesmo com per-tool ativo: o system prompt do engine ensinava
operation/query SEMPRE (instrução contraditória com as funções per-tool do
function spec), o wizard gerava skills novas no paradigma velho, e o
validador exigia operations onde o conceito não existe. Agora as três
superfícies seguem o MODO EFETIVO de cada conector (o mesmo critério do
gate de build: per_tool_enabled_for + discovered_tools).
"""
from __future__ import annotations

import json

from app.agents.engine import _build_mcp_tools_prompt_section
from app.routes.wizard import _mcp_block, _split_tools_by_mode


_DISCOVERED = json.dumps([
    {"name": "web_search", "description": "busca na web",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "extract_page", "description": "extrai página",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
])


def _tool(**over):
    base = {"id": "t1", "name": "Tavily", "description": "busca web",
            "operations": "search,extract", "discovered_tools": _DISCOVERED,
            "per_tool_mode": "inherit"}
    base.update(over)
    return base


class TestSplitPorModo:
    def test_criterio_igual_ao_gate_do_build(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        per, legacy = _split_tools_by_mode([
            _tool(per_tool_mode="on"),                       # piloto
            _tool(id="t2", per_tool_mode="inherit"),         # herda global OFF
            _tool(id="t3", per_tool_mode="on", discovered_tools=None),  # sem descoberta
        ])
        assert [t["id"] for t in per] == ["t1"]
        assert [t["id"] for t in legacy] == ["t2", "t3"]


class TestMcpBlockDoWizard:
    def test_legado_puro_texto_intacto(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        block = _mcp_block([_tool()])
        assert "REGRA CRÍTICA — operations" in block
        assert "operation=search" in block
        assert "per-tool" not in block

    def test_per_tool_orienta_nomes_reais(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        block = _mcp_block([_tool()])
        assert "`web_search`" in block and "`extract_page`" in block
        assert "NOME REAL" in block
        assert "NÃO use `operation=`/`query=`" in block
        assert "Chame a função `web_search`" in block
        # a orientação de operations do legado NÃO aparece
        assert "REGRA CRÍTICA — operations" not in block

    def test_misto_orienta_os_dois_mundos(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        block = _mcp_block([
            _tool(per_tool_mode="on"),
            _tool(id="t2", name="Context7", per_tool_mode="inherit",
                  operations="docs,code"),
        ])
        assert "NOME REAL" in block                      # bloco per-tool
        assert "REGRA CRÍTICA — operations" in block     # bloco legado
        assert "`Context7`" in block


class TestSystemPromptDoEngine:
    _LEGACY_HOW = ("**Como chamar**: use o function call com `operation` "
                   "(uma das operações listadas acima) e `query`")

    def _per_fn(self, name="web_search", server="Tavily"):
        return {"type": "function",
                "function": {"name": name, "description": "busca na web"},
                "_schema_origin": "discovered_per_tool",
                "_mcp_server_tool": server, "_mcp_real_name": name}

    def test_legado_byte_identico(self):
        mcp = [{"name": "Tavily", "operations": ["search"],
                "description": "busca", "mcp_server": "http://x"}]
        section = _build_mcp_tools_prompt_section(mcp, [])
        assert self._LEGACY_HOW in section
        assert "função per-tool" not in section
        assert "- **Tavily** (function `Tavily`, operações: search)" in section

    def test_per_tool_puro_sem_operation_query(self):
        mcp = [{"name": "Tavily", "operations": ["search"],
                "description": "busca", "mcp_server": "http://x"}]
        section = _build_mcp_tools_prompt_section(mcp, [self._per_fn()])
        assert "- **web_search** (função per-tool" in section
        assert "NÃO existe `operation`/`query`" in section
        assert self._LEGACY_HOW not in section
        # o conector coberto por funções per-tool não reaparece como legado
        assert "operações: search" not in section

    def test_misto_ensina_os_dois(self):
        mcp = [{"name": "Tavily", "operations": ["search"], "description": "",
                "mcp_server": ""},
               {"name": "Context7", "operations": ["docs"], "description": "",
                "mcp_server": ""}]
        section = _build_mcp_tools_prompt_section(mcp, [self._per_fn()])
        assert "- **web_search** (função per-tool" in section
        assert "operações: docs" in section              # legado listado
        assert "NÃO use `operation`/`query` nelas" in section
        assert "Para as demais ferramentas" in section


class TestValidatorPerToolAware:
    def _validate(self, tools):
        from app.skill_parser.parser import parse_skill_md
        from app.skill_parser.wizard_validator import validate_generated_skill
        skill_md = (
            "---\nname: X\nkind: subagent\n---\n"
            "## Purpose\nBuscar.\n"
            "## Workflow\n1. Chame a tool `Tavily` com operation=foo e query=<x>.\n"
            "## Tool Bindings\n- `t1` (Tavily)\n"
        )
        parsed = parse_skill_md(skill_md)
        return validate_generated_skill(parsed, bindings={
            "mcp_tools": tools, "rag_sources": [], "data_tables": [],
            "api_endpoints": [],
        }, raw_md=skill_md)

    def test_per_tool_nao_exige_operations(self, monkeypatch):
        """Conector per-tool fora da agregação → operation.invented não
        dispara mesmo com operation=foo no Workflow (paridade F4a)."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        result = self._validate([_tool()])
        rules = [v.rule for v in getattr(result, "violations", result)
                 if hasattr(v, "rule")]
        assert not any(r.startswith("operation.") for r in rules)

    def test_legado_continua_validando(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        result = self._validate([_tool()])
        rules = [v.rule for v in getattr(result, "violations", result)
                 if hasattr(v, "rule")]
        assert "operation.invented" in rules
