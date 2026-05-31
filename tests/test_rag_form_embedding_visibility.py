"""UX: footer do RAG form mostra embedding provider+dim ativos.

User reportou (2026-05-30) dúvida sobre dimensionalidade — query embedding
precisa bater com a collection do Qdrant pra busca vetorial funcionar.
A info do provider+dim ativo estava só em /settings; pra debugar quando
RAG não devolve nada, operador tinha que sair do workspace.

Esta PR expõe `rag_meta.embedding_provider` + `rag_meta.embedding_dim`
no CanonicalFormSchema. Footer condicional mostra "embeddings: qwen3 (1024d)"
quando binding_kind=='rag'.

Cobertura:
- normalize_rag_binding popula embedding_provider+dim
- Helpers safe (não propagam exception)
- UI condicional ao binding_kind=='rag'
- Tooltip explica significado pro user power
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ────────────────────────────────────────────────────────────────
# Backend: normalize_rag_binding inclui embedding info
# ────────────────────────────────────────────────────────────────


SAMPLE_SOURCE = {
    "id": "ks-test",
    "name": "Manual RH",
    "source_type": "pdf_archive",
    "confidentiality_label": "internal",
    "kb_mode": "hybrid",
    "authorized": 1,
}


class TestNormalizeRagBindingEmbeddingInfo:
    def test_rag_meta_has_embedding_provider_field(self):
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(SAMPLE_SOURCE)
        assert "embedding_provider" in result["rag_meta"]
        # Default é 'qwen3' ou 'azure'; em qualquer caso é string não-vazia
        provider = result["rag_meta"]["embedding_provider"]
        assert isinstance(provider, str)
        assert provider != ""

    def test_rag_meta_has_embedding_dim_field(self):
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(SAMPLE_SOURCE)
        assert "embedding_dim" in result["rag_meta"]
        dim = result["rag_meta"]["embedding_dim"]
        assert isinstance(dim, int)
        # qwen3 default 1024, azure 1536, fallback 0
        assert dim >= 0

    def test_existing_rag_meta_fields_preserved(self):
        """Não regressão: campos existentes do rag_meta continuam presentes."""
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(SAMPLE_SOURCE)
        meta = result["rag_meta"]
        assert meta["source_type"] == "pdf_archive"
        assert meta["confidentiality"] == "internal"
        assert meta["kb_mode"] == "hybrid"
        assert meta["authorized"] is True


# ────────────────────────────────────────────────────────────────
# Backend: helpers safe (não propagam exception)
# ────────────────────────────────────────────────────────────────


class TestSafeHelpers:
    def test_provider_helper_returns_string_on_failure(self):
        """Se get_settings falha (import circular, env de teste), helper
        cai pra '?' em vez de propagar."""
        from app.workspace import binding_schema

        with patch("app.core.config.get_settings", side_effect=Exception("boom")):
            result = binding_schema._safe_get_embedding_provider()
        assert result == "?"

    def test_dim_helper_returns_zero_on_failure(self):
        from app.workspace import binding_schema

        # Onda Q: helper migrou de qdrant_store pra embedder
        with patch("app.evidence.embedder.get_active_embedding_dim", side_effect=Exception("boom")):
            result = binding_schema._safe_get_embedding_dim()
        assert result == 0

    def test_provider_normalized_to_lowercase(self):
        """Caso settings retornem 'AZURE' por engano, lowercase pra
        consistência com o resto do código."""
        from app.workspace import binding_schema

        class FakeSettings:
            embedding_provider = "QWEN3"

        with patch("app.core.config.get_settings", return_value=FakeSettings()):
            result = binding_schema._safe_get_embedding_provider()
        assert result == "qwen3"


# ────────────────────────────────────────────────────────────────
# Frontend: footer condicional ao binding_kind RAG
# ────────────────────────────────────────────────────────────────


@pytest.fixture
def html():
    return Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")


class TestRagFooterUI:
    def test_embedding_badge_conditional_on_rag(self, html):
        """Badge embeddings só aparece quando binding_kind === 'rag'.
        Pra MCP/API/Tabular, esconde."""
        # Template x-if condiciona por binding_kind
        assert "binding_kind === 'rag'" in html

    def test_embedding_badge_uses_rag_meta_fields(self, html):
        """x-text concatena provider + dim do rag_meta."""
        # Procura no bloco do form footer
        idx = html.find("Sem LLM intermediário")
        assert idx >= 0
        # Janela depois do texto principal
        block = html[idx:idx + 2000]
        assert "rag_meta?.embedding_provider" in block or "rag_meta.embedding_provider" in block
        assert "rag_meta.embedding_dim" in block

    def test_embedding_badge_has_tooltip_explaining_significance(self, html):
        """Tooltip explica pro user que dim deve bater com a collection pgvector
        e que mudança de provider exige reindex.

        Onda Q (2026-05-30) removeu Qdrant e adotou pgvector como backend
        único. O tooltip foi atualizado em PR #222 — este teste agora valida
        a redação nova (caça resíduos "Qdrant" que confundem operadores).
        """
        idx = html.find("Sem LLM intermediário")
        block = html[idx:idx + 2000]
        assert "pgvector" in block
        assert "reindex" in block

    def test_embedding_badge_shows_d_suffix(self, html):
        """Format 'qwen3 (1024d)' — sufixo 'd' deixa claro que é dimensões."""
        idx = html.find("Sem LLM intermediário")
        block = html[idx:idx + 2000]
        # 'd)' aparece no template literal de x-text
        assert "'d)'" in block

    def test_main_footer_text_preserved(self, html):
        """Texto principal "Sem LLM intermediário — payload exato vai pro motor"
        (do PR #210) preservado — não foi quebrado."""
        assert "Sem LLM intermediário — payload exato vai pro motor" in html
