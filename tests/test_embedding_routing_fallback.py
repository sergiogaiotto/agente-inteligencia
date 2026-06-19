"""Roteamento/fallback de embeddings (paridade com a resiliência do LLM).

Quando o provider de embedding CONFIGURADO está inacessível (ex.: hub qwen3 fora
da rede), o embedder ROTEIA para o fallback alcançável (ex.: azure). A dimensão
ativa segue o provider EFETIVO — crítico para a coluna pgvector bater com os
vetores realmente gerados.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.evidence.embedder as embedder


@pytest.fixture(autouse=True)
def _reset_embedder_state():
    # Garante estado limpo entre testes (globais de cache do módulo).
    embedder._embedder = None
    embedder._effective_provider = None
    yield
    embedder._embedder = None
    embedder._effective_provider = None


def _patch_settings(monkeypatch, **kw):
    kw.setdefault("qwen3_dimensions", 0)
    monkeypatch.setattr(embedder, "get_settings", lambda: SimpleNamespace(**kw))


# ── cadeia de roteamento ──────────────────────────────────────────


def test_chain_qwen3_falls_back_to_azure(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="qwen3", embedding_fallback_provider="")
    assert embedder._embedding_chain() == ["qwen3", "azure"]


def test_chain_azure_falls_back_to_qwen3(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="azure", embedding_fallback_provider="")
    assert embedder._embedding_chain() == ["azure", "qwen3"]


def test_chain_respects_explicit_fallback(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="qwen3", embedding_fallback_provider="azure")
    assert embedder._embedding_chain() == ["qwen3", "azure"]


# ── dimensão segue o provider efetivo ─────────────────────────────


def test_active_dim_follows_effective_provider(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="qwen3", qwen3_dimensions=0)
    # Sem efetivo resolvido → dim do configurado (qwen3 = 1024).
    assert embedder.get_active_embedding_dim() == 1024
    # Efetivo resolvido como azure (fallback) → dim azure (1536).
    embedder._effective_provider = "azure"
    assert embedder.get_active_embedding_dim() == 1536


# ── fallback no embed real (builders mockados) ────────────────────


class _FakeEmbedder:
    def __init__(self, dim=1536, fail=False):
        self.dim = dim
        self.fail = fail

    async def aembed_documents(self, texts):
        if self.fail:
            raise ConnectionError("All connection attempts failed")
        return [[0.0] * self.dim for _ in texts]

    async def aembed_query(self, text):
        if self.fail:
            raise ConnectionError("All connection attempts failed")
        return [0.0] * self.dim


@pytest.mark.asyncio
async def test_embed_texts_routes_to_fallback_when_primary_unreachable(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="qwen3", embedding_fallback_provider="")
    # qwen3 (primário) inacessível; azure (fallback) responde.
    monkeypatch.setattr(embedder, "_BUILDERS", {
        "qwen3": lambda: _FakeEmbedder(dim=1024, fail=True),
        "azure": lambda: _FakeEmbedder(dim=1536, fail=False),
    })
    vectors = await embedder.embed_texts(["um", "dois"])
    assert vectors is not None
    assert len(vectors) == 2 and len(vectors[0]) == 1536
    # provider efetivo passou a ser o fallback → dimensão ativa segue ele
    assert embedder._effective_provider == "azure"
    assert embedder.get_active_embedding_dim() == 1536


@pytest.mark.asyncio
async def test_embed_texts_returns_none_when_whole_chain_fails(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="qwen3", embedding_fallback_provider="")
    monkeypatch.setattr(embedder, "_BUILDERS", {
        "qwen3": lambda: _FakeEmbedder(fail=True),
        "azure": lambda: _FakeEmbedder(fail=True),
    })
    assert await embedder.embed_texts(["x"]) is None
    assert embedder._effective_provider is None
