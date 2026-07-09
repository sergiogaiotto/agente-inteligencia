"""Detecção visível do drift de dimensão do vector store (incidente Aurora).

Coluna vector(1536) + modelo de embedding ativo produzindo 1024: o upsert de
vetores falhava em silêncio (ingest HTTP 200 com partial=true) e a busca
degradava para BM25-only sem nenhum aviso. Agora:
- GET /api/v1/rag/health expõe status/dim_actual/dim_expected/dim_drift/hint
  no TOPO da resposta (UI e monitores não escavam o objeto aninhado);
- /rag mostra banner com CTA "Reindexar agora" quando dim_drift;
- o abort do upsert loga ERROR estruturado (event=pgvector.upsert.blocked_dim_mismatch).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────
# GET /api/v1/rag/health — campos de drift no topo
# ────────────────────────────────────────────────────────────────


def _health_response(monkeypatch, info):
    import asyncio
    from app.routes import dashboard

    async def fake_collection_info():
        return info

    monkeypatch.setattr(
        "app.evidence.pgvector_store.collection_info", fake_collection_info
    )
    return asyncio.run(dashboard.rag_health())


class TestRagHealthDrift:
    def test_drift_surfaces_top_level(self, monkeypatch):
        body = _health_response(monkeypatch, {
            "name": "evidence_chunks.embedding", "exists": True,
            "points_count": 0, "status": "drift",
            "dim_actual": 1536, "dim_expected": 1024, "dim_match": False,
            "backend": "pgvector",
        })
        assert body["dim_drift"] is True
        assert body["status"] == "drift"
        assert body["dim_actual"] == 1536
        assert body["dim_expected"] == 1024
        assert body["hint"] and "Reindex" in body["hint"]
        # back-compat preservado
        assert body["qdrant_collection"]["dim_match"] is False
        assert body["vector_collection"]["status"] == "drift"

    def test_green_has_no_drift_nor_hint(self, monkeypatch):
        body = _health_response(monkeypatch, {
            "name": "evidence_chunks.embedding", "exists": True,
            "points_count": 42, "status": "green",
            "dim_actual": 1024, "dim_expected": 1024, "dim_match": True,
            "backend": "pgvector",
        })
        assert body["dim_drift"] is False
        assert body["status"] == "green"
        assert body["hint"] is None
        assert body["points_count"] == 42

    def test_unavailable_backend(self, monkeypatch):
        body = _health_response(monkeypatch, None)
        assert body["rag_available"] is False
        assert body["dim_drift"] is False
        assert body["status"] == "unavailable"


# ────────────────────────────────────────────────────────────────
# upsert_chunks bloqueado → ERROR estruturado (não warning perdido)
# ────────────────────────────────────────────────────────────────


class TestUpsertBlockedLogsError:
    @pytest.mark.asyncio
    async def test_blocked_upsert_logs_structured_error(self, monkeypatch, caplog):
        from app.evidence import pgvector_store

        async def fake_ensure():
            return False

        async def fake_column_dim():
            return 1536

        monkeypatch.setattr(pgvector_store, "ensure_embedding_column", fake_ensure)
        monkeypatch.setattr(pgvector_store, "_column_dim", fake_column_dim)
        monkeypatch.setattr(pgvector_store, "get_active_embedding_dim", lambda: 1024)

        with caplog.at_level(logging.ERROR, logger="app.evidence.pgvector_store"):
            n = await pgvector_store.upsert_chunks(
                [{"id": "c1", "embedding": [0.1] * 1024, "source_id": "ks1"}]
            )
        assert n == 0
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "abort deve logar ERROR (era warning que se perdia)"
        rec = errors[0]
        assert getattr(rec, "event", "") == "pgvector.upsert.blocked_dim_mismatch"
        assert getattr(rec, "dim_actual", None) == 1536
        assert getattr(rec, "dim_expected", None) == 1024


# ────────────────────────────────────────────────────────────────
# UI /rag — smoke estático do banner + CTA (convenção do repo)
# ────────────────────────────────────────────────────────────────


class TestUISmokeDriftBanner:
    def _html(self):
        return Path("app/templates/pages/evidence.html").read_text(encoding="utf-8")

    def test_state_and_fetch(self):
        html = self._html()
        assert "ragHealth: null" in html
        assert "reindexRunning: false" in html
        assert "/api/v1/rag/health" in html

    def test_banner_bound_to_drift(self):
        html = self._html()
        assert 'data-testid="rag-drift-banner"' in html
        assert "ragHealth.dim_drift" in html
        assert "ragHealth.dim_actual" in html
        assert "ragHealth.dim_expected" in html

    def test_reindex_cta_with_confirm(self):
        html = self._html()
        assert 'data-testid="rag-reindex-btn"' in html
        assert "runReindex()" in html
        assert "/api/v1/evidence/reindex" in html
        # confirmação in-app (F2 do E2E: uiConfirm no lugar do confirm nativo,
        # ver tests/test_no_native_confirm_dialogs.py)
        assert "uiConfirm({message: 'Reindexar o vector store?" in html
