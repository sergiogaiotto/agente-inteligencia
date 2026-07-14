"""FF6 (35.6.0) — webhook de conclusão do invoke-job, padrão FALLBACK.

Decisão do dono: `callback_url` por request SOBREPÕE o `webhook_url` default da
API-key. Payload LEVE sem result (o receptor busca via GET autenticado — nada
de PII para a URL); assinatura HMAC-SHA256 com segredo = sha256 da key (o
cliente detém a key e deriva o mesmo segredo); guarda SSRF no ACEITE, no
REGISTRO (PATCH) e no ENVIO (DNS pode mudar); entrega best-effort com retries
na fila off-path (drenada no shutdown).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import invoke_jobs


@pytest.fixture(autouse=True)
def _estado_limpo():
    invoke_jobs._reset_for_tests()
    yield
    invoke_jobs._reset_for_tests()


class FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeHttpClient:
    """Substitui httpx.AsyncClient: registra chamadas e devolve da fila."""
    calls: list = []
    responses: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        FakeHttpClient.calls.append({"url": url, "content": content, "headers": headers})
        return FakeHttpClient.responses.pop(0) if FakeHttpClient.responses else FakeResp(200)


def _fake_dns(monkeypatch):
    """A guarda SSRF resolve DNS de verdade (lição do repo: hosts de teste não
    resolvem → falso bloqueio). IP público p/ *.example.com; IPs literais
    privados seguem barrados pelo validate (não passam por DNS)."""
    import socket as _socket
    real = _socket.getaddrinfo

    def fake(host, *a, **kw):
        if str(host).endswith("example.com"):
            return [(2, 1, 6, "", ("93.184.216.34", 443))]
        return real(host, *a, **kw)
    monkeypatch.setattr("app.core.ssrf.socket.getaddrinfo", fake)


@pytest.fixture
def fake_http(monkeypatch):
    FakeHttpClient.calls = []
    FakeHttpClient.responses = []
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeHttpClient)
    # sleep dos retries vira no-op (teste rápido)
    monkeypatch.setattr(invoke_jobs.asyncio, "sleep", AsyncMock())
    _fake_dns(monkeypatch)
    return FakeHttpClient


def _use_pool(monkeypatch, key_hash="abc123hash"):
    class _Con:
        async def fetchrow(self, sql, *a):
            return {"key_hash": key_hash} if "key_hash" in sql else None

    class _Pool:
        def acquire(self):
            con = _Con()

            class _Ctx:
                async def __aenter__(self):
                    return con

                async def __aexit__(self, *b):
                    return False
            return _Ctx()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool())


class TestEntregaAssinada:
    @pytest.mark.asyncio
    async def test_assinatura_hmac_com_key_hash(self, monkeypatch, fake_http):
        _use_pool(monkeypatch, key_hash="segredo-derivavel-pelo-cliente")
        payload = {"event": "invoke_job.finished", "job_id": "j1", "status": "completed"}
        await invoke_jobs._deliver_webhook("https://cliente.example.com/hook", payload, "k1")
        assert len(fake_http.calls) == 1
        call = fake_http.calls[0]
        body = call["content"]
        esperada = hmac_mod.new(b"segredo-derivavel-pelo-cliente", body,
                                hashlib.sha256).hexdigest()
        assert call["headers"]["X-Maestro-Signature"] == f"sha256={esperada}"
        assert call["headers"]["X-Maestro-Event"] == "invoke_job.finished"
        assert json.loads(body)["job_id"] == "j1"

    @pytest.mark.asyncio
    async def test_ssrf_no_envio_bloqueia_url_privada(self, monkeypatch, fake_http):
        _use_pool(monkeypatch)
        await invoke_jobs._deliver_webhook("http://127.0.0.1:8080/hook",
                                           {"event": "x", "job_id": "j1"}, None)
        assert fake_http.calls == []  # nem tentou

    @pytest.mark.asyncio
    async def test_retry_ate_sucesso(self, monkeypatch, fake_http):
        _use_pool(monkeypatch)
        fake_http.responses = [FakeResp(500), FakeResp(200)]
        await invoke_jobs._deliver_webhook("https://cliente.example.com/hook",
                                           {"event": "x", "job_id": "j1"}, None)
        assert len(fake_http.calls) == 2  # 500 → retry → 200 → para


class TestNotifyFinish:
    def test_noop_sem_webhook(self, monkeypatch):
        agendou = MagicMock()
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics", agendou)
        invoke_jobs._notify_finish("j1", "p1", {"user_input": "oi"}, "completed")
        agendou.assert_not_called()

    def test_payload_leve_sem_result(self, monkeypatch):
        capturado = {}

        def _capture(coro):
            # coroutine de _deliver_webhook(url, payload, api_key_id)
            capturado["frame"] = coro.cr_frame.f_locals
            coro.close()
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics", _capture)
        req = {"webhook_url": "https://x.example.com/h", "api_key_id": "k1",
               "idempotency_key": "op-1", "user_input": "PII AQUI"}
        invoke_jobs._notify_finish("j9", "p1", req, "failed", "job_timeout")
        payload = capturado["frame"]["payload"]
        assert payload["job_id"] == "j9" and payload["status"] == "failed"
        assert payload["error"] == "job_timeout"
        assert payload["idempotency_key"] == "op-1"
        assert payload["status_url"].endswith("/pipelines/p1/jobs/j9")
        # NUNCA envia conteúdo: nem input, nem output/result
        blob = json.dumps(payload)
        assert "PII AQUI" not in blob and "result" not in payload and "output" not in payload


class TestFiacao:
    def test_schema_e_migracao(self):
        from app.core.database import SCHEMA, _IDEMPOTENT_MIGRATIONS
        assert "webhook_url TEXT" in SCHEMA
        assert any("api_keys ADD COLUMN IF NOT EXISTS webhook_url" in m
                   for m in _IDEMPOTENT_MIGRATIONS)

    def test_contrato_tem_callback_url(self):
        from app.models.schemas import PipelineInvokeRequest
        assert "callback_url" in PipelineInvokeRequest.model_fields

    def test_rota_async_resolve_fallback_e_valida_ssrf(self):
        src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        assert "data.callback_url" in src
        assert 'invalid_callback_url' in src
        assert 'request_payload["webhook_url"] = webhook_url' in src

    def test_worker_notifica_todos_os_finais(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        # completed + 7 finais de falha do worker + lost do boot
        assert src.count("_notify_finish(") >= 9
        assert '_notify_finish(job_id, pid, req, "completed")' in src
        assert '"lost", "job_interrupted"' in src

    def test_patch_valida_e_ui_tem_campo(self):
        rotas = Path("app/routes/api_keys.py").read_text(encoding="utf-8")
        assert "invalid_webhook_url" in rotas and '"webhook_url": webhook' in rotas
        ui = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert 'data-testid="apikey-scope-webhook"' in ui
        assert "X-Maestro-Signature" in ui  # a UI documenta a assinatura


class TestRotaAsyncFallback:
    """Nível de rota: callback_url do request vence o default da key."""

    def _client(self, monkeypatch, key_webhook=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.core.auth import require_user
        from app.routes.pipelines import router
        _fake_dns(monkeypatch)
        monkeypatch.setattr("app.core.config.get_settings",
                            lambda: SimpleNamespace(invoke_async_enabled=True,
                                                    api_key_invoke_published_only=False))
        from app.core import database as db
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            AsyncMock(return_value={"id": "p1", "status": "publicado", "name": "P"}))
        monkeypatch.setattr(db.api_keys_repo, "find_by_id",
                            AsyncMock(return_value={"id": "k1", "webhook_url": key_webhook}))
        monkeypatch.setattr("app.catalog.pipeline_defs._build_subgraph",
                            AsyncMock(return_value={"root_agent_id": "a1", "nodes": [{"id": "a1"}]}))
        self.cj = AsyncMock(side_effect=lambda **kw: (
            {"id": "ij_t", "pipeline_id": kw["pipeline_id"], "status": "queued",
             "attempts": 0, "idempotency_key": None,
             "request_payload": json.dumps(kw["request_payload"])}, True))
        monkeypatch.setattr("app.core.invoke_jobs.create_job", self.cj)
        monkeypatch.setattr("app.core.invoke_jobs.find_existing_job", AsyncMock(return_value=None))
        monkeypatch.setattr("app.core.invoke_jobs.dispatch", MagicMock(return_value=True))
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user] = lambda: {"id": "u1", "role": "comum"}
        return TestClient(app)

    def test_callback_do_request_persiste(self, monkeypatch):
        c = self._client(monkeypatch)
        r = c.post("/api/v1/pipelines/p1/invoke/async",
                   json={"message": "oi", "callback_url": "https://req.example.com/hook"})
        assert r.status_code == 202
        assert self.cj.call_args.kwargs["request_payload"]["webhook_url"] == "https://req.example.com/hook"

    def test_url_privada_400_nomeado(self, monkeypatch):
        c = self._client(monkeypatch)
        r = c.post("/api/v1/pipelines/p1/invoke/async",
                   json={"message": "oi", "callback_url": "http://192.168.0.10/hook"})
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_callback_url"

    def test_sem_callback_sem_key_sem_webhook(self, monkeypatch):
        c = self._client(monkeypatch)
        r = c.post("/api/v1/pipelines/p1/invoke/async", json={"message": "oi"})
        assert r.status_code == 202
        assert "webhook_url" not in self.cj.call_args.kwargs["request_payload"]
