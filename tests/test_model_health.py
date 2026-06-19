"""Saúde dos modelos: resolve o roteamento, deduplica e sonda cada modelo.

Cobre o mapeamento papel→provider/model, a lógica de all_ok/any_fallback e a
DEDUPLICAÇÃO de sondas (modelos repetidos no roteamento são sondados 1x).
"""
from __future__ import annotations

import pytest

import app.core.model_health as mh


@pytest.fixture(autouse=True)
def _reset_cache():
    mh._cache = None
    mh._cache_at = 0.0
    yield
    mh._cache = None
    mh._cache_at = 0.0


_ROUTING = {
    "tool_calling": "azure/gpt-4o",
    "reasoning": "gpt-oss-120b/openai/gpt-oss-120b",
    "instruct": "gpt-oss-20b/openai/gpt-oss-20b",
    "classification": "gpt-oss-20b/openai/gpt-oss-20b",
    "skill_generation": "azure/gpt-4o",
    "multimodal_fallback": "azure/gpt-4o",
}


def _patch_routing(monkeypatch):
    async def fake_routing():
        return dict(_ROUTING)
    monkeypatch.setattr("app.llm_routing.load_routing", fake_routing)


@pytest.mark.asyncio
async def test_maps_roles_dedups_probes_and_flags_down(monkeypatch):
    _patch_routing(monkeypatch)
    calls = []

    async def fake_chat(provider, model):
        calls.append((provider, model))
        ok = provider == "azure"  # gpt-oss inacessível
        return {"ok": ok, "latency_ms": 10, "error": None if ok else "timeout"}

    async def fake_emb():
        return {"ok": True, "configured": "qwen3", "effective": "azure",
                "fallback_active": True, "dim": 1536, "latency_ms": 5, "error": None}

    monkeypatch.setattr(mh, "_probe_chat", fake_chat)
    monkeypatch.setattr(mh, "_probe_embedding", fake_emb)

    res = await mh.get_model_health(force=True)

    # mapeamento papel→status
    assert res["chat"]["tool_calling"]["ok"] is True
    assert res["chat"]["reasoning"]["ok"] is False
    assert res["chat"]["instruct"]["model"] == "openai/gpt-oss-20b"
    # gpt-oss down → all_ok False; embeddings em fallback → any_fallback True
    assert res["all_ok"] is False
    assert res["any_fallback"] is True
    assert res["embeddings"]["effective"] == "azure"
    # DEDUP: 6 papéis, mas só 3 modelos distintos (azure, oss-120b, oss-20b)
    assert len(calls) == 3
    assert len(set(calls)) == 3


@pytest.mark.asyncio
async def test_all_ok_when_every_model_responds(monkeypatch):
    _patch_routing(monkeypatch)

    async def fake_chat(provider, model):
        return {"ok": True, "latency_ms": 5, "error": None}

    async def fake_emb():
        return {"ok": True, "configured": "azure", "effective": "azure",
                "fallback_active": False, "dim": 1536, "latency_ms": 5, "error": None}

    monkeypatch.setattr(mh, "_probe_chat", fake_chat)
    monkeypatch.setattr(mh, "_probe_embedding", fake_emb)

    res = await mh.get_model_health(force=True)
    assert res["all_ok"] is True
    assert res["any_fallback"] is False


@pytest.mark.asyncio
async def test_cache_avoids_reprobing(monkeypatch):
    _patch_routing(monkeypatch)
    n = {"chat": 0, "emb": 0}

    async def fake_chat(provider, model):
        n["chat"] += 1
        return {"ok": True, "latency_ms": 1, "error": None}

    async def fake_emb():
        n["emb"] += 1
        return {"ok": True, "configured": "azure", "effective": "azure",
                "fallback_active": False, "dim": 1536, "latency_ms": 1, "error": None}

    monkeypatch.setattr(mh, "_probe_chat", fake_chat)
    monkeypatch.setattr(mh, "_probe_embedding", fake_emb)

    await mh.get_model_health(force=True)
    first = (n["chat"], n["emb"])
    await mh.get_model_health()  # sem force → cache, não re-sonda
    assert (n["chat"], n["emb"]) == first
