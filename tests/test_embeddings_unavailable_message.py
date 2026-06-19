"""A mensagem do 503 de embeddings deve ser FIEL ao provider ativo.

Regressão: antes a ingestão sempre dizia "Azure OpenAI embeddings indisponível.
Verifique AZURE_OPENAI_API_KEY/ENDPOINT" mesmo quando o provider configurado era
qwen3 — o que induzia o operador a procurar chaves Azure inexistentes quando o
problema real era o endpoint do hub qwen3 inacessível.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.evidence.embedder as embedder
from app.evidence.embedder import embeddings_unavailable_detail


def _patch_provider(monkeypatch, provider: str):
    monkeypatch.setattr(
        embedder, "get_settings", lambda: SimpleNamespace(embedding_provider=provider)
    )


def test_message_mentions_qwen3_when_provider_qwen3(monkeypatch):
    _patch_provider(monkeypatch, "qwen3")
    msg = embeddings_unavailable_detail()
    assert "qwen3" in msg
    # não deve mandar o usuário checar Azure quando o provider é qwen3
    assert "AZURE_OPENAI_API_KEY" not in msg


def test_message_mentions_azure_when_provider_azure(monkeypatch):
    _patch_provider(monkeypatch, "azure")
    msg = embeddings_unavailable_detail()
    assert "azure" in msg.lower()
    assert "AZURE_OPENAI_API_KEY" in msg


def test_message_defaults_to_azure_when_provider_missing(monkeypatch):
    # provider None/ausente cai no default azure (comportamento histórico)
    monkeypatch.setattr(
        embedder, "get_settings", lambda: SimpleNamespace(embedding_provider=None)
    )
    msg = embeddings_unavailable_detail()
    assert "azure" in msg.lower()
