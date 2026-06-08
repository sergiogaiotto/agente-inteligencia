"""Linter: skill MCP com ## Inputs sem `operation` é ERROR (Fatia B, 2026-06-08).

Complementa o fix do wizard (#324 — geração força {operation, query}). O C só age
na GERAÇÃO; esta regra de lint cobre os caminhos que o C não pega: skill MCP
EDITADA à mão, criada via 'manual', ou importada. Sem `operation`, o runtime usa
o nome do servidor como tool → 'Unknown tool' → bolha vazia (bug "tavily a").

Precisão (baixo falso-positivo): só dispara quando há binding MCP E o ## Inputs
tem schema com properties MAS sem `operation`. Skill MCP SEM ## Inputs não erra —
o runtime cai no fallback legacy {operation, query} e funciona.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.skill_parser.linter import lint_skill

# Binding MCP no formato canônico do Wizard (parse_tool_bindings reconhece).
_MCP_TB = "- `tavily` (Tavily MCP Server) — Official Tavily MCP server for web search."
_NO_MCP_TB = "_Esta skill não usa ferramentas MCP._ (Nenhuma ferramenta MCP foi selecionada.)"

_INPUTS_DOMAIN = '```json\n{"type":"object","properties":{"address":{"type":"string"},"radius_meters":{"type":"integer"}}}\n```'
_INPUTS_OP = '```json\n{"type":"object","properties":{"operation":{"type":"string"},"query":{"type":"string"}}}\n```'


def _parsed(tool_bindings="", inputs=""):
    return SimpleNamespace(
        execution_mode="", api_bindings_parsed=[], output_contract="",
        tool_bindings=tool_bindings, inputs=inputs,
    )


def _codes(issues, sev=None):
    return [i["code"] for i in issues if sev is None or i["severity"] == sev]


def test_mcp_inputs_without_operation_is_error():
    issues = lint_skill(_parsed(tool_bindings=_MCP_TB, inputs=_INPUTS_DOMAIN))
    assert "mcp_inputs_missing_operation" in _codes(issues, "error")


def test_mcp_inputs_with_operation_ok():
    issues = lint_skill(_parsed(tool_bindings=_MCP_TB, inputs=_INPUTS_OP))
    assert "mcp_inputs_missing_operation" not in _codes(issues)


def test_mcp_without_inputs_section_no_error():
    # Sem ## Inputs → runtime usa fallback legacy {operation, query} → funciona.
    issues = lint_skill(_parsed(tool_bindings=_MCP_TB, inputs=""))
    assert "mcp_inputs_missing_operation" not in _codes(issues)


def test_non_mcp_inputs_without_operation_no_error():
    # Sem binding MCP (skill não-MCP) → o contrato operation/query não se aplica.
    issues = lint_skill(_parsed(tool_bindings=_NO_MCP_TB, inputs=_INPUTS_DOMAIN))
    assert "mcp_inputs_missing_operation" not in _codes(issues)
