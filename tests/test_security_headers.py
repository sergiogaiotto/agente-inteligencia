"""Baseline de cabeçalhos de segurança no app (SKILL.md §7)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security_headers import install_security_headers_middleware


def _client():
    app = FastAPI()
    install_security_headers_middleware(app)

    @app.get("/x")
    async def x():
        return {"ok": True}

    return TestClient(app)


def test_baseline_headers_present():
    r = _client().get("/x")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "strict-origin" in r.headers["referrer-policy"]
    assert "camera=()" in r.headers["permissions-policy"]
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp


def test_hsts_absent_on_plain_http():
    r = _client().get("/x")  # TestClient usa http por padrão
    assert "strict-transport-security" not in r.headers


def test_hsts_present_when_forwarded_https():
    r = _client().get("/x", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in r.headers.get("strict-transport-security", "")
