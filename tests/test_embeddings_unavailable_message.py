"""A mensagem do 503 de embeddings deve refletir a CADEIA de roteamento ativa.

Regressão: antes a ingestão sempre dizia "Azure OpenAI embeddings indisponível.
Verifique AZURE_OPENAI_API_KEY/ENDPOINT" mesmo com o provider configurado em
qwen3 — induzindo o operador a procurar chaves Azure quando o problema real era
o endpoint qwen3 inacessível. Agora a mensagem nomeia a cadeia (primário →
fallback) e só ocorre quando TODOS os providers da cadeia falham.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.evidence.embedder as embedder
from app.evidence.embedder import embeddings_unavailable_detail


def _patch_settings(monkeypatch, **kw):
    monkeypatch.setattr(embedder, "get_settings", lambda: SimpleNamespace(**kw))


def test_message_names_qwen3_chain_when_provider_qwen3(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="qwen3", embedding_fallback_provider="")
    msg = embeddings_unavailable_detail()
    # nomeia qwen3 e a cadeia de roteamento (não é mais uma mensagem só-Azure)
    assert "qwen3" in msg
    assert "roteamento" in msg.lower()


def test_message_mentions_azure_when_provider_azure(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider="azure", embedding_fallback_provider="")
    msg = embeddings_unavailable_detail()
    assert "AZURE_OPENAI_API_KEY" in msg


def test_message_defaults_to_azure_when_provider_missing(monkeypatch):
    _patch_settings(monkeypatch, embedding_provider=None, embedding_fallback_provider="")
    msg = embeddings_unavailable_detail()
    assert "AZURE_OPENAI_API_KEY" in msg
