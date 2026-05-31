"""PR #231 — eventos estruturados para os endpoints de teste de MCP e
API connector.

Sintoma reportado: operador testa MCP e vê 401, mas ao ir em Observabilidade
> Manutenção de Logs não acha nada sobre o teste. Os logs http.request /
http.response do middleware só dizem que houve um POST 200 — não revelam
que o teste lógico falhou.

Fix: cada um dos 3 endpoints emite um evento canônico no app.log para que
o Log Viewer 2.0 (PR #221) consiga filtrar por `event` e mostrar histórico.

Endpoints cobertos:
- POST /api/v1/tools/test                  → mcp.test.completed|failed
- POST /api/v1/tools/execute               → mcp.execute.completed|failed
- POST /api/v1/api-connectors/{id}/test    → api_connector.test.completed|failed

Estratégia dos testes: capturar `caplog` ao invocar via TestClient. Mocka o
trabalho real (httpx ou repos) para que o endpoint sempre devolva um resultado
controlado e podemos verificar a forma do log emitido.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── Helpers ──────────────────────────────────────────────────


def _records_with_event(caplog, event: str) -> list:
    return [r for r in caplog.records if getattr(r, "event", "") == event]


# ─── 1. /tools/test ────────────────────────────────────────────


class TestMCPTestConnectionEmitsEvent:
    def _mock_impl(self, monkeypatch, return_value: dict):
        """Substitui _test_mcp_connection_impl pra evitar httpx real."""
        from app.routes import dashboard

        async def fake(data):
            return return_value

        monkeypatch.setattr(dashboard, "_test_mcp_connection_impl", fake)

    def _app(self):
        from app.routes.dashboard import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_success_emits_mcp_test_completed_info_level(
        self, monkeypatch, caplog,
    ):
        self._mock_impl(monkeypatch, {
            "success": True,
            "details": "MCP Server conectado (JSON-RPC)",
            "latency": 234,
            "server_name": "tavily-mcp v1.0.0",
            "discovered_tools": [{"name": "search"}, {"name": "extract"}],
            "recommendations": ["Servidor: tavily-mcp v1.0.0", "2 ferramenta(s)"],
        })
        client = self._app()
        with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/test", json={
                "endpoint": "https://mcp.tavily.com/mcp",
                "auth_type": "api_key",
                "auth_token": "fake",
            })
        assert r.status_code == 200
        recs = _records_with_event(caplog, "mcp.test.completed")
        assert len(recs) == 1, f"Esperava 1 evento mcp.test.completed; recebi {len(recs)}"
        rec = recs[0]
        assert rec.levelno == logging.INFO
        assert rec.mcp_endpoint == "https://mcp.tavily.com/mcp"
        assert rec.auth_type == "api_key"
        assert rec.success is True
        assert rec.discovered_tools_count == 2
        assert rec.server_name == "tavily-mcp v1.0.0"
        assert rec.latency_ms == 234
        # `duration_ms` calculado pelo wrapper
        assert isinstance(rec.duration_ms, (int, float))

    def test_failure_emits_mcp_test_failed_warning_level(
        self, monkeypatch, caplog,
    ):
        self._mock_impl(monkeypatch, {
            "success": False,
            "details": "HTTP 401",
            "latency": 414,
            "server_name": None,
            "discovered_tools": [],
            "recommendations": ["HTTP 401", "Autenticação necessária. Configure Auth."],
        })
        client = self._app()
        with caplog.at_level(logging.WARNING, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/test", json={
                "endpoint": "https://mcp.tavily.com/mcp",
                "auth_type": "api_key",
                "auth_token": "",
            })
        assert r.status_code == 200  # 200 do app — falha lógica
        recs = _records_with_event(caplog, "mcp.test.failed")
        assert len(recs) == 1
        rec = recs[0]
        assert rec.levelno == logging.WARNING
        assert rec.success is False
        assert "401" in rec.details
        assert rec.recommendations_count == 2

    def test_log_does_not_include_auth_token(
        self, monkeypatch, caplog,
    ):
        """Token nunca pode entrar no log — leak via Loki/Grafana/backups."""
        self._mock_impl(monkeypatch, {
            "success": True, "details": "", "latency": 50,
            "server_name": "x", "discovered_tools": [], "recommendations": [],
        })
        client = self._app()
        secret = "tvly-supersecret-123-DO-NOT-LEAK"
        with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/test", json={
                "endpoint": "https://x", "auth_type": "api_key",
                "auth_token": secret,
            })
        assert r.status_code == 200
        for rec in caplog.records:
            assert secret not in str(rec.__dict__), (
                f"Token vazado no log record: {rec.__dict__}"
            )

    def test_log_uses_event_key_not_reserved_logrecord_name(
        self, monkeypatch, caplog,
    ):
        """Regressão para PR #225: key reservada `name` no extra={} levanta
        KeyError em makeRecord. Verifica que o log foi emitido — se a key
        fosse `name` em vez de `mcp_endpoint`, o logger explodiria."""
        self._mock_impl(monkeypatch, {
            "success": True, "details": "", "latency": 1,
            "server_name": "x", "discovered_tools": [], "recommendations": [],
        })
        client = self._app()
        with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/test", json={
                "endpoint": "https://x", "auth_type": "", "auth_token": "",
            })
        assert r.status_code == 200
        assert len(_records_with_event(caplog, "mcp.test.completed")) == 1


# ─── 2. /tools/execute ─────────────────────────────────────────


class TestMCPExecuteEmitsEvent:
    def _mock_impl(self, monkeypatch, return_value: dict):
        from app.routes import dashboard
        async def fake(data):
            return return_value
        monkeypatch.setattr(dashboard, "_execute_mcp_tool_impl", fake)

    def _app(self):
        from app.routes.dashboard import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_success_emits_execute_completed(self, monkeypatch, caplog):
        self._mock_impl(monkeypatch, {
            "success": True, "data": "result payload",
            "latency": 187,
        })
        client = self._app()
        with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/execute", json={
                "endpoint": "https://mcp.tavily.com/mcp",
                "tool_name": "search",
                "arguments": {"query": "fatura"},
                "auth_type": "api_key",
                "auth_token": "x",
            })
        assert r.status_code == 200
        recs = _records_with_event(caplog, "mcp.execute.completed")
        assert len(recs) == 1
        rec = recs[0]
        assert rec.tool_name == "search"
        assert rec.success is True
        assert rec.args_size_bytes > 0   # {"query":"fatura"} tem bytes
        assert rec.data_size_bytes == len("result payload")
        assert rec.latency_ms == 187

    def test_failure_emits_execute_failed_warning(self, monkeypatch, caplog):
        self._mock_impl(monkeypatch, {
            "success": False, "error": "tool not found", "latency": 12,
        })
        client = self._app()
        with caplog.at_level(logging.WARNING, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/execute", json={
                "endpoint": "https://x", "tool_name": "ghost", "arguments": {},
                "auth_type": "", "auth_token": "",
            })
        assert r.status_code == 200
        recs = _records_with_event(caplog, "mcp.execute.failed")
        assert len(recs) == 1
        assert recs[0].levelno == logging.WARNING
        assert recs[0].error == "tool not found"

    def test_log_does_not_include_arguments_or_data(
        self, monkeypatch, caplog,
    ):
        """args e data podem conter PII / business data; logger só guarda size."""
        sensitive = "cpf:123.456.789-00 cliente premium"
        self._mock_impl(monkeypatch, {
            "success": True, "data": sensitive, "latency": 5,
        })
        client = self._app()
        with caplog.at_level(logging.INFO, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/execute", json={
                "endpoint": "https://x", "tool_name": "x",
                "arguments": {"sensitive_input": sensitive},
                "auth_type": "", "auth_token": "",
            })
        assert r.status_code == 200
        for rec in caplog.records:
            assert sensitive not in str(rec.__dict__)


# ─── 3. /api-connectors/{id}/test ──────────────────────────────


class TestApiConnectorTestEmitsEvent:
    def _app(self, monkeypatch, connector_row, fake_response_status=None):
        """Mocka tudo que toca rede + repos. fake_response_status define o
        que o httpx fake devolveria."""
        from app.routes import api_connectors

        def fake_repos():
            class _Repo:
                async def find_by_id(self_inner, cid):
                    return connector_row if cid == connector_row["id"] else None
            return _Repo(), None, None

        monkeypatch.setattr(api_connectors, "_repos", fake_repos)
        monkeypatch.setattr(api_connectors, "_build_auth_headers", lambda c: {})
        monkeypatch.setattr(api_connectors, "_client_kwargs", lambda c: {})

        # Mocka httpx.AsyncClient
        class _FakeResp:
            def __init__(self, status):
                self.status_code = status

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, url):
                if fake_response_status is None:
                    import httpx
                    raise httpx.ConnectError("no")
                return _FakeResp(fake_response_status)

        monkeypatch.setattr(api_connectors.httpx, "AsyncClient", _FakeClient)

        app = FastAPI()
        app.include_router(api_connectors.router)
        return TestClient(app)

    def test_success_emits_api_connector_test_completed(
        self, monkeypatch, caplog,
    ):
        row = {
            "id": "conn-1", "name": "Salesforce",
            "base_url": "https://api.example.com",
            "health_path": "/health",
        }
        client = self._app(monkeypatch, row, fake_response_status=200)
        with caplog.at_level(logging.INFO, logger="app.routes.api_connectors"):
            r = client.post("/api/v1/api-connectors/conn-1/test")
        assert r.status_code == 200
        recs = _records_with_event(caplog, "api_connector.test.completed")
        assert len(recs) == 1
        rec = recs[0]
        assert rec.connector_id == "conn-1"
        assert rec.connector_name == "Salesforce"
        assert rec.url == "https://api.example.com/health"
        assert rec.ok is True
        assert rec.status == 200

    def test_http_4xx_emits_failed_warning(self, monkeypatch, caplog):
        row = {"id": "c2", "name": "X", "base_url": "https://x", "health_path": "/h"}
        client = self._app(monkeypatch, row, fake_response_status=401)
        with caplog.at_level(logging.WARNING, logger="app.routes.api_connectors"):
            r = client.post("/api/v1/api-connectors/c2/test")
        assert r.status_code == 200
        recs = _records_with_event(caplog, "api_connector.test.failed")
        assert len(recs) == 1
        assert recs[0].levelno == logging.WARNING
        assert recs[0].status == 401
        assert recs[0].ok is False

    def test_connect_error_emits_failed_with_status_0(
        self, monkeypatch, caplog,
    ):
        row = {"id": "c3", "name": "Y", "base_url": "https://nope", "health_path": "/h"}
        client = self._app(monkeypatch, row, fake_response_status=None)
        with caplog.at_level(logging.WARNING, logger="app.routes.api_connectors"):
            r = client.post("/api/v1/api-connectors/c3/test")
        assert r.status_code == 200
        recs = _records_with_event(caplog, "api_connector.test.failed")
        assert len(recs) == 1
        assert recs[0].status == 0
        assert "conectar" in recs[0].error.lower()
