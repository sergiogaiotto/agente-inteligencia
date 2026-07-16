"""SSOT dos predicados per-tool (39.x — item 3 PR5).

A métrica de cobertura só vale como GATE do PR6 se fizer a MESMA pergunta que
o runtime. Este arquivo pina isso: `per_tool_covered` é o gate de
`build_openai_tools`, e `per_tool_discovery_ready` é a prontidão — que IGNORA
o modo de propósito (senão a frota inteira mediria 0% com o toggle global OFF,
que é o default).
"""
from __future__ import annotations

import json

import pytest

from app.mcp.runtime import (
    build_openai_tools, per_tool_covered, per_tool_discovery_ready,
)


_DISC = json.dumps([
    {"name": "web_search", "inputSchema": {"type": "object",
                                           "properties": {"q": {"type": "string"}}}},
])


def _tool(**over):
    base = {"id": "t1", "name": "Tavily", "operations": ["search"],
            "mcp_server": "http://mcp:3001", "description": "busca",
            "discovered_tools": _DISC, "per_tool_mode": "inherit"}
    base.update(over)
    return base


class TestParidadeComOGateDoBuild:
    """A prova central: a métrica não pode divergir do que o runtime pratica."""

    @pytest.mark.parametrize("modo,disc,glob,esperado", [
        # modo  discovered  global   → per-tool?
        ("on", _DISC, False, True),    # override liga sem o global
        ("on", None, False, False),   # sem descoberta cai no legado (silencioso)
        ("off", _DISC, True, False),   # opt-out vence o global
        ("off", None, True, False),
        ("inherit", _DISC, True, True),
        ("inherit", _DISC, False, False),   # global OFF = default da casa
        ("inherit", None, True, False),
        ("xpto", _DISC, True, True),    # valor desconhecido → inherit (fail-safe)
    ])
    def test_per_tool_covered_identico_ao_gate_do_build(
        self, monkeypatch, modo, disc, glob, esperado,
    ):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1" if glob else "0")
        t = _tool(per_tool_mode=modo, discovered_tools=disc)

        assert per_tool_covered(t) is esperado

        # ...e o gate REAL concorda: per-tool expande na função descoberta,
        # legado emite a função única com o par {operation, query}.
        built = build_openai_tools([t])
        expandiu = any(f.get("_mcp_real_name") == "web_search" for f in built)
        assert expandiu is esperado, (
            "predicado divergiu do build_openai_tools — a métrica mentiria"
        )

    def test_discovery_ready_ignora_o_modo(self, monkeypatch):
        """O predicado da MÉTRICA responde 'sobrevive ao PR6?', não 'está ligado?'."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "0")
        t = _tool(per_tool_mode="off")
        assert per_tool_discovery_ready(t) is True    # tem descoberta → pronto
        assert per_tool_covered(t) is False           # mas hoje está no legado

    def test_on_sem_descoberta_nao_esta_pronto(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        t = _tool(per_tool_mode="on", discovered_tools=None)
        assert per_tool_discovery_ready(t) is False
        assert per_tool_covered(t) is False


class TestDescobertaVaziaOuInvalida:
    @pytest.mark.parametrize("raw", [
        None, "", "[]", "{", "não é json",
        json.dumps([{"description": "sem name"}]),   # entrada sem `name`
        json.dumps({"name": "não é lista"}),
    ])
    def test_nao_conta_como_pronto(self, raw):
        assert per_tool_discovery_ready(_tool(discovered_tools=raw)) is False

    def test_tool_none_nao_explode(self):
        assert per_tool_discovery_ready(None) is False
        assert per_tool_covered(None) is False


class TestCallSitesDelegamAoSSOT:
    def test_split_tools_by_mode_do_wizard(self, monkeypatch):
        from app.routes.wizard import _split_tools_by_mode
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        per, legacy = _split_tools_by_mode([
            _tool(id="a"), _tool(id="b", discovered_tools=None),
            _tool(id="c", per_tool_mode="off"),
        ])
        assert [t["id"] for t in per] == ["a"]
        assert [t["id"] for t in legacy] == ["b", "c"]
