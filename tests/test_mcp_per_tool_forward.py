"""F3 (Per-tool D) — forwarding.

``execute_tool_call`` encaminha ao nome REAL do tool (``_mcp_real_name``) com
os ``arguments`` CRUS quando a função chamada foi construída no modo per-tool
(origin ``discovered_per_tool`` — ver ``build_per_tool_openai_functions``).
Sem match → caminho legado ``{operation, query}`` + ``_resolve_tool_name`` +
``_build_call_arguments`` intacto.

REDE DE SEGURANÇA: com a flag OFF não existem funções per-tool, então
``openai_tools`` nunca traz origin per-tool → ``execute_tool_call`` roda
byte-idêntico ao legado. Os testes legados abaixo provam isso.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.runtime import execute_tool_call, resolve_per_tool_call


# ── Função per-tool (como build_per_tool_openai_functions produz) ──
PER_TOOL_FUNCS = [
    {
        "type": "function",
        "function": {
            "name": "github_create_issue",
            "description": "Cria issue",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "labels": {"type": "array"},
                },
                "required": ["title"],
            },
        },
        "_mcp_server_tool": "GitHub MCP Server",
        "_mcp_real_name": "github_create_issue",
        "_schema_origin": "discovered_per_tool",
    },
]

GH_SERVER = [{
    "name": "GitHub MCP Server",
    "mcp_server": "https://gh.example/mcp",
    "auth_requirements": "",
    "auth_token": "",
}]

TAV_SERVER = [{
    "name": "Tavily MCP Server",
    "mcp_server": "https://tavily.example/mcp",
    "auth_requirements": "",
    "auth_token": "",
}]

TAV_SERVER_TOOLS = [{
    "name": "tavily_search",
    "inputSchema": {"type": "object", "required": ["query"],
                    "properties": {"query": {"type": "string"}}},
}]


class _FakeResp:
    def __init__(self, body, content_type="application/json"):
        self._body = body
        self.headers = {"content-type": content_type}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeMCPClient:
    """Stand-in p/ httpx.AsyncClient. Captura todos os POSTs."""
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts: list = []
        _FakeMCPClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        method = (json or {}).get("method")
        if method == "tools/call":
            return _FakeResp({"jsonrpc": "2.0", "id": 1,
                              "result": {"content": [{"type": "text", "text": "DONE"}]}})
        return _FakeResp({"jsonrpc": "2.0", "id": (json or {}).get("id"), "result": {}})


@pytest.fixture
def fake_mcp_http(monkeypatch):
    _FakeMCPClient.instances = []
    monkeypatch.setattr("app.mcp.runtime.httpx.AsyncClient", _FakeMCPClient)
    monkeypatch.setattr("app.core.secrets.read_secret", lambda v: v or "")
    yield _FakeMCPClient


def _last_tools_call(client):
    for p in reversed(client.posts):
        if (p["json"] or {}).get("method") == "tools/call":
            return p["json"]
    return None


# ── resolve_per_tool_call (unit) ──────────────────────────────────
def test_resolve_matches_per_tool_function():
    out = resolve_per_tool_call("github_create_issue", PER_TOOL_FUNCS)
    assert out == {"server_tool": "GitHub MCP Server", "real_name": "github_create_issue"}


def test_resolve_returns_none_for_unknown_name():
    assert resolve_per_tool_call("nope", PER_TOOL_FUNCS) is None


def test_resolve_ignores_non_per_tool_origin():
    legacy = [{"type": "function", "function": {"name": "Tavily_MCP_Server"},
               "_schema_origin": "legacy_operation_query"}]
    assert resolve_per_tool_call("Tavily_MCP_Server", legacy) is None


def test_resolve_none_when_no_openai_tools():
    assert resolve_per_tool_call("x", None) is None
    assert resolve_per_tool_call("x", []) is None


# ── execute_tool_call per-tool forwarding (e2e mockado) ───────────
@pytest.mark.asyncio
async def test_per_tool_forwards_real_name_and_raw_args(fake_mcp_http):
    raw_args = {"title": "Bug X", "body": "desc", "labels": ["bug", "p1"]}
    with patch("app.mcp.runtime._discover_server_tools",
               new=AsyncMock(return_value=[])) as disc:
        out = await execute_tool_call(
            "github_create_issue", raw_args, GH_SERVER, timeout=5,
            openai_tools=PER_TOOL_FUNCS,
        )
    assert out == "DONE"
    # per-tool NÃO re-descobre tools/list (economiza round-trip)
    disc.assert_not_called()
    call = _last_tools_call(fake_mcp_http.instances[-1])
    assert call["params"]["name"] == "github_create_issue"   # nome REAL
    assert call["params"]["arguments"] == raw_args            # args CRUS, sem remap


@pytest.mark.asyncio
async def test_per_tool_finds_server_by_metadata_not_function_name(fake_mcp_http):
    """A função se chama 'github_create_issue', mas o servidor é 'GitHub MCP
    Server' — o match é via _mcp_server_tool, não pelo nome da função."""
    with patch("app.mcp.runtime._discover_server_tools",
               new=AsyncMock(return_value=[])):
        out = await execute_tool_call(
            "github_create_issue", {"title": "x"}, GH_SERVER, timeout=5,
            openai_tools=PER_TOOL_FUNCS,
        )
    assert out == "DONE"
    assert fake_mcp_http.instances[-1].posts[-1]["url"] == "https://gh.example/mcp"


# ── Rede de segurança: legado intacto ─────────────────────────────
@pytest.mark.asyncio
async def test_legacy_path_unchanged_without_openai_tools(fake_mcp_http):
    """Sem openai_tools (flag OFF não gera per-tool funcs) → operation/query
    + _resolve_tool_name + _build_call_arguments, idêntico ao de hoje."""
    with patch("app.mcp.runtime._discover_server_tools",
               new=AsyncMock(return_value=TAV_SERVER_TOOLS)) as disc:
        out = await execute_tool_call(
            "Tavily_MCP_Server", {"operation": "search", "query": "foo"},
            TAV_SERVER, timeout=5,
        )
    assert out == "DONE"
    disc.assert_called()  # caminho legado DESCOBRE tools/list
    call = _last_tools_call(fake_mcp_http.instances[-1])
    assert call["params"]["name"] == "tavily_search"          # resolvido (fuzzy)
    assert call["params"]["arguments"] == {"query": "foo"}    # construído


@pytest.mark.asyncio
async def test_per_tool_origin_present_but_name_mismatch_is_legacy(fake_mcp_http):
    """openai_tools tem per-tool funcs (de GitHub), mas o tool_name chamado é
    do Tavily → não casa → cai no legado (resolve_per_tool_call=None)."""
    with patch("app.mcp.runtime._discover_server_tools",
               new=AsyncMock(return_value=TAV_SERVER_TOOLS)) as disc:
        out = await execute_tool_call(
            "Tavily_MCP_Server", {"operation": "search", "query": "bar"},
            TAV_SERVER, timeout=5, openai_tools=PER_TOOL_FUNCS,
        )
    assert out == "DONE"
    disc.assert_called()
    call = _last_tools_call(fake_mcp_http.instances[-1])
    assert call["params"]["name"] == "tavily_search"
    assert call["params"]["arguments"] == {"query": "bar"}
