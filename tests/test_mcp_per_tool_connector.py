"""MCP per-tool POR CONECTOR (39.0.0 — item 3 PR1 do plano).

`tools.per_tool_mode` tri-state ('inherit'|'on'|'off') COMPÕE com o toggle
global MCP_PER_TOOL_ENABLED: 'on' força per-tool no conector mesmo com a
frota OFF (piloto); 'off' segura o conector no legado {operation, query}
mesmo com a frota ON (opt-out pontual); 'inherit' (default) = comportamento
pré-39.0.0 byte-idêntico. Decisão única em per_tool_enabled_for; o modo
viaja do Registry via match_with_registry.
"""
from __future__ import annotations

import json

import pytest

from app.mcp import runtime


_DISCOVERED = json.dumps([
    {"name": "web_search", "description": "busca",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}},
                     "required": ["q"]}},
])


def _tool(**over):
    base = {"name": "Tavily", "mcp_server": "http://mcp:3001",
            "description": "busca web", "operations": ["search"],
            "discovered_tools": _DISCOVERED}
    base.update(over)
    return base


class TestPerToolEnabledFor:
    @pytest.mark.parametrize("mode,global_on,expected", [
        ("on", False, True),      # piloto: liga só este conector
        ("on", True, True),
        ("off", True, False),     # opt-out: segura este conector no legado
        ("off", False, False),
        ("inherit", True, True),  # default herda a frota
        ("inherit", False, False),
        ("", True, True),         # ausente/legado = inherit
        (None, False, False),
        ("banana", True, True),   # valor desconhecido → fail-safe inherit
    ])
    def test_matriz(self, monkeypatch, mode, global_on, expected):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1" if global_on else "")
        assert runtime.per_tool_enabled_for(_tool(per_tool_mode=mode)) is expected

    def test_tool_none_herda(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        assert runtime.per_tool_enabled_for(None) is True


class TestBuildGatePorConector:
    def _origins(self, tools):
        return [t.get("_schema_origin") for t in runtime.build_openai_tools(tools)]

    def test_on_com_global_off_expande_per_tool(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        origins = self._origins([_tool(per_tool_mode="on")])
        assert origins == ["discovered_per_tool"]

    def test_off_com_global_on_fica_no_legado(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        origins = self._origins([_tool(per_tool_mode="off")])
        assert origins == ["legacy_operation_query"]

    def test_inherit_segue_o_global(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        assert self._origins([_tool(per_tool_mode="inherit")]) == ["discovered_per_tool"]
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        assert self._origins([_tool(per_tool_mode="inherit")]) == ["legacy_operation_query"]

    def test_mix_de_conectores_na_mesma_chamada(self, monkeypatch):
        """Frota OFF com 1 conector em piloto: só ele expande."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        tools = [_tool(name="Piloto", per_tool_mode="on"),
                 _tool(name="Legado", per_tool_mode="inherit")]
        origins = self._origins(tools)
        assert origins == ["discovered_per_tool", "legacy_operation_query"]

    def test_on_sem_discovered_cai_no_legado(self, monkeypatch):
        """'on' sem descoberta persistida não pode deixar o agente sem tools
        — cai no legado (fail-open deliberado, igual ao global)."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "")
        origins = self._origins([_tool(per_tool_mode="on", discovered_tools=None)])
        assert origins == ["legacy_operation_query"]


class TestPropagacaoERegistro:
    @pytest.mark.asyncio
    async def test_match_with_registry_propaga_o_modo(self):
        class _Repo:
            async def find_all(self, limit=200):
                return [{"id": "t1", "name": "Tavily", "mcp_server": "http://x",
                         "description": "", "discovered_tools": _DISCOVERED,
                         "per_tool_mode": "on", "auth_requirements": "",
                         "auth_token": "", "auth_config": "{}", "operations": "[]"}]

        enriched = await runtime.match_with_registry(
            [{"name": "Tavily", "display_name": ""}], _Repo()
        )
        assert enriched[0]["per_tool_mode"] == "on"

    def test_migracao_idempotente_registrada(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        assert ("ALTER TABLE tools ADD COLUMN IF NOT EXISTS per_tool_mode "
                "TEXT DEFAULT 'inherit'") in migs

    def test_schemas_carregam_o_campo(self):
        from app.models.schemas import ToolCreate, ToolUpdate
        assert ToolCreate(name="x").per_tool_mode == "inherit"
        assert "per_tool_mode" in ToolUpdate.model_fields

    def test_form_da_ui_tem_o_seletor(self):
        from pathlib import Path
        src = Path("app/templates/pages/tools.html").read_text(encoding="utf-8")
        assert 'data-testid="tool-per-tool-mode"' in src
        assert 'x-model="form.per_tool_mode"' in src
        assert "per_tool_mode: tool.per_tool_mode||'inherit'" in src
