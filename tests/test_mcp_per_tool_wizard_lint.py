"""F4a (Per-tool D) — modo per-tool dispensa o contrato {operation, query}.

Com ``MCP_PER_TOOL_ENABLED`` ON, cada tool MCP vira sua própria função com o
schema REAL (``build_openai_tools`` expande per-tool e ignora o ``## Inputs``
genérico). Logo, nesse modo:
- o Wizard NÃO força/reescreve o ``## Inputs`` para ``{operation, query}``;
- o Linter NÃO acusa ``mcp_inputs_missing_operation``.

Flag OFF (default) → comportamento idêntico ao de hoje (#324 / Fatia B). Os
testes legados (test_wizard_mcp_inputs_contract.py e
test_linter_mcp_inputs_operation.py) seguem cobrindo o caminho OFF.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.routes.wizard import _ensure_mcp_inputs_contract
from app.skill_parser.linter import lint_skill


MCP = [{"id": "t1", "name": "Tavily MCP Server", "description": "web search"}]

_BAD = '''---
name: tavily-a
---
## Purpose
Busca pontos turísticos.

## Inputs
```json
{"type":"object","properties":{"address":{"type":"string"}},"required":["address"]}
```

## Workflow
Chame a tool.
'''

_MCP_TB = "- `tavily` (Tavily MCP Server) — Official Tavily MCP server for web search."
_INPUTS_DOMAIN = '```json\n{"type":"object","properties":{"address":{"type":"string"}}}\n```'


def _parsed(tool_bindings="", inputs=""):
    return SimpleNamespace(
        execution_mode="", api_bindings_parsed=[], output_contract="",
        tool_bindings=tool_bindings, inputs=inputs,
    )


def _codes(issues, sev=None):
    return [i["code"] for i in issues if sev is None or i["severity"] == sev]


# ── Wizard ────────────────────────────────────────────────────────
def test_wizard_forces_contract_when_flag_off(monkeypatch):
    monkeypatch.delenv("MCP_PER_TOOL_ENABLED", raising=False)
    out = _ensure_mcp_inputs_contract(_BAD, MCP)
    assert '"operation"' in out and '"address"' not in out  # legado força o contrato


def test_wizard_leaves_inputs_untouched_when_flag_on(monkeypatch):
    monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "true")
    out = _ensure_mcp_inputs_contract(_BAD, MCP)
    assert out == _BAD            # per-tool: não reescreve o ## Inputs
    assert '"address"' in out     # schema de domínio preservado


# ── Linter ────────────────────────────────────────────────────────
def test_linter_flags_missing_operation_when_flag_off(monkeypatch):
    monkeypatch.delenv("MCP_PER_TOOL_ENABLED", raising=False)
    issues = lint_skill(_parsed(tool_bindings=_MCP_TB, inputs=_INPUTS_DOMAIN))
    assert "mcp_inputs_missing_operation" in _codes(issues, "error")


def test_linter_silent_when_flag_on(monkeypatch):
    monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "true")
    issues = lint_skill(_parsed(tool_bindings=_MCP_TB, inputs=_INPUTS_DOMAIN))
    assert "mcp_inputs_missing_operation" not in _codes(issues)
