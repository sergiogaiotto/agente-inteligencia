"""PR6 — Plataforma Externa como STEP de recipe (orquestração híbrida).

Cobre _resolve_target (aceita external_platform published+openai_chat configurado;
rejeita sem adapter / http_ping / não-published / kind inválido; agent inalterado)
e _invoke_step/_invoke_external_step (dispatch por kind; mapeia run_probe; falha
levanta para quebrar a chain).

Sem rede/Postgres: find_by_id, get_entry_adapter_raw e run_probe monkeypatchados.
"""
from __future__ import annotations

import asyncio

import pytest

import app.catalog.executor as ex
from app.core.database import catalog_entries_repo


def _async(v):
    async def f(*a, **k):
        return v
    return f


def _ext(status="published", name="ChatGPT"):
    return {"id": "ext-1", "name": name, "kind": "external_platform",
            "status": status, "artifact_id": None}


class TestResolveTargetExternal:
    def _patch(self, monkeypatch, entry, probe=None):
        monkeypatch.setattr(catalog_entries_repo, "find_by_id", _async(entry))
        monkeypatch.setattr("app.catalog.queries.get_entry_adapter_raw",
                            _async({"probe": probe} if probe else {}))

    def test_external_openai_chat_published_ok(self, monkeypatch):
        self._patch(monkeypatch, _ext(), probe={"base_url": "https://api.x", "mode": "openai_chat"})
        entry, reason = asyncio.run(ex._resolve_target("ext-1"))
        assert reason is None and entry["kind"] == "external_platform"

    def test_external_no_adapter_reason(self, monkeypatch):
        self._patch(monkeypatch, _ext(), probe=None)
        _, reason = asyncio.run(ex._resolve_target("ext-1"))
        assert reason and "conexão" in reason

    def test_external_http_ping_rejected(self, monkeypatch):
        self._patch(monkeypatch, _ext(), probe={"base_url": "https://api.x", "mode": "http_ping"})
        _, reason = asyncio.run(ex._resolve_target("ext-1"))
        assert reason and "http_ping" in reason

    def test_external_not_published_rejected(self, monkeypatch):
        self._patch(monkeypatch, _ext(status="draft"), probe={"base_url": "https://api.x", "mode": "openai_chat"})
        _, reason = asyncio.run(ex._resolve_target("ext-1"))
        assert reason and "published" in reason

    def test_skill_kind_rejected(self, monkeypatch):
        monkeypatch.setattr(catalog_entries_repo, "find_by_id",
                            _async({"id": "s", "name": "S", "kind": "skill", "status": "published"}))
        _, reason = asyncio.run(ex._resolve_target("s"))
        assert reason and "external_platform" in reason

    def test_agent_still_ok(self, monkeypatch):
        monkeypatch.setattr(catalog_entries_repo, "find_by_id",
                            _async({"id": "a", "name": "A", "kind": "agent", "status": "published", "artifact_id": "art1"}))
        _, reason = asyncio.run(ex._resolve_target("a"))
        assert reason is None

    def test_agent_without_artifact_reason(self, monkeypatch):
        monkeypatch.setattr(catalog_entries_repo, "find_by_id",
                            _async({"id": "a", "name": "A", "kind": "agent", "status": "published", "artifact_id": None}))
        _, reason = asyncio.run(ex._resolve_target("a"))
        assert reason and "artifact_id" in reason


class TestInvokeExternalStep:
    def test_ok_maps_normalized_output(self, monkeypatch):
        monkeypatch.setattr("app.catalog.queries.get_entry_adapter_raw", _async({"probe": {
            "base_url": "https://api.x", "mode": "openai_chat", "model": "gpt-4o-mini", "secret_cipher": "enc::s"}}))
        captured = {}

        async def fake_run(probe, *, secret="", input_text="", allow_http=False):
            captured.update(secret=secret, input_text=input_text)
            return {"ok": True, "status": 200, "latency_ms": 42, "output": "resposta", "tokens_input": 10, "tokens_output": 5}

        monkeypatch.setattr("app.catalog.external_probe.run_probe", fake_run)
        inv = asyncio.run(ex._invoke_external_step({"id": "ext-1", "kind": "external_platform"}, "olá mundo"))
        assert inv["output"] == "resposta" and inv["duration_ms"] == 42
        assert inv["tokens_total"] == 15 and inv["model"] == "gpt-4o-mini"
        assert inv["final_state"] == "external"
        # current_input vira o prompt; segredo cifrado é repassado
        assert captured["input_text"] == "olá mundo" and captured["secret"] == "enc::s"

    def test_failure_raises_to_break_chain(self, monkeypatch):
        monkeypatch.setattr("app.catalog.queries.get_entry_adapter_raw",
                            _async({"probe": {"base_url": "https://api.x", "mode": "openai_chat"}}))
        monkeypatch.setattr("app.catalog.external_probe.run_probe",
                            _async({"ok": False, "status": 401, "error": "Auth falhou", "output": ""}))
        with pytest.raises(RuntimeError):
            asyncio.run(ex._invoke_external_step({"id": "ext-1", "kind": "external_platform"}, "oi"))

    def test_invoke_step_dispatches_external(self, monkeypatch):
        called = {"ext": False}

        async def fake_ext(target, inp):
            called["ext"] = True
            return {"output": "x", "duration_ms": 1, "tokens_input": 0, "tokens_output": 0,
                    "tokens_total": 0, "provider": None, "model": None,
                    "interaction_id": None, "final_state": "external"}

        monkeypatch.setattr(ex, "_invoke_external_step", fake_ext)
        inv = asyncio.run(ex._invoke_step({"id": "e", "kind": "external_platform"}, "oi", "u"))
        assert called["ext"] is True and inv["final_state"] == "external"
