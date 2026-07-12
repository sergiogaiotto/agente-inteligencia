"""API-6 — teto global do corpo de requisição (anti-DoS de memória).

O middleware ASGI rejeita 413 por Content-Length ANTES de ler o corpo. Cobre:
corpo acima do cap → 413; abaixo → passa (corpo intacto); sem Content-Length →
passa (limitação conhecida); cap acomoda o maior corpo legítimo (anexos ~67MB).
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.request_limits import RequestBodySizeLimitMiddleware


def _app(cap_bytes: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodySizeLimitMiddleware, max_bytes_getter=lambda: cap_bytes)

    @app.post("/echo")
    async def echo(request: Request):
        raw = await request.body()
        return {"received": len(raw)}

    return app


class TestBodyCap:
    def test_rejeita_acima_do_cap(self):
        c = TestClient(_app(1024))  # cap 1KB
        r = c.post("/echo", content=b"x" * 2048)  # 2KB
        assert r.status_code == 413
        assert r.json()["error"] == "request_too_large"

    def test_permite_abaixo_do_cap_corpo_intacto(self):
        c = TestClient(_app(1024))
        r = c.post("/echo", content=b"x" * 512)
        assert r.status_code == 200
        assert r.json()["received"] == 512  # o corpo chegou íntegro ao handler

    def test_permite_exatamente_no_cap(self):
        c = TestClient(_app(1024))
        r = c.post("/echo", content=b"x" * 1024)
        assert r.status_code == 200

    def test_get_sem_corpo_passa(self):
        app = _app(1024)

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        assert TestClient(app).get("/ping").status_code == 200

    def test_cap_zero_desliga(self):
        # cap<=0 → middleware inerte (não rejeita nada).
        c = TestClient(_app(0))
        r = c.post("/echo", content=b"x" * 10000)
        assert r.status_code == 200

    def test_mensagem_413_traz_limite_em_mb(self):
        c = TestClient(_app(2 * 1024 * 1024))  # 2 MB
        r = c.post("/echo", content=b"x" * (3 * 1024 * 1024))
        assert r.status_code == 413
        assert "2 MB" in r.json()["message"]


class TestWiredIntoApp:
    def test_settings_default_acomoda_anexos(self):
        # O default (100MB) precisa ficar ACIMA do maior corpo legítimo:
        # 5 anexos × 10MB raw ≈ 67MB base64 (routes/agents.py).
        from app.core.config import Settings

        cap_mb = Settings(app_env="development").max_request_body_mb
        assert cap_mb * 1024 * 1024 > 67 * 1024 * 1024
