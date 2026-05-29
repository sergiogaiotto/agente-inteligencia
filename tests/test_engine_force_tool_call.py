"""Cobre DeepAgentHarness._should_force_tool_call.

Regressão pro bug 'return False indentado dentro do if dentro do for'
(introduzido por refactor que desalinhou o return — funcionalmente
equivalente porque Python retorna None implícito, mas confunde leitor
e mascara a intenção).

Garante os 3 paths principais:
1. Verb match    → True (workflow tem verbo imperativo tipo "chame")
2. Primary token → True (workflow menciona o token base do nome da tool)
3. Sem match     → False **explícito** (não None implícito)

Bypassa __init__ com object.__new__ pra não tocar get_provider(),
que exige Settings reais.
"""
from __future__ import annotations

from app.agents.engine import DeepAgentHarness


def _make_harness(workflow: str, mcp_tools: list[dict]) -> DeepAgentHarness:
    """Cria DeepAgentHarness sem passar pelo __init__ pesado."""
    h = object.__new__(DeepAgentHarness)
    h.mcp_tools = mcp_tools
    h.config = {"_parsed_skill": {"workflow": workflow}}
    return h


class TestShouldForceToolCall:
    def test_verb_match_returns_true(self):
        """Workflow com verbo imperativo dispara force=True mesmo sem citar tool."""
        h = _make_harness(
            workflow="Para resolver, **chame** a busca externa antes de responder.",
            mcp_tools=[{"name": "Tavily Search"}],
        )
        assert h._should_force_tool_call() is True

    def test_primary_token_match_returns_true(self):
        """Workflow que menciona o token base do nome da tool dispara True."""
        h = _make_harness(
            workflow="Quando a pergunta exigir contexto da web, use o tavily pra buscar.",
            mcp_tools=[{"name": "Tavily MCP Server"}],
        )
        assert h._should_force_tool_call() is True

    def test_no_match_returns_false_explicitly(self):
        """Sem verbo nem nome de tool no workflow → False explícito (não None).

        Este é o teste que pegaria a regressão original: antes do fix,
        a função caía no fim do for e retornava None implícito.
        """
        h = _make_harness(
            workflow="Responda em português, seja conciso, use tom formal.",
            mcp_tools=[{"name": "Tavily MCP Server"}],
        )
        result = h._should_force_tool_call()
        assert result is False, (
            f"Esperado False explícito, veio {result!r}. "
            "Se veio None, o return False está indentado errado."
        )

    def test_no_mcp_tools_returns_false(self):
        """Sem tools configuradas, nunca força."""
        h = _make_harness(workflow="chame qualquer coisa", mcp_tools=[])
        assert h._should_force_tool_call() is False

    def test_empty_workflow_returns_false(self):
        """Workflow vazio no SKILL.md → não força."""
        h = _make_harness(workflow="", mcp_tools=[{"name": "Tavily"}])
        assert h._should_force_tool_call() is False
