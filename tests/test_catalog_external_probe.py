"""PR1 — Plataforma Externa testável: config de conexão (adapter) + teste inline.

Cobre:
- ExternalProbeConfig (validação de base_url/timeout).
- Redação do segredo cifrado (db_row_to_entry_dict / _redact_adapter_config).
- run_probe (openai_chat/http_ping/SSRF/auth/timeout/erro-do-vendor) com
  _http_request e socket.getaddrinfo monkeypatchados (sem rede).
- Endpoints PUT/GET /external-adapter e POST /external-test-inline
  (auth owner/root, kind gate, draft gate, cifra do segredo, redação na resposta).

Sem rede/Postgres: repos, queries de adapter e run_probe são monkeypatchados.
"""
from __future__ import annotations

import asyncio
import socket as _socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.catalog.external_probe as probe
import app.core.ssrf as ssrf
from app.catalog.external_probe import run_probe
from app.catalog.models import ExternalProbeConfig
from app.catalog.queries import _redact_adapter_config, db_row_to_entry_dict
from app.core.auth import require_user
from app.core.crypto import encrypt_secret
from app.core.database import audit_repo, catalog_entries_repo
from app.routes.catalog import router as catalog_router


# ─── helpers ─────────────────────────────────────────────────────


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _patch_resolve(monkeypatch, ips):
    def fake(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake)


def _cfg(**over) -> dict:
    base = {
        "mode": "openai_chat",
        "base_url": "https://api.vendor.example",
        "auth_type": "bearer",
        "auth_header": "X-API-Key",
        "model": "gpt-4o-mini",
        "path": None,
        "test_prompt": "Responda apenas: OK",
        "timeout_ms": 30000,
    }
    base.update(over)
    return base


# ═════════════════════════════════════════════════════════════════
# ExternalProbeConfig — validação
# ═════════════════════════════════════════════════════════════════


class TestProbeConfigModel:
    def test_valid_defaults(self):
        c = ExternalProbeConfig(base_url="https://api.vendor.example/")
        assert c.base_url == "https://api.vendor.example"  # trailing / removido
        assert c.mode == "openai_chat" and c.auth_type == "bearer"
        assert c.timeout_ms == 30000

    def test_base_url_requires_scheme(self):
        with pytest.raises(ValidationError):
            ExternalProbeConfig(base_url="api.vendor.example")

    def test_timeout_bounds(self):
        with pytest.raises(ValidationError):
            ExternalProbeConfig(base_url="https://x.example", timeout_ms=500)
        with pytest.raises(ValidationError):
            ExternalProbeConfig(base_url="https://x.example", timeout_ms=999999)

    def test_http_allowed_in_model(self):
        # o model aceita http (dev); a guarda SSRF é quem barra em runtime
        c = ExternalProbeConfig(base_url="http://x.example")
        assert c.base_url == "http://x.example"


# ═════════════════════════════════════════════════════════════════
# Redação do segredo
# ═════════════════════════════════════════════════════════════════


class TestRedaction:
    def test_strips_cipher_adds_secret_set(self):
        cfg = {"probe": {"mode": "openai_chat", "secret_cipher": "enc::abc"}}
        red = _redact_adapter_config(cfg)
        assert "secret_cipher" not in red["probe"]
        assert red["probe"]["secret_set"] is True

    def test_no_probe_unchanged(self):
        cfg = {"other": 1}
        assert _redact_adapter_config(cfg) == {"other": 1}

    def test_non_dict_unchanged(self):
        assert _redact_adapter_config(None) is None
        assert _redact_adapter_config("x") == "x"

    def test_db_row_redacts(self):
        row = {
            "id": "e1", "kind": "external_platform", "tags": "[]",
            "adapter_config": '{"probe": {"mode": "http_ping", "secret_cipher": "enc::zzz"}}',
        }
        out = db_row_to_entry_dict(row)
        assert out["adapter_config"]["probe"]["secret_set"] is True
        assert "secret_cipher" not in out["adapter_config"]["probe"]

    def test_db_row_does_not_mutate_input(self):
        # redação opera em cópia — não altera o dict original
        original_probe = {"mode": "openai_chat", "secret_cipher": "enc::keepme"}
        cfg = {"probe": original_probe}
        _redact_adapter_config(cfg)
        assert original_probe["secret_cipher"] == "enc::keepme"


# ═════════════════════════════════════════════════════════════════
# run_probe — sem rede
# ═════════════════════════════════════════════════════════════════


class TestRunProbe:
    def test_openai_chat_success(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        captured = {}

        async def fake_http(method, url, *, headers, json_body, timeout_s):
            captured.update(method=method, url=url, headers=headers, body=json_body)
            payload = (
                b'{"choices":[{"message":{"content":"OK"}}],'
                b'"usage":{"prompt_tokens":7,"completion_tokens":3}}'
            )
            return 200, payload

        monkeypatch.setattr(probe, "_http_request", fake_http)
        res = asyncio.run(run_probe(_cfg(), secret="sk-test"))
        assert res["ok"] is True and res["status"] == 200
        assert res["output"] == "OK"
        assert res["tokens_input"] == 7 and res["tokens_output"] == 3
        # path default + auth bearer aplicados
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["headers"].get("Authorization") == "Bearer sk-test"
        assert captured["body"]["messages"][0]["content"] == "Responda apenas: OK"

    def test_http_ping_success(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])

        async def fake_http(method, url, *, headers, json_body, timeout_s):
            assert method == "GET" and json_body is None
            return 204, b""

        monkeypatch.setattr(probe, "_http_request", fake_http)
        res = asyncio.run(run_probe(_cfg(mode="http_ping", base_url="https://x.example", path="/health")))
        assert res["ok"] is True and res["status"] == 204
        assert res["url"].endswith("/health")

    def test_ssrf_blocks_private_ip(self, monkeypatch):
        _patch_resolve(monkeypatch, ["10.0.0.5"])
        called = {"http": False}

        async def fake_http(*a, **k):
            called["http"] = True
            return 200, b"{}"

        monkeypatch.setattr(probe, "_http_request", fake_http)
        res = asyncio.run(run_probe(_cfg()))
        assert res["ok"] is False
        assert "SSRF" in (res["error"] or "")
        assert called["http"] is False  # nem chega a chamar o vendor

    def test_input_override(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        captured = {}

        async def fake_http(method, url, *, headers, json_body, timeout_s):
            captured.update(body=json_body)
            return 200, b'{"choices":[{"message":{"content":"hi"}}]}'

        monkeypatch.setattr(probe, "_http_request", fake_http)
        asyncio.run(run_probe(_cfg(), secret="k", input_text="prompt customizado"))
        assert captured["body"]["messages"][0]["content"] == "prompt customizado"

    def test_non_200_sets_hint(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(probe, "_http_request", _async((401, b"{}")))
        res = asyncio.run(run_probe(_cfg(), secret="bad"))
        assert res["ok"] is False and res["status"] == 401
        assert "Auth falhou" in (res["hint"] or "")

    def test_vendor_error_message_extracted(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        monkeypatch.setattr(
            probe, "_http_request",
            _async((400, b'{"error":{"message":"model not found"}}')),
        )
        res = asyncio.run(run_probe(_cfg()))
        assert res["ok"] is False
        assert res["error"] == "model not found"

    def test_timeout_returns_408(self, monkeypatch):
        import httpx
        _patch_resolve(monkeypatch, ["93.184.216.34"])

        async def boom(*a, **k):
            raise httpx.TimeoutException("slow")

        monkeypatch.setattr(probe, "_http_request", boom)
        res = asyncio.run(run_probe(_cfg()))
        assert res["ok"] is False and res["status"] == 408

    def test_connect_error_fail_soft(self, monkeypatch):
        import httpx
        _patch_resolve(monkeypatch, ["93.184.216.34"])

        async def boom(*a, **k):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(probe, "_http_request", boom)
        res = asyncio.run(run_probe(_cfg()))
        assert res["ok"] is False and res["status"] == 0
        assert "conectar" in (res["error"] or "").lower()


# ═════════════════════════════════════════════════════════════════
# Endpoints — PUT/GET external-adapter + POST external-test-inline
# ═════════════════════════════════════════════════════════════════


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


OWNER = {"id": "owner-1", "role": "user"}
OTHER = {"id": "intruder", "role": "user"}
ROOT = {"id": "root-1", "role": "root"}


def _entry(**over) -> dict:
    base = {
        "id": "ext-1",
        "name": "ChatGPT Enterprise",
        "kind": "external_platform",
        "status": "draft",
        "version": "0.1.0",
        "owner_user_id": "owner-1",
        "visibility": "company",
        "visibility_scope": None,
        "artifact_type": None,
        "artifact_id": None,
        "tags": "[]",
        "adapter_config": "{}",
        "urn": "urn:maestro:default:external_platform:ext-1:0.1.0",
    }
    base.update(over)
    return base


@pytest.fixture
def adapter_storage(monkeypatch):
    """Mock dos repos + queries de adapter usados pelos endpoints."""
    state = {"entry": _entry(), "raw_cfg": {}, "saved": None, "audit": []}

    async def fake_find_by_id(eid):
        e = state["entry"]
        return dict(e) if e and e["id"] == eid else None

    async def fake_audit_create(data):
        state["audit"].append(dict(data))
        return data

    async def fake_get_raw(entry_id):
        return dict(state["raw_cfg"])

    async def fake_update(entry_id, adapter_config):
        state["saved"] = adapter_config
        return {"id": entry_id, "adapter_config": adapter_config}

    monkeypatch.setattr(catalog_entries_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(audit_repo, "create", fake_audit_create)
    monkeypatch.setattr("app.routes.catalog.get_entry_adapter_raw", fake_get_raw)
    monkeypatch.setattr("app.routes.catalog.update_entry_adapter", fake_update)
    return state


class TestPutAdapter:
    def test_owner_saves_and_encrypts(self, adapter_storage):
        c = _client(OWNER)
        r = c.put("/api/v1/catalog/entries/ext-1/external-adapter", json={
            "probe": _cfg(), "secret": "sk-super-secret",
        })
        assert r.status_code == 200
        body = r.json()["probe"]
        # resposta REDIGIDA — sem cipher, com secret_set
        assert "secret_cipher" not in body and body["secret_set"] is True
        # persistido cifrado (enc::), nunca em claro
        saved = adapter_storage["saved"]["probe"]
        assert saved["secret_cipher"].startswith("enc::")
        assert "sk-super-secret" not in saved["secret_cipher"]

    def test_omit_secret_keeps_existing(self, adapter_storage):
        adapter_storage["raw_cfg"] = {"probe": {"secret_cipher": encrypt_secret("old-key")}}
        c = _client(OWNER)
        r = c.put("/api/v1/catalog/entries/ext-1/external-adapter", json={"probe": _cfg()})
        assert r.status_code == 200
        assert adapter_storage["saved"]["probe"]["secret_cipher"] == \
            adapter_storage["raw_cfg"]["probe"]["secret_cipher"]

    def test_non_external_kind_422(self, adapter_storage):
        adapter_storage["entry"] = _entry(kind="agent")
        c = _client(OWNER)
        r = c.put("/api/v1/catalog/entries/ext-1/external-adapter", json={"probe": _cfg()})
        assert r.status_code == 422

    def test_non_owner_403(self, adapter_storage):
        c = _client(OTHER)
        r = c.put("/api/v1/catalog/entries/ext-1/external-adapter", json={"probe": _cfg()})
        assert r.status_code == 403

    def test_published_409(self, adapter_storage):
        adapter_storage["entry"] = _entry(status="published")
        c = _client(OWNER)
        r = c.put("/api/v1/catalog/entries/ext-1/external-adapter", json={"probe": _cfg()})
        assert r.status_code == 409

    def test_root_can_save(self, adapter_storage):
        c = _client(ROOT)
        r = c.put("/api/v1/catalog/entries/ext-1/external-adapter", json={"probe": _cfg(), "secret": "k"})
        assert r.status_code == 200


class TestGetAdapter:
    def test_owner_reads_redacted(self, adapter_storage):
        adapter_storage["entry"] = _entry(
            adapter_config='{"probe": {"mode": "openai_chat", "secret_cipher": "enc::zzz"}}'
        )
        c = _client(OWNER)
        r = c.get("/api/v1/catalog/entries/ext-1/external-adapter")
        assert r.status_code == 200
        probe_body = r.json()["probe"]
        assert probe_body["secret_set"] is True
        assert "secret_cipher" not in probe_body

    def test_non_external_404(self, adapter_storage):
        adapter_storage["entry"] = _entry(kind="agent")
        c = _client(OWNER)
        assert c.get("/api/v1/catalog/entries/ext-1/external-adapter").status_code == 404


class TestTestInline:
    def test_owner_runs_probe_with_body_secret(self, adapter_storage, monkeypatch):
        captured = {}

        async def fake_run(config, *, secret="", input_text="", allow_http=False):
            captured.update(secret=secret, input_text=input_text)
            return {"ok": True, "status": 200, "latency_ms": 12.0, "output": "OK"}

        monkeypatch.setattr("app.routes.catalog.run_probe", fake_run)
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-test-inline", json={
            "probe": _cfg(), "secret": "sk-live", "input": "ping",
        })
        assert r.status_code == 200 and r.json()["ok"] is True
        assert captured["secret"] == "sk-live" and captured["input_text"] == "ping"

    def test_falls_back_to_stored_cipher(self, adapter_storage, monkeypatch):
        adapter_storage["raw_cfg"] = {"probe": {"secret_cipher": "enc::stored"}}
        captured = {}

        async def fake_run(config, *, secret="", input_text="", allow_http=False):
            captured.update(secret=secret)
            return {"ok": True, "status": 200}

        monkeypatch.setattr("app.routes.catalog.run_probe", fake_run)
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-test-inline", json={"probe": _cfg()})
        assert r.status_code == 200
        assert captured["secret"] == "enc::stored"

    def test_non_owner_403(self, adapter_storage, monkeypatch):
        monkeypatch.setattr("app.routes.catalog.run_probe", _async({"ok": True}))
        c = _client(OTHER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-test-inline", json={"probe": _cfg()})
        assert r.status_code == 403

    def test_non_external_422(self, adapter_storage, monkeypatch):
        adapter_storage["entry"] = _entry(kind="recipe")
        monkeypatch.setattr("app.routes.catalog.run_probe", _async({"ok": True}))
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-test-inline", json={"probe": _cfg()})
        assert r.status_code == 422

    def test_test_inline_allowed_when_published(self, adapter_storage, monkeypatch):
        # diferente do PUT: testar é permitido em qualquer status (é dev)
        adapter_storage["entry"] = _entry(status="published")
        monkeypatch.setattr("app.routes.catalog.run_probe", _async({"ok": True, "status": 200}))
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/external-test-inline", json={"probe": _cfg()})
        assert r.status_code == 200


# ═════════════════════════════════════════════════════════════════
# PR2 — POST /entries/{id}/probe ("Provar Capacidade")
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def probe_storage(monkeypatch):
    """Mock dos repos + queries + background task usados por POST /probe."""
    state = {
        "entry": _entry(status="published"),
        "raw_cfg": {"probe": {
            "mode": "openai_chat", "base_url": "https://api.vendor.example",
            "test_prompt": "Responda apenas: OK", "secret_cipher": "enc::stored",
        }},
        "created": None,
        "bg_calls": [],
    }

    async def fake_find_by_id(eid):
        e = state["entry"]
        return dict(e) if e and e["id"] == eid else None

    async def fake_audit_create(data):
        return data

    async def fake_get_raw(entry_id):
        return dict(state["raw_cfg"])

    async def fake_create_execution(*, recipe_entry_id, consumer_user_id, input_text, is_sandbox=False):
        row = {
            "id": "exec-probe-1", "recipe_entry_id": recipe_entry_id,
            "consumer_user_id": consumer_user_id, "input": input_text,
            "is_sandbox": is_sandbox, "started_at": None, "status": "running",
        }
        state["created"] = dict(row)
        return dict(row)

    async def fake_probe_bg(**kwargs):
        state["bg_calls"].append(kwargs)

    monkeypatch.setattr(catalog_entries_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(audit_repo, "create", fake_audit_create)
    monkeypatch.setattr("app.routes.catalog.get_entry_adapter_raw", fake_get_raw)
    monkeypatch.setattr("app.routes.catalog.create_execution", fake_create_execution)
    monkeypatch.setattr("app.catalog.executor.probe_external_platform", fake_probe_bg)
    return state


class TestProbeEndpoint:
    def test_owner_dispatches_sandbox_execution(self, probe_storage):
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/probe", json={"input": "olá"})
        assert r.status_code == 202
        body = r.json()
        assert body["execution_id"] == "exec-probe-1" and body["is_sandbox"] is True
        # execução criada como sandbox, com o input fornecido
        assert probe_storage["created"]["is_sandbox"] is True
        assert probe_storage["created"]["input"] == "olá"

    def test_input_falls_back_to_test_prompt(self, probe_storage):
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/probe", json={})
        assert r.status_code == 202
        assert probe_storage["created"]["input"] == "Responda apenas: OK"

    def test_no_adapter_configured_422(self, probe_storage):
        probe_storage["raw_cfg"] = {}  # sem probe.base_url
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/probe", json={})
        assert r.status_code == 422

    def test_non_owner_403(self, probe_storage):
        c = _client(OTHER)
        r = c.post("/api/v1/catalog/entries/ext-1/probe", json={})
        assert r.status_code == 403

    def test_non_external_422(self, probe_storage):
        probe_storage["entry"] = _entry(kind="recipe", status="published")
        c = _client(OWNER)
        r = c.post("/api/v1/catalog/entries/ext-1/probe", json={})
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════
# PR2 — executor probe_external_platform (grava 1 step + finaliza)
# ═════════════════════════════════════════════════════════════════


class TestProbeExecutor:
    def _patch_exec(self, monkeypatch, probe_result):
        import app.catalog.executor as ex
        steps, finals = [], []

        async def fake_run(config, *, secret="", input_text="", allow_http=False):
            return probe_result

        async def fake_append(execution_id, step):
            steps.append(step)

        async def fake_finalize(execution_id, *, status, total_cost_usd, total_latency_ms, error_message=None):
            finals.append({"status": status, "error": error_message, "latency": total_latency_ms})

        monkeypatch.setattr("app.catalog.external_probe.run_probe", fake_run)
        monkeypatch.setattr(ex, "append_step_result", fake_append)
        monkeypatch.setattr(ex, "finalize_execution", fake_finalize)
        return steps, finals

    def test_success_completes(self, monkeypatch):
        from app.catalog.executor import probe_external_platform
        steps, finals = self._patch_exec(monkeypatch, {
            "ok": True, "status": 200, "latency_ms": 30,
            "output": "OK", "tokens_input": 5, "tokens_output": 2,
        })
        asyncio.run(probe_external_platform(
            execution_id="e1", entry_id="ext-1", entry_name="ChatGPT",
            config={"base_url": "https://api.x", "model": "gpt-4o-mini"},
            secret="enc::s", user_input="oi",
        ))
        assert len(steps) == 1
        assert steps[0]["status"] == "success" and steps[0]["output"] == "OK"
        assert steps[0]["tokens_total"] == 7 and steps[0]["target_name"] == "ChatGPT"
        assert finals[0]["status"] == "completed"

    def test_failure_marks_failed(self, monkeypatch):
        from app.catalog.executor import probe_external_platform
        steps, finals = self._patch_exec(monkeypatch, {
            "ok": False, "status": 401, "latency_ms": 12,
            "error": "Auth falhou", "output": "",
        })
        asyncio.run(probe_external_platform(
            execution_id="e1", entry_id="ext-1", entry_name="ChatGPT",
            config={"base_url": "https://api.x"}, secret="", user_input="oi",
        ))
        assert steps[0]["status"] == "error" and steps[0]["error"] == "Auth falhou"
        assert finals[0]["status"] == "failed" and finals[0]["error"] == "Auth falhou"
