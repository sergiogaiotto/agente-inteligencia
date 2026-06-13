"""PR8d — endpoints de suporte à UI de federação (config + remote-entries).

GET/PUT /api/v1/federation/config (root-only, valida workspace) e
GET /api/v1/federation/remote-entries (lista entries federadas). Sem Postgres:
settings_store e pool monkeypatchados.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.routes import federation as fed_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client(user):
    app = FastAPI()
    app.include_router(fed_routes.router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


ROOT = {"id": "r", "role": "root"}
MEMBER = {"id": "u", "role": "member"}


class TestConfigGet:
    def _patch(self, monkeypatch, *, enabled=True, ws="acme", dev=False, key=True):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(enabled))
        monkeypatch.setattr(fed_routes, "local_workspace", _async(ws))
        monkeypatch.setattr(fed_routes, "secret_key_present", lambda: key)
        monkeypatch.setattr(fed_routes.settings_store, "get",
                            _async("true" if dev else "false"))

    def test_non_root_403(self, monkeypatch):
        self._patch(monkeypatch)
        assert _client(MEMBER).get("/api/v1/federation/config").status_code == 403

    def test_root_returns_config(self, monkeypatch):
        self._patch(monkeypatch, enabled=True, ws="acme", dev=True, key=False)
        r = _client(ROOT).get("/api/v1/federation/config")
        assert r.status_code == 200
        b = r.json()
        assert b == {"enabled": True, "workspace": "acme", "dev_allow_http": True, "secret_key_present": False}


class TestConfigPut:
    def _patch_read(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes, "local_workspace", _async("acme"))
        monkeypatch.setattr(fed_routes, "secret_key_present", lambda: True)
        monkeypatch.setattr(fed_routes.settings_store, "get", _async("false"))

    def _capture_set(self, monkeypatch):
        saved = {}
        async def fake_set(k, v):
            saved[k] = v
        monkeypatch.setattr(fed_routes.settings_store, "set", fake_set)
        monkeypatch.setattr(fed_routes.audit_repo, "create", _async(None))
        return saved

    def test_non_root_403(self, monkeypatch):
        self._patch_read(monkeypatch)
        self._capture_set(monkeypatch)
        assert _client(MEMBER).put("/api/v1/federation/config", json={"enabled": True}).status_code == 403

    def test_invalid_workspace_422(self, monkeypatch):
        self._patch_read(monkeypatch)
        self._capture_set(monkeypatch)
        r = _client(ROOT).put("/api/v1/federation/config", json={"workspace": "Acme Corp!"})
        assert r.status_code == 422

    def test_saves_valid(self, monkeypatch):
        self._patch_read(monkeypatch)
        saved = self._capture_set(monkeypatch)
        r = _client(ROOT).put("/api/v1/federation/config",
                              json={"enabled": True, "workspace": "acme", "dev_allow_http": True})
        assert r.status_code == 200
        assert saved.get(fed_routes.WORKSPACE_SETTING_KEY) == "acme"
        assert saved.get(fed_routes.ENABLED_SETTING_KEY) == "true"
        assert saved.get(fed_routes._DEV_ALLOW_HTTP_KEY) == "true"

    def test_partial_only_enabled(self, monkeypatch):
        self._patch_read(monkeypatch)
        saved = self._capture_set(monkeypatch)
        r = _client(ROOT).put("/api/v1/federation/config", json={"enabled": False})
        assert r.status_code == 200
        assert saved.get(fed_routes.ENABLED_SETTING_KEY) == "false"
        assert fed_routes.WORKSPACE_SETTING_KEY not in saved  # workspace não tocado


class TestRemoteEntries:
    class _Conn:
        def __init__(self, rows): self.rows = rows
        async def fetch(self, sql, *p): return self.rows

    class _Acq:
        def __init__(self, c): self.c = c
        async def __aenter__(self): return self.c
        async def __aexit__(self, *a): return False

    class _Pool:
        def __init__(self, c): self.c = c
        def acquire(self): return TestRemoteEntries._Acq(self.c)

    def test_lists_federated(self, monkeypatch):
        rows = [{
            "id": "e1", "name": "Remote X", "version": "1.0.0",
            "remote_urn": "urn:maestro:acme:pipeline:x:1.0.0",
            "adapter_config": '{"remote": true, "peer_workspace": "acme"}',
        }]
        monkeypatch.setattr(fed_routes, "_get_pool", lambda: self._Pool(self._Conn(rows)))
        r = _client(MEMBER).get("/api/v1/federation/remote-entries")
        assert r.status_code == 200
        e = r.json()["entries"][0]
        assert e["name"] == "Remote X"
        assert e["peer_workspace"] == "acme"
        assert e["remote_urn"] == "urn:maestro:acme:pipeline:x:1.0.0"
