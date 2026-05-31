"""PR #232 — backend resolve auth_token armazenado do banco quando UI passa
`tool_id` e o `auth_token` veio vazio.

# Bug

Após PR #229, o GET `/api/v1/tools` mascara o `auth_token` (substitui por
`""`). O painel direito da tela MCP (`testPreview` em tools.html) usa
`previewTool.auth_token` direto para chamar `/tools/test` — passou a enviar
string vazia. Resultado: testar o MCP via "Testar agora" sempre dá
**HTTP 401** (sem Authorization Bearer).

O log estruturado do PR #231 confirmou isso (operador enviou amostra):
```json
{"event":"mcp.test.failed","mcp_endpoint":"https://mcp.tavily.com/mcp/",
 "auth_type":"api_key","details":"HTTP 401",
 "request_body":"...auth_token:\"\"..."}
```

# Fix

Adiciona `tool_id: Optional[str]` em `MCPTestRequest` e `MCPExecuteRequest`.
Helper `_resolve_secrets_from_tool_id(data)` busca o token armazenado (e
secrets do auth_config) quando o caller passa `tool_id` E os campos vêm
vazios.

Prioridade: token preenchido pelo caller SEMPRE vence — caso operador
queira testar um valor novo antes de salvar.

Token armazenado pode estar cifrado (`fernet:`); `_build_mcp_auth` chama
`read_secret` que decifra. Idempotente para plaintext legacy.

# Cobertura dos testes

1. tool_id + auth_token="" → backend busca token cifrado, decifra, monta
   header Authorization Bearer
2. tool_id + auth_token preenchido → caller wins, backend NÃO sobrescreve
3. tool_id válido + token vazio + auth_config com secrets vazios →
   merge dos secrets do banco
4. sem tool_id → comportamento legado (sem fallback)
5. tool_id inexistente → graceful, segue sem token (vai falhar downstream)
6. Log do mcp.test.failed inclui `tool_id`
"""
from __future__ import annotations

import logging
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import secrets as _secrets


# ─── Helpers ──────────────────────────────────────────────


def _records_with_event(caplog, event: str) -> list:
    return [r for r in caplog.records if getattr(r, "event", "") == event]


def _mock_tools_repo(monkeypatch, store: dict):
    from app.routes import dashboard

    async def fake_find_by_id(tid: str):
        return store.get(tid)

    monkeypatch.setattr(dashboard.tools_repo, "find_by_id", fake_find_by_id)


# ─── Captura do dict que vai pro `_build_mcp_auth` ─────────


@pytest.fixture
def captured_auth(monkeypatch):
    """Substitui `_build_mcp_auth` por um espião que registra o `auth_token`
    que efetivamente foi passado para a construção do header — é o efeito
    visível do `_resolve_secrets_from_tool_id`."""
    from app.routes import dashboard
    captured = {"auth_token": None, "auth_type": None, "auth_config": None}

    real = dashboard._build_mcp_auth

    def spy(auth_type="", auth_token="", auth_config="{}"):
        captured["auth_token"] = auth_token
        captured["auth_type"] = auth_type
        captured["auth_config"] = auth_config
        return real(auth_type, auth_token, auth_config)

    monkeypatch.setattr(dashboard, "_build_mcp_auth", spy)
    return captured


@pytest.fixture
def mocked_test_impl(monkeypatch):
    """Faz o `_test_mcp_connection_impl` retornar resultado controlado e
    NÃO chamar httpx (evita rede)."""
    from app.routes import dashboard

    # Não substituímos o impl inteiro — queremos que `_resolve_secrets_from_tool_id`
    # rode de verdade. Em vez disso, paramos a execução após `_build_mcp_auth`
    # já ter sido chamado, retornando um stub de resultado.

    real_impl = dashboard._test_mcp_connection_impl

    async def lite_impl(data):
        # Roda _resolve + _build_mcp_auth (capturado pelo spy) e retorna
        await dashboard._resolve_secrets_from_tool_id(data)
        dashboard._build_mcp_auth(
            data.auth_type or "", data.auth_token or "", data.auth_config or "{}",
        )
        return {
            "success": False, "details": "HTTP 401 (stub)",
            "latency": 50, "server_name": None,
            "discovered_tools": [], "recommendations": [],
        }

    monkeypatch.setattr(dashboard, "_test_mcp_connection_impl", lite_impl)


def _app():
    from app.routes.dashboard import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ─── Suite ─────────────────────────────────────────────────


class TestResolveAuthFromToolId:

    def test_resolves_stored_token_when_request_auth_token_empty(
        self, monkeypatch, captured_auth, mocked_test_impl,
    ):
        """Cenário do bug original: UI manda auth_token="" + tool_id."""
        stored_cipher = _secrets.write_secret("tvly-real-secret-xyz")
        _mock_tools_repo(monkeypatch, {
            "t1": {
                "id": "t1", "name": "Tavily",
                "auth_token": stored_cipher,
                "auth_config": "{}",
            },
        })
        client = _app()
        r = client.post("/api/v1/tools/test", json={
            "endpoint": "https://mcp.tavily.com/mcp/",
            "auth_type": "api_key",
            "auth_token": "",      # vazio (UI mascarou)
            "tool_id": "t1",
        })
        assert r.status_code == 200
        # Backend recebeu o token cifrado armazenado (será decifrado dentro
        # de _build_mcp_auth via read_secret)
        assert captured_auth["auth_token"] == stored_cipher

    def test_caller_provided_token_wins_over_stored(
        self, monkeypatch, captured_auth, mocked_test_impl,
    ):
        """Operador colou valor novo antes de salvar — backend respeita."""
        _mock_tools_repo(monkeypatch, {
            "t1": {
                "id": "t1", "auth_token": _secrets.write_secret("old-stored"),
                "auth_config": "{}",
            },
        })
        client = _app()
        r = client.post("/api/v1/tools/test", json={
            "endpoint": "https://x",
            "auth_type": "api_key",
            "auth_token": "novo-token-do-operador",   # caller forneceu
            "tool_id": "t1",
        })
        assert r.status_code == 200
        assert captured_auth["auth_token"] == "novo-token-do-operador"

    def test_no_tool_id_falls_back_to_legacy_empty_token(
        self, monkeypatch, captured_auth, mocked_test_impl,
    ):
        """Sem tool_id, backend não busca nada — comportamento legado."""
        _mock_tools_repo(monkeypatch, {})
        client = _app()
        r = client.post("/api/v1/tools/test", json={
            "endpoint": "https://x",
            "auth_type": "api_key",
            "auth_token": "",
        })
        assert r.status_code == 200
        assert captured_auth["auth_token"] == ""

    def test_unknown_tool_id_does_not_crash(
        self, monkeypatch, captured_auth, mocked_test_impl,
    ):
        """tool_id apontando para tool deletada → segue sem token."""
        _mock_tools_repo(monkeypatch, {})
        client = _app()
        r = client.post("/api/v1/tools/test", json={
            "endpoint": "https://x",
            "auth_type": "api_key",
            "auth_token": "",
            "tool_id": "ghost",
        })
        assert r.status_code == 200
        assert captured_auth["auth_token"] == ""

    def test_merges_oauth_secrets_from_stored_when_ui_blanked_them(
        self, monkeypatch, captured_auth, mocked_test_impl,
    ):
        """auth_config com client_secret vazio → backend mescla do banco."""
        import json as _json
        _mock_tools_repo(monkeypatch, {
            "t2": {
                "id": "t2",
                "auth_token": "",
                "auth_config": _json.dumps({
                    "client_id": "ci-stored",
                    "client_secret": "real-secret",
                    "token_url": "https://auth/token",
                }),
            },
        })
        ui_cfg = _json.dumps({
            "client_id": "ci-stored",
            "client_secret": "",        # mascarado pela UI
            "token_url": "https://auth/token",
        })
        client = _app()
        r = client.post("/api/v1/tools/test", json={
            "endpoint": "https://x",
            "auth_type": "oauth2",
            "auth_token": "",
            "auth_config": ui_cfg,
            "tool_id": "t2",
        })
        assert r.status_code == 200
        merged = _json.loads(captured_auth["auth_config"])
        assert merged["client_secret"] == "real-secret"
        assert merged["client_id"] == "ci-stored"

    def test_log_event_includes_tool_id(
        self, monkeypatch, captured_auth, mocked_test_impl, caplog,
    ):
        """O evento estruturado deve trazer `tool_id` para que o operador
        consiga correlacionar uma falha no Log Viewer com a tool específica."""
        _mock_tools_repo(monkeypatch, {
            "t1": {"id": "t1", "auth_token": "", "auth_config": "{}"},
        })
        client = _app()
        with caplog.at_level(logging.WARNING, logger="app.routes.dashboard"):
            r = client.post("/api/v1/tools/test", json={
                "endpoint": "https://x", "auth_type": "api_key",
                "auth_token": "", "tool_id": "t1",
            })
        assert r.status_code == 200
        recs = _records_with_event(caplog, "mcp.test.failed")
        assert len(recs) == 1
        assert recs[0].tool_id == "t1"
