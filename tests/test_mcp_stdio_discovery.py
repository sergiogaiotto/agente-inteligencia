"""Descoberta stdio PERSISTIDA (39.1.0 — item 3 PR2 do plano).

Antes: o teste de conexão stdio retornava ANTES do bloco de persist (que
vivia só no happy-path HTTP) e o backfill pulava não-HTTP — conector stdio
nunca entrava no modo per-tool, mesmo com o teste verde. run_stdio_session
(action='test') sempre devolveu discovered_tools; faltava persistir nos
dois pontos. É o pré-requisito duro da depreciação do legado
{operation, query} (item 3 PR6): sem descoberta, remover o fallback
deixaria o conector sem função nenhuma.
"""
from __future__ import annotations

import json

import pytest

import app.routes.dashboard as dash
from app.mcp import runtime


_DISCOVERED = [{"name": "fs_read", "description": "lê arquivo",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}]


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


class TestTesteDeConexaoStdioPersiste:
    def _req(self, **over):
        base = {"endpoint": "npx -y @modelcontextprotocol/server-filesystem /tmp",
                "tool_id": "t-stdio"}
        base.update(over)
        return dash.MCPTestRequest(**base)

    @pytest.fixture(autouse=True)
    def _no_secret_lookup(self, monkeypatch):
        # _resolve_secrets_from_tool_id consulta o pool real quando há tool_id
        # (PR #232) — irrelevante p/ stdio sem auth; noop nos testes.
        async def _noop(data):
            return None
        monkeypatch.setattr(dash, "_resolve_secrets_from_tool_id", _noop)

    @pytest.mark.asyncio
    async def test_sucesso_persiste_discovered(self, monkeypatch):
        updates = {}

        async def _update(tid, data):
            updates[tid] = data

        monkeypatch.setattr(
            "app.mcp.runtime.run_stdio_session",
            _async({"success": True, "server_name": "fs v1",
                    "discovered_tools": _DISCOVERED,
                    "details": "Stdio MCP conectado (1 ferramentas)"}),
        )
        monkeypatch.setattr("app.core.database.tools_repo.update", _update)

        result = await dash._test_mcp_connection_impl(self._req())
        assert result["success"] is True
        assert json.loads(updates["t-stdio"]["discovered_tools"])[0]["name"] == "fs_read"

    @pytest.mark.asyncio
    async def test_falha_nao_persiste(self, monkeypatch):
        updates = {}

        async def _update(tid, data):
            updates[tid] = data

        monkeypatch.setattr(
            "app.mcp.runtime.run_stdio_session",
            _async({"success": False, "details": "Comando 'npx' não encontrado"}),
        )
        monkeypatch.setattr("app.core.database.tools_repo.update", _update)
        result = await dash._test_mcp_connection_impl(self._req())
        assert result["success"] is False and updates == {}

    @pytest.mark.asyncio
    async def test_sem_tool_id_nao_persiste(self, monkeypatch):
        updates = {}

        async def _update(tid, data):
            updates[tid] = data

        monkeypatch.setattr(
            "app.mcp.runtime.run_stdio_session",
            _async({"success": True, "discovered_tools": _DISCOVERED}),
        )
        monkeypatch.setattr("app.core.database.tools_repo.update", _update)
        result = await dash._test_mcp_connection_impl(self._req(tool_id=None))
        assert result["success"] is True and updates == {}

    @pytest.mark.asyncio
    async def test_persist_falhando_nao_quebra_o_teste(self, monkeypatch):
        """Mesmo contrato best-effort do HTTP: persist morto → teste segue
        devolvendo discovered_tools (e loga warning)."""
        async def _boom(*a, **k):
            raise RuntimeError("db fora")

        monkeypatch.setattr(
            "app.mcp.runtime.run_stdio_session",
            _async({"success": True, "discovered_tools": _DISCOVERED}),
        )
        monkeypatch.setattr("app.core.database.tools_repo.update", _boom)
        result = await dash._test_mcp_connection_impl(self._req())
        assert result["success"] is True
        assert result["discovered_tools"] == _DISCOVERED


class TestBackfillCobreStdio:
    class _Repo:
        def __init__(self, rows):
            self.rows = rows
            self.updates = {}

        async def find_all(self, limit=500):
            return self.rows

        async def update(self, tid, data):
            self.updates[tid] = data

    @pytest.mark.asyncio
    async def test_stdio_backfilled(self, monkeypatch):
        repo = self._Repo([{"id": "t1", "mcp_server": "npx -y servidor-mcp",
                            "auth_requirements": "", "discovered_tools": None}])
        seen = {}

        async def _stdio(command, action="test", timeout=90, **kw):
            seen.update(command=command, timeout=timeout)
            return {"success": True, "discovered_tools": _DISCOVERED}

        monkeypatch.setattr(runtime, "run_stdio_session", _stdio)
        summary = await runtime.backfill_discovered_tools(repo)
        assert summary["backfilled"] == 1 and summary["failed"] == 0
        assert seen["command"] == "npx -y servidor-mcp"
        assert seen["timeout"] == 90  # 1ª execução do npx baixa o pacote
        assert json.loads(repo.updates["t1"]["discovered_tools"])[0]["name"] == "fs_read"

    @pytest.mark.asyncio
    async def test_stdio_falho_conta_como_empty(self, monkeypatch):
        repo = self._Repo([{"id": "t1", "mcp_server": "npx quebrado",
                            "auth_requirements": "", "discovered_tools": None}])
        monkeypatch.setattr(runtime, "run_stdio_session",
                            _async({"success": False, "details": "boom"}))
        summary = await runtime.backfill_discovered_tools(repo)
        assert summary["backfilled"] == 0 and summary["skipped"] == 1
        assert repo.updates == {}

    @pytest.mark.asyncio
    async def test_sem_endpoint_pulado_e_idempotencia_preservada(self, monkeypatch):
        repo = self._Repo([
            {"id": "vazio", "mcp_server": "", "auth_requirements": "",
             "discovered_tools": None},
            {"id": "ja-tem", "mcp_server": "npx x", "auth_requirements": "",
             "discovered_tools": json.dumps(_DISCOVERED)},
        ])
        monkeypatch.setattr(runtime, "run_stdio_session",
                            _async({"success": True, "discovered_tools": _DISCOVERED}))
        summary = await runtime.backfill_discovered_tools(repo)
        assert summary["skipped"] == 2 and summary["total"] == 0
        assert repo.updates == {}
