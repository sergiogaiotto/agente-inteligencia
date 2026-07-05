"""CORS dinâmico (P0) — allowlist via platform_settings, sem restart.

Cobre: parse da allowlist; preflight OPTIONS (origem permitida→204 com headers,
não-permitida→sem CORS); resposta real (ACAO só p/ origem permitida); allowlist
vazia = inerte (comportamento atual preservado).
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import cors as cors_mod
from app.core.cors import parse_allowed_origins, DynamicCORSMiddleware


def _client(monkeypatch, origins: str) -> TestClient:
    class _S:
        cors_allowed_origins = origins

    monkeypatch.setattr(cors_mod, "get_settings", lambda: _S())
    app = FastAPI()
    app.add_middleware(DynamicCORSMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/ping")
    def ping_post():
        return {"ok": True}

    return TestClient(app)


class TestParseAllowedOrigins:
    def test_empty_is_empty_set(self):
        assert parse_allowed_origins("") == set()
        assert parse_allowed_origins(None) == set()

    def test_csv_trims_and_strips_trailing_slash(self):
        got = parse_allowed_origins(" https://a.com/ , https://b.io ")
        assert got == {"https://a.com", "https://b.io"}


class TestPreflight:
    def test_allowed_origin_returns_204_with_cors_headers(self, monkeypatch):
        c = _client(monkeypatch, "https://app.cliente.com")
        r = c.options(
            "/ping",
            headers={
                "Origin": "https://app.cliente.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key,content-type",
            },
        )
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "https://app.cliente.com"
        assert "POST" in r.headers["access-control-allow-methods"]
        assert "x-api-key" in r.headers["access-control-allow-headers"]
        assert r.headers.get("access-control-allow-credentials") == "true"

    def test_disallowed_origin_preflight_has_no_cors(self, monkeypatch):
        c = _client(monkeypatch, "https://app.cliente.com")
        r = c.options(
            "/ping",
            headers={
                "Origin": "https://evil.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in r.headers

    def test_empty_allowlist_is_inert(self, monkeypatch):
        c = _client(monkeypatch, "")
        r = c.options(
            "/ping",
            headers={
                "Origin": "https://app.cliente.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in r.headers


class TestApplyClearsOnEmpty:
    """Regressão: setar cors_allowed_origins='' (desligar CORS) deve LIMPAR o env.
    Antes, valor vazio de chave não-modelo era ignorado e o env antigo persistia
    no processo → a UI não conseguia desligar o CORS."""

    @pytest.mark.asyncio
    async def test_explicit_empty_clears_env(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://old.com")

        class _Store:
            async def get_all(self):
                return {"cors_allowed_origins": ""}  # operador salvou vazio

        monkeypatch.setattr("app.core.database.settings_store", _Store())
        await cfg.apply_settings_to_env()
        assert os.environ.get("CORS_ALLOWED_ORIGINS") is None

    @pytest.mark.asyncio
    async def test_absent_key_preserves_env(self, monkeypatch):
        """Chave AUSENTE do banco (não-modelo) preserva o .env como fallback."""
        from app.core import config as cfg

        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://from-dotenv.com")

        class _Store:
            async def get_all(self):
                return {}  # cors ausente

        monkeypatch.setattr("app.core.database.settings_store", _Store())
        await cfg.apply_settings_to_env()
        assert os.environ.get("CORS_ALLOWED_ORIGINS") == "https://from-dotenv.com"


class TestActualResponse:
    def test_allowed_origin_gets_acao_and_expose(self, monkeypatch):
        c = _client(monkeypatch, "https://app.cliente.com")
        r = c.get("/ping", headers={"Origin": "https://app.cliente.com"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://app.cliente.com"
        assert "x-request-id" in r.headers.get("access-control-expose-headers", "")

    def test_disallowed_origin_no_acao(self, monkeypatch):
        c = _client(monkeypatch, "https://app.cliente.com")
        r = c.get("/ping", headers={"Origin": "https://evil.com"})
        assert "access-control-allow-origin" not in r.headers

    def test_no_origin_header_no_acao(self, monkeypatch):
        c = _client(monkeypatch, "https://app.cliente.com")
        r = c.get("/ping")
        assert "access-control-allow-origin" not in r.headers
