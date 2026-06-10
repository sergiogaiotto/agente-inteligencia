"""MCP Streamable HTTP — captura e propagação do `Mcp-Session-Id`.

Bug (2026-06-10, reportado pelo usuário via log do agente `context7`): o
Context7 MCP Server respondia "falta um identificador de sessão válido". O
transporte MCP Streamable HTTP (spec 2025-03-26) é STATEFUL — o servidor
devolve um `Mcp-Session-Id` no header da resposta ao `initialize` e exige que o
cliente o ECOE em todas as chamadas seguintes. Os 5 caminhos HTTP do produto
faziam `initialize` mas DESCARTAVAM a resposta → sessão perdida → `tools/call`
rejeitado com 400 "No valid session ID provided".

Regra de projeto (definida com o usuário): a correção tem que ser GENÉRICA
(qualquer MCP, criado/usado a qualquer momento, funcionando de primeira) e SEM
regressão para servidores stateless. Por isso a captura virou fonte única
(`app/mcp/runtime.py::mcp_http_handshake`) reusada pelos 5 sites.

Cobertura:
- extract_session_id: header presente / ausente / grafia case-insensitive
- mcp_http_handshake: captura sessão + versão e ecoa no notifications/initialized
- mcp_http_handshake: stateless → headers idênticos ao base (sem regressão)
- mcp_http_handshake: falha no initialize é best-effort (não levanta)
- execute_tool_call (caminho REAL do agente): servidor stateful que rejeita
  qualquer chamada sem sessão → SUCESSO só é possível porque a sessão foi
  ecoada no tools/call
- _test_mcp_connection_impl (caminho de CADASTRO/descoberta): registrar um MCP
  stateful novo descobre as tools de primeira porque o tools/list leva a sessão
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ════════════════════════════════════════════════════════════════
# Fakes httpx — resposta e cliente que simulam um servidor MCP
# ════════════════════════════════════════════════════════════════


class FakeResponse:
    """Mímica mínima de httpx.Response para os parsers do runtime/dashboard."""

    def __init__(self, *, headers=None, json_body=None, text="", status_code=200,
                 content_type="application/json"):
        self._headers = dict(headers or {})
        self._headers.setdefault("content-type", content_type)
        self._json = json_body
        self.text = text
        self.status_code = status_code

    @property
    def headers(self):
        # dict simples (case-sensitive) — força os extratores a tentar as duas
        # grafias, como fariam contra um header lowercased por HTTP/2.
        return self._headers

    def json(self):
        if self._json is None:
            raise ValueError("sem corpo JSON")
        return self._json


class RecordingClient:
    """Cliente que grava (method, headers) de cada POST e delega a resposta a
    um handler. Suporta `async with`."""

    SESSION = "S1"

    def __init__(self, **kwargs):
        self.calls = []  # [(method, headers_snapshot)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, endpoint, json=None, headers=None):
        method = (json or {}).get("method")
        snap = dict(headers or {})
        self.calls.append((method, snap))
        return self._respond(method, snap)

    def headers_for(self, method):
        return [h for (m, h) in self.calls if m == method]

    # ── override em subclasses ──
    def _respond(self, method, headers):
        return FakeResponse(json_body={"jsonrpc": "2.0", "result": {}})


class StatefulServer(RecordingClient):
    """Servidor MCP que EXIGE o Mcp-Session-Id (como o Context7 atual): qualquer
    tools/list ou tools/call sem a sessão recebe o erro 'No valid session ID'."""

    TOOLS = [{
        "name": "get-library-docs",
        "description": "Docs de uma lib",
        "inputSchema": {"type": "object", "required": ["libraryName"],
                        "properties": {"libraryName": {"type": "string"}}},
    }]

    def _respond(self, method, headers):
        has_session = headers.get("Mcp-Session-Id") == self.SESSION
        if method == "initialize":
            return FakeResponse(
                headers={"Mcp-Session-Id": self.SESSION},
                json_body={"jsonrpc": "2.0", "id": 0, "result": {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {"name": "context7", "version": "1.0"}}},
            )
        if method == "notifications/initialized":
            return FakeResponse(json_body={"jsonrpc": "2.0", "result": {}})
        if method == "tools/list":
            if not has_session:
                return FakeResponse(json_body={"jsonrpc": "2.0", "id": 99, "error": {
                    "code": -32600, "message": "Bad Request: No valid session ID provided"}})
            return FakeResponse(json_body={"jsonrpc": "2.0", "id": 99,
                                           "result": {"tools": self.TOOLS}})
        if method == "tools/call":
            if not has_session:
                return FakeResponse(json_body={"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32600, "message": "Bad Request: No valid session ID provided"}})
            return FakeResponse(json_body={"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": "PATTERN: pagination cursor-based"}]}})
        return FakeResponse(json_body={"jsonrpc": "2.0", "result": {}})


def _factory(client_cls, sink):
    """Devolve um callable usável como httpx.AsyncClient(...) que instancia
    `client_cls`, guarda a instância em `sink` e a retorna."""
    def make(*args, **kwargs):
        c = client_cls(**kwargs)
        sink.append(c)
        return c
    return make


# ════════════════════════════════════════════════════════════════
# Unit — extract_session_id
# ════════════════════════════════════════════════════════════════


class TestExtractSessionId:
    def test_reads_header_canonical_case(self):
        from app.mcp.runtime import extract_session_id
        resp = FakeResponse(headers={"Mcp-Session-Id": "abc-123"})
        assert extract_session_id(resp) == "abc-123"

    def test_reads_header_lowercased(self):
        from app.mcp.runtime import extract_session_id
        resp = FakeResponse(headers={"mcp-session-id": "xyz-9"})
        assert extract_session_id(resp) == "xyz-9"

    def test_absent_returns_empty(self):
        from app.mcp.runtime import extract_session_id
        assert extract_session_id(FakeResponse(headers={})) == ""


# ════════════════════════════════════════════════════════════════
# Unit — mcp_http_handshake
# ════════════════════════════════════════════════════════════════


class TestHandshake:
    @pytest.mark.asyncio
    async def test_captures_and_echoes_session_id(self):
        from app.mcp.runtime import mcp_http_handshake
        client = StatefulServer()
        base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        out = await mcp_http_handshake(client, "https://x/mcp", base)

        # Sessão e versão negociada capturadas
        assert out["Mcp-Session-Id"] == "S1"
        assert out["MCP-Protocol-Version"] == "2025-03-26"
        # base não foi mutado
        assert "Mcp-Session-Id" not in base
        # notifications/initialized JÁ saiu com a sessão
        notif = client.headers_for("notifications/initialized")
        assert notif and notif[0].get("Mcp-Session-Id") == "S1"

    @pytest.mark.asyncio
    async def test_stateless_no_regression(self):
        """Servidor sem Mcp-Session-Id → headers idênticos ao base."""
        from app.mcp.runtime import mcp_http_handshake

        class Stateless(RecordingClient):
            def _respond(self, method, headers):
                # initialize sem header de sessão e sem protocolVersion no corpo
                return FakeResponse(json_body={"jsonrpc": "2.0", "result": {"serverInfo": {}}})

        base = {"Content-Type": "application/json"}
        out = await mcp_http_handshake(Stateless(), "https://x/mcp", base)
        assert out == base
        assert "Mcp-Session-Id" not in out
        assert "MCP-Protocol-Version" not in out

    @pytest.mark.asyncio
    async def test_initialize_failure_is_best_effort(self):
        """Falha no initialize não levanta — retorna base e ainda tenta o
        notifications/initialized (paridade com o `except: pass` histórico)."""
        from app.mcp.runtime import mcp_http_handshake

        class Flaky(RecordingClient):
            async def post(self, endpoint, json=None, headers=None):
                method = (json or {}).get("method")
                self.calls.append((method, dict(headers or {})))
                if method == "initialize":
                    raise ConnectionError("server down")
                return FakeResponse(json_body={"jsonrpc": "2.0", "result": {}})

        client = Flaky()
        out = await mcp_http_handshake(client, "https://x/mcp", {"a": "b"})
        assert out == {"a": "b"}
        assert client.headers_for("notifications/initialized")  # tentou mesmo assim


# ════════════════════════════════════════════════════════════════
# Integração — execute_tool_call (caminho REAL do agente)
# ════════════════════════════════════════════════════════════════


class TestExecuteToolCallStateful:
    @pytest.mark.asyncio
    async def test_stateful_server_succeeds_because_session_is_echoed(self):
        import app.mcp.runtime as rt

        rt._MCP_TOOLS_LIST_CACHE.clear()  # tools/list é cacheado por endpoint
        mcp_tools = [{
            "name": "Context7 MCP Server",
            "mcp_server": "https://mcp.context7.com/mcp",
            "operations": ["get-library-docs"],
            "auth_requirements": "",
            "auth_token": "",
        }]
        sink = []
        with patch("httpx.AsyncClient", _factory(StatefulServer, sink)):
            out = await rt.execute_tool_call(
                "Context7_MCP_Server",
                {"operation": "get-library-docs", "query": "openapi"},
                mcp_tools,
                timeout=10,
            )

        # SUCESSO só ocorre se a sessão foi ecoada — o servidor rejeita o
        # contrário. Prova end-to-end da correção no caminho do agente.
        assert "PATTERN: pagination" in out
        assert "No valid session ID" not in out

        client = sink[0]
        call_headers = client.headers_for("tools/call")
        assert call_headers and call_headers[0].get("Mcp-Session-Id") == "S1"


# ════════════════════════════════════════════════════════════════
# Integração — _test_mcp_connection_impl (caminho de CADASTRO/descoberta)
# ════════════════════════════════════════════════════════════════


class TestCadastroDiscoveryStateful:
    @pytest.mark.asyncio
    async def test_register_stateful_mcp_discovers_tools_first_try(self):
        """Registrar um MCP stateful novo e clicar 'Testar' descobre as tools de
        primeira — porque o tools/list agora leva o Mcp-Session-Id."""
        from app.routes.dashboard import _test_mcp_connection_impl, MCPTestRequest

        data = MCPTestRequest(endpoint="https://mcp.context7.com/mcp")
        sink = []
        with patch("httpx.AsyncClient", _factory(StatefulServer, sink)):
            res = await _test_mcp_connection_impl(data)

        assert res["success"] is True
        names = [t["name"] for t in res["discovered_tools"]]
        assert "get-library-docs" in names

        client = sink[0]
        listed = client.headers_for("tools/list")
        assert listed and listed[0].get("Mcp-Session-Id") == "S1"
