"""PR7 — discovery de Plataforma Externa por URL.

Cobre _parse_openai_models, discover (openai 200/401, federação, SSRF, nada
detectado) com _http_request + socket.getaddrinfo monkeypatchados, e o endpoint
POST /entries/{id}/external-discover (auth owner/root, kind gate).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.catalog.external_discovery as disc
import app.core.ssrf as ssrf
from app.catalog.external_discovery import _parse_openai_models, discover
from app.core.auth import require_user
from app.core.database import audit_repo, catalog_entries_repo
from app.routes.catalog import router as catalog_router


def _async(v):
    async def f(*a, **k):
        return v
    return f


def _patch_resolve(monkeypatch, ips):
    def fake(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake)


def _http_dispatch(*, models=None, federation=None):
    async def fake(method, url, *, headers, json_body, timeout_s):
        if url.endswith("/v1/models"):
            if models is None:
                raise httpx.ConnectError("no models")
            return models
        if url.endswith("maestro-federation.json"):
            if federation is None:
                raise httpx.ConnectError("no fed")
            return federation
        raise httpx.ConnectError("unknown")
    return fake


class TestParseModels:
    def test_parses_ids(self):
        assert _parse_openai_models(b'{"data":[{"id":"a"},{"id":"b"}]}') == ["a", "b"]

    def test_bad_shape(self):
        assert _parse_openai_models(b'{"nope":1}') == []
        assert _parse_openai_models(b'garbage') == []


class TestDiscover:
    def test_openai_200_suggests_first_model(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(disc, "_http_request", _http_dispatch(
            models=(200, b'{"data":[{"id":"gpt-4o-mini"},{"id":"gpt-4o"}]}')))
        res = asyncio.run(discover("https://api.vendor.example"))
        assert any(d["type"] == "openai_compatible" for d in res["detected"])
        assert res["suggested"]["mode"] == "openai_chat"
        assert res["suggested"]["path"] == "/v1/chat/completions"
        assert res["suggested"]["model"] == "gpt-4o-mini"

    def test_openai_401_detected_auth_required(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(disc, "_http_request", _http_dispatch(models=(401, b"{}")))
        res = asyncio.run(discover("https://api.vendor.example"))
        assert any(d["type"] == "openai_compatible" for d in res["detected"])
        assert res["suggested"]["model"] is None

    def test_federation_manifest_detected(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(disc, "_http_request", _http_dispatch(
            federation=(200, b'{"workspace":"acme","capabilities":[{"urn":"x"}]}')))
        res = asyncio.run(discover("https://peer.example"))
        types = [d["type"] for d in res["detected"]]
        assert "maestro_federation" in types

    def test_ssrf_blocks_private(self, monkeypatch):
        _patch_resolve(monkeypatch, ["10.0.0.1"])
        called = {"http": False}

        async def fake_http(*a, **k):
            called["http"] = True
            return (200, b"{}")

        monkeypatch.setattr(disc, "_http_request", fake_http)
        res = asyncio.run(discover("https://api.vendor.example"))
        assert "SSRF" in (res["error"] or "") and called["http"] is False

    def test_nothing_detected_sets_error(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(disc, "_http_request", _http_dispatch())  # tudo ConnectError
        res = asyncio.run(discover("https://api.vendor.example"))
        assert res["detected"] == [] and res["error"]


# ─── Endpoint ────────────────────────────────────────────────────


def _client(user):
    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


OWNER = {"id": "owner-1", "role": "user"}
OTHER = {"id": "x", "role": "user"}


def _entry(**over):
    base = {"id": "ext-1", "name": "X", "kind": "external_platform", "status": "draft",
            "owner_user_id": "owner-1", "visibility": "company", "tags": "[]",
            "adapter_config": "{}", "urn": "urn:maestro:default:external_platform:ext-1:0.1.0"}
    base.update(over)
    return base


@pytest.fixture
def disc_storage(monkeypatch):
    state = {"entry": _entry()}
    monkeypatch.setattr(catalog_entries_repo, "find_by_id",
                        lambda eid: _async(dict(state["entry"]) if state["entry"]["id"] == eid else None)())
    monkeypatch.setattr(audit_repo, "create", _async(None))
    monkeypatch.setattr("app.routes.catalog.discover_external",
                        _async({"base_url": "https://api.x", "detected": [{"type": "openai_compatible", "detail": "3 modelos"}],
                                "suggested": {"mode": "openai_chat", "path": "/v1/chat/completions", "model": "gpt-4o-mini", "auth_type": "bearer"}}))
    return state


class TestDiscoverEndpoint:
    def test_owner_ok(self, disc_storage):
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-discover", json={"base_url": "https://api.x"})
        assert r.status_code == 200
        assert r.json()["suggested"]["model"] == "gpt-4o-mini"

    def test_non_owner_403(self, disc_storage):
        c = _client(OTHER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-discover", json={"base_url": "https://api.x"})
        assert r.status_code == 403

    def test_non_external_422(self, disc_storage):
        disc_storage["entry"] = _entry(kind="agent")
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-discover", json={"base_url": "https://api.x"})
        assert r.status_code == 422
