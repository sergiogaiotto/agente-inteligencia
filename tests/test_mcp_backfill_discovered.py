"""F5 (Per-tool D) — backfill de ``discovered_tools``.

Descobre (``tools/list``) e persiste ``discovered_tools`` para conectores MCP
HTTP que predam a F1. Propriedades:
- **Idempotente**: pula quem já tem (salvo ``force=True``).
- **Best-effort por conector**: 1 falha não derruba os outros (asyncio.gather +
  return_exceptions).
- **Pula** stdio (sem tools/list HTTP) e auth complexa (oauth2/mTLS).
- **NÃO ativa nada**: só popula a coluna dormante que o builder per-tool consome
  quando ``MCP_PER_TOOL_ENABLED`` está ON.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.runtime import backfill_discovered_tools


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows
        self.updates: list = []  # (id, patch)

    async def find_all(self, limit=500, **kw):
        return [dict(r) for r in self._rows]

    async def update(self, _id, patch):
        self.updates.append((_id, patch))
        return True


def _tool(id, name, endpoint="https://x/mcp", auth="api_key", disc=None):
    return {"id": id, "name": name, "mcp_server": endpoint,
            "auth_requirements": auth, "auth_token": "", "discovered_tools": disc}


_SRV_TOOLS = [{"name": "tavily_search", "description": "busca",
               "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}]


@pytest.mark.asyncio
async def test_backfills_empty_connector_and_persists():
    repo = _FakeRepo([_tool("t1", "Tavily")])
    with patch("app.mcp.runtime._discover_connector_tools",
               new=AsyncMock(return_value=_SRV_TOOLS)):
        summary = await backfill_discovered_tools(repo)
    assert summary["backfilled"] == 1 and summary["failed"] == 0
    assert len(repo.updates) == 1
    _id, patch_ = repo.updates[0]
    assert _id == "t1"
    persisted = json.loads(patch_["discovered_tools"])
    assert persisted[0]["name"] == "tavily_search"


@pytest.mark.asyncio
async def test_skips_connector_that_already_has_discovered():
    repo = _FakeRepo([_tool("t1", "Tavily", disc=json.dumps(_SRV_TOOLS))])
    with patch("app.mcp.runtime._discover_connector_tools",
               new=AsyncMock(return_value=_SRV_TOOLS)) as disc:
        summary = await backfill_discovered_tools(repo)
    assert summary["skipped"] == 1 and summary["backfilled"] == 0
    disc.assert_not_called()  # nem tenta descobrir
    assert repo.updates == []


@pytest.mark.asyncio
async def test_force_refreshes_even_if_present():
    repo = _FakeRepo([_tool("t1", "Tavily", disc=json.dumps(_SRV_TOOLS))])
    with patch("app.mcp.runtime._discover_connector_tools",
               new=AsyncMock(return_value=_SRV_TOOLS)):
        summary = await backfill_discovered_tools(repo, force=True)
    assert summary["backfilled"] == 1
    assert len(repo.updates) == 1


@pytest.mark.asyncio
async def test_skips_stdio_and_complex_auth():
    repo = _FakeRepo([
        _tool("t1", "Stdio", endpoint="/usr/bin/mcp-thing", auth=""),
        _tool("t2", "OAuthy", auth="oauth2"),
        _tool("t3", "MtlsY", auth="mTLS"),
    ])
    with patch("app.mcp.runtime._discover_connector_tools",
               new=AsyncMock(return_value=_SRV_TOOLS)) as disc:
        summary = await backfill_discovered_tools(repo)
    assert summary["backfilled"] == 0
    assert summary["skipped"] == 3
    disc.assert_not_called()
    assert repo.updates == []


@pytest.mark.asyncio
async def test_empty_discovery_not_persisted():
    repo = _FakeRepo([_tool("t1", "Tavily")])
    with patch("app.mcp.runtime._discover_connector_tools",
               new=AsyncMock(return_value=[])):
        summary = await backfill_discovered_tools(repo)
    assert summary["backfilled"] == 0
    assert summary["skipped"] == 1  # descobriu vazio → nada a persistir
    assert repo.updates == []


@pytest.mark.asyncio
async def test_one_failure_does_not_block_others():
    repo = _FakeRepo([_tool("good", "Good", endpoint="https://good/mcp"),
                      _tool("bad", "Bad", endpoint="https://bad/mcp")])

    async def disc_mock(t, timeout):
        if "bad" in (t.get("mcp_server") or ""):
            raise ConnectionError("down")
        return _SRV_TOOLS

    with patch("app.mcp.runtime._discover_connector_tools", side_effect=disc_mock):
        summary = await backfill_discovered_tools(repo)
    assert summary["backfilled"] == 1
    assert summary["failed"] == 1
    assert [u[0] for u in repo.updates] == ["good"]


# ── Endpoint de manutenção ────────────────────────────────────────
@pytest.mark.asyncio
async def test_backfill_endpoint_returns_summary_and_flag(monkeypatch):
    """O POST /tools/backfill-discovered devolve o sumário + o estado da flag
    (transparência: o operador vê se o backfill já está ATIVO ou ainda dormente)."""
    from app.routes.dashboard import backfill_mcp_discovered, MCPBackfillRequest
    monkeypatch.setattr("app.mcp.runtime.backfill_discovered_tools",
                        AsyncMock(return_value={"backfilled": 2, "skipped": 1, "failed": 0, "total": 2}))
    monkeypatch.setattr("app.mcp.runtime.per_tool_enabled", lambda: False)
    out = await backfill_mcp_discovered(MCPBackfillRequest(force=False))
    assert out["backfilled"] == 2 and out["total"] == 2
    assert out["per_tool_enabled"] is False
