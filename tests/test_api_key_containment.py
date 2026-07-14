"""Contenção de privilégio da API Key (P0).

Uma key herda o role do dono → sem contenção, alcança toda a superfície admin.
Aqui: escalação (api-keys/settings/users) é SEMPRE negada a um principal-via-key;
a restrição 'só superfície pública' (invoke+descoberta) é opt-in via setting.
Cookie/UI NÃO é afetado.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.api_auth import (
    _is_escalation_path,
    _is_public_surface,
    apikey_route_denied,
    ApiAuthMiddleware,
)


# ── funções puras ──
class TestEscalationPath:
    @pytest.mark.parametrize("path", [
        "/api/v1/api-keys", "/api/v1/api-keys/abc",
        "/api/v1/settings", "/api/v1/settings/parameters",
        "/api/v1/users", "/api/v1/users/abc", "/api/v1/domains",
    ])
    def test_escalation_paths_flagged(self, path):
        assert _is_escalation_path(path) is True

    @pytest.mark.parametrize("path", [
        "/api/v1/pipelines", "/api/v1/agents", "/api/v1/skills",
        "/api/v1/users/me",  # /me é allowlisted (não-guarded) — startswith não casa "users/" exato? casa users/me
    ])
    def test_non_escalation_paths_not_flagged(self, path):
        # NB: /users/me casa o prefixo, mas /me é allowlisted ANTES (requires_auth
        # False) então a contenção nunca roda pra ele — ver teste de integração.
        if path == "/api/v1/users/me":
            assert _is_escalation_path(path) is True  # prefixo casa; protegido a montante
        else:
            assert _is_escalation_path(path) is False


class TestPublicSurface:
    @pytest.mark.parametrize("method,path,expected", [
        ("GET", "/api/v1/pipelines", True),
        ("GET", "/api/v1/pipelines/abc", True),
        ("GET", "/api/v1/pipelines/abc/inputs-schema", True),
        ("POST", "/api/v1/pipelines/abc/invoke", True),
        ("POST", "/api/v1/pipelines/abc/invoke/stream", True),
        ("POST", "/api/v1/pipelines", False),            # criar pipeline: não
        ("POST", "/api/v1/pipelines/abc/status", False), # mudar status: não
        ("GET", "/api/v1/agents", False),                # fora de pipelines
    ])
    def test_public_surface(self, method, path, expected):
        assert _is_public_surface(method, path) is expected


class TestApikeyRouteDenied:
    def _settings(self, monkeypatch, public_only: bool):
        class _S:
            api_key_public_surface_only = public_only
        monkeypatch.setattr("app.core.config.get_settings", lambda: _S())

    def test_escalation_always_denied_even_toggle_off(self, monkeypatch):
        self._settings(monkeypatch, public_only=False)
        assert apikey_route_denied("POST", "/api/v1/api-keys") == "escalation_or_secret_route"
        assert apikey_route_denied("GET", "/api/v1/settings") == "escalation_or_secret_route"

    def test_toggle_off_allows_non_escalation(self, monkeypatch):
        self._settings(monkeypatch, public_only=False)
        assert apikey_route_denied("GET", "/api/v1/agents") is None  # permitido (default)
        assert apikey_route_denied("POST", "/api/v1/pipelines/x/invoke") is None

    def test_toggle_on_restricts_to_public_surface(self, monkeypatch):
        self._settings(monkeypatch, public_only=True)
        assert apikey_route_denied("GET", "/api/v1/agents") == "public_surface_only_enabled"
        assert apikey_route_denied("POST", "/api/v1/pipelines/x/invoke") is None  # público → ok
        assert apikey_route_denied("GET", "/api/v1/pipelines") is None


# ── integração no middleware: principal-via-key vs cookie ──
def _app(monkeypatch, *, via_api_key: bool, public_only: bool = False):
    async def _fake_require_user(request):
        if via_api_key:
            request.state.api_key_id = "key-123"
        return {"id": "u1", "role": "root"}

    monkeypatch.setattr("app.core.auth.require_user", _fake_require_user)

    class _S:
        api_key_public_surface_only = public_only
    monkeypatch.setattr("app.core.config.get_settings", lambda: _S())

    app = FastAPI()
    app.add_middleware(ApiAuthMiddleware)

    @app.get("/api/v1/settings")
    def settings():
        return {"secret": "x"}

    @app.get("/api/v1/agents")
    def agents():
        return {"agents": []}

    @app.post("/api/v1/pipelines/{pid}/invoke")
    def invoke(pid: str):
        return {"status": "completed"}

    return TestClient(app, raise_server_exceptions=False)


class TestMiddlewareContainment:
    def test_api_key_denied_on_settings(self, monkeypatch):
        c = _app(monkeypatch, via_api_key=True)
        r = c.get("/api/v1/settings")
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "api_key_forbidden_route"

    def test_api_key_allowed_on_invoke(self, monkeypatch):
        c = _app(monkeypatch, via_api_key=True)
        r = c.post("/api/v1/pipelines/abc/invoke")
        assert r.status_code == 200

    def test_api_key_agents_allowed_toggle_off(self, monkeypatch):
        c = _app(monkeypatch, via_api_key=True, public_only=False)
        assert c.get("/api/v1/agents").status_code == 200

    def test_api_key_agents_denied_toggle_on(self, monkeypatch):
        c = _app(monkeypatch, via_api_key=True, public_only=True)
        assert c.get("/api/v1/agents").status_code == 403

    def test_cookie_principal_unaffected(self, monkeypatch):
        # via_api_key=False → sem api_key_id → contenção NÃO roda (cookie/UI livre)
        c = _app(monkeypatch, via_api_key=False, public_only=True)
        assert c.get("/api/v1/settings").status_code == 200
        assert c.get("/api/v1/agents").status_code == 200
