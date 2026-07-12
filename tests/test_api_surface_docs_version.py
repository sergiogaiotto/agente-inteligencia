"""API-4 + API-5 — versão real no OpenAPI/health e /docs trancado por admin em prod.

API-4: FastAPI(version) e /api/health expõem APP_VERSION (não mais "2.0.0" stale).
API-5: em produção, /docs /redoc /openapi.json exigem root/admin (fecha recon
anônimo da superfície inteira da API); em dev ficam abertos.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.version import APP_VERSION
from app.main import build_app


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    # Evita init_db/close_db reais no teste — só exercita a construção do app.
    yield


def _app(env: str) -> FastAPI:
    return build_app(Settings(app_env=env), lifespan_fn=_noop_lifespan)


class TestApiVersionTruth:
    def test_fastapi_version_is_app_version(self):
        assert _app("development").version == APP_VERSION
        assert _app("production").version == APP_VERSION

    def test_health_returns_app_version(self):
        # O handler /api/health é do módulo (não de build_app) — chama direto.
        from app.main import health

        payload = asyncio.run(health())
        assert payload["version"] == APP_VERSION
        assert payload["status"] == "ok"


class TestDocsLockdownProd:
    # O guard é `require_role("root","admin")`, cujo `_dep` chama `require_user`
    # por lookup no módulo auth (não via Depends) → monkeypatch em auth.require_user.

    def test_prod_openapi_requires_auth(self):
        c = TestClient(_app("production"))
        assert c.get("/openapi.json").status_code == 401
        assert c.get("/docs").status_code == 401
        assert c.get("/redoc").status_code == 401

    def test_prod_openapi_403_for_non_admin(self, monkeypatch):
        import app.core.auth as auth

        async def _member(request):
            return {"id": "u1", "role": "comum"}

        monkeypatch.setattr(auth, "require_user", _member)
        c = TestClient(_app("production"))
        assert c.get("/openapi.json").status_code == 403

    def test_prod_admin_gets_docs(self, monkeypatch):
        import app.core.auth as auth

        async def _admin(request):
            return {"id": "root", "role": "root"}

        monkeypatch.setattr(auth, "require_user", _admin)
        c = TestClient(_app("production"))
        r = c.get("/openapi.json")
        assert r.status_code == 200
        assert r.json()["info"]["version"] == APP_VERSION
        assert c.get("/docs").status_code == 200

    def test_dev_docs_open(self):
        c = TestClient(_app("development"))
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200
