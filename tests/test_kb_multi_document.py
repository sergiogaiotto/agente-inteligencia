"""Testes do PR #227 — múltiplos documentos por KB (RAG textual).

Cobertura:

1. `list_documents_for_source`:
   - Agrupa por metadata.source_doc_id (mock _get_pool com rows fake)
   - Trata chunks sem metadata como "legacy" via COALESCE($2)
   - Ordena por ingested_at desc

2. `delete_document`:
   - Apaga só chunks do source_doc_id alvo
   - Para `_legacy_` apaga chunks com metadata NULL
   - Idempotente (chunks_deleted=0 quando não há nada)

3. Endpoints HTTP:
   - GET /knowledge-sources/{ks_id}/documents → 200 com shape correto, 404 para KS inexistente
   - DELETE /knowledge-sources/{ks_id}/documents/{doc_id} → 200, idempotente

4. Constante `LEGACY_DOC_ID` exposta e estável.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.evidence import ingest as ingest_mod
from app.evidence.ingest import (
    LEGACY_DOC_ID,
    delete_document,
    list_documents_for_source,
)


# ─── Fixture: mock do pool / connection ────────────────────────


@pytest.fixture
def fake_pool(monkeypatch):
    """Pool fake que captura query+args e devolve rows controladas.

    Para `list_documents_for_source` o teste configura `next_rows`.
    Para `delete_document` o teste configura `next_execute_response`.
    Sempre captura a SQL e os args do último uso em `last_query`/`last_args`.
    """
    state = {
        "next_rows": [],
        "next_execute_response": "DELETE 0",
        "last_query": None,
        "last_args": None,
    }

    class _Conn:
        async def fetch(self, sql, *args):
            state["last_query"] = sql
            state["last_args"] = args
            return state["next_rows"]

        async def execute(self, sql, *args):
            state["last_query"] = sql
            state["last_args"] = args
            return state["next_execute_response"]

    class _Acquire:
        async def __aenter__(self): return _Conn()
        async def __aexit__(self, *a): return None

    class _Pool:
        def acquire(self): return _Acquire()

    monkeypatch.setattr(ingest_mod, "_get_pool", lambda: _Pool())
    return state


# ─── 1. list_documents_for_source ──────────────────────────────


class TestListDocuments:
    def test_aggregates_by_source_doc_id(self, fake_pool):
        fake_pool["next_rows"] = [
            {
                "source_doc_id": "doc-a", "source_filename": "Manual.pdf",
                "source_format": "pdf", "source_uri": None,
                "ingested_at": "2026-05-31T10:00:00+00:00",
                "chunks_count": 12, "tokens_total": 6000,
            },
            {
                "source_doc_id": "doc-b", "source_filename": "FAQ.md",
                "source_format": "md", "source_uri": None,
                "ingested_at": "2026-05-30T09:00:00+00:00",
                "chunks_count": 5, "tokens_total": 1500,
            },
        ]
        docs = asyncio.run(list_documents_for_source("ks-x"))
        assert len(docs) == 2
        assert docs[0]["source_doc_id"] == "doc-a"
        assert docs[0]["chunks_count"] == 12
        assert docs[0]["is_legacy"] is False
        assert docs[1]["source_filename"] == "FAQ.md"

    def test_legacy_chunks_appear_as_legacy_doc(self, fake_pool):
        """Chunks sem metadata (legados pré-PR #227) aparecem como doc único
        com `is_legacy=True`. SQL usa COALESCE($2) para agregá-los."""
        fake_pool["next_rows"] = [
            {
                "source_doc_id": LEGACY_DOC_ID,
                "source_filename": None, "source_format": None,
                "source_uri": None, "ingested_at": None,
                "chunks_count": 30, "tokens_total": 15000,
            },
        ]
        docs = asyncio.run(list_documents_for_source("ks-legacy"))
        assert len(docs) == 1
        assert docs[0]["is_legacy"] is True
        assert docs[0]["source_doc_id"] == LEGACY_DOC_ID
        # SQL usou o sentinel em $2
        assert fake_pool["last_args"][1] == LEGACY_DOC_ID

    def test_empty_ks_returns_empty_list(self, fake_pool):
        fake_pool["next_rows"] = []
        docs = asyncio.run(list_documents_for_source("ks-empty"))
        assert docs == []


# ─── 2. delete_document ────────────────────────────────────────


class TestDeleteDocument:
    def test_delete_targeted_doc_uses_metadata_filter(self, fake_pool):
        fake_pool["next_execute_response"] = "DELETE 7"
        result = asyncio.run(delete_document("ks-x", "doc-a"))
        assert result["chunks_deleted"] == 7
        assert result["source_doc_id"] == "doc-a"
        # SQL apaga COM filtro por metadata->>'source_doc_id'
        assert "metadata->>'source_doc_id' = $2" in fake_pool["last_query"]
        assert fake_pool["last_args"] == ("ks-x", "doc-a")

    def test_delete_legacy_apaga_metadata_null(self, fake_pool):
        fake_pool["next_execute_response"] = "DELETE 30"
        result = asyncio.run(delete_document("ks-x", LEGACY_DOC_ID))
        assert result["chunks_deleted"] == 30
        # Para legacy, SQL filtra por metadata NULL (não usa o doc_id literal)
        assert "metadata IS NULL" in fake_pool["last_query"]
        assert "metadata->>'source_doc_id' IS NULL" in fake_pool["last_query"]

    def test_delete_idempotent_when_nothing_matches(self, fake_pool):
        fake_pool["next_execute_response"] = "DELETE 0"
        result = asyncio.run(delete_document("ks-x", "doc-ghost"))
        assert result["chunks_deleted"] == 0

    def test_delete_handles_malformed_execute_response(self, fake_pool):
        """asyncpg sempre devolve 'DELETE N' mas defesa contra qualquer drift."""
        fake_pool["next_execute_response"] = "DELETE"
        result = asyncio.run(delete_document("ks-x", "doc"))
        # Não levanta — fallback para 0
        assert "chunks_deleted" in result


# ─── 3. Endpoints HTTP ─────────────────────────────────────────


def _make_app(monkeypatch):
    """FastAPI mínimo com router de dashboard. Mocka knowledge_repo.find_by_id."""
    from app.routes import dashboard

    async def fake_find_by_id(ks_id):
        return None if ks_id == "ghost" else {"id": ks_id, "name": "K"}

    monkeypatch.setattr(dashboard.knowledge_repo, "find_by_id", fake_find_by_id)

    app = FastAPI()
    app.include_router(dashboard.router)
    return app


class TestDocumentsEndpoints:
    def test_list_endpoint_returns_documents(self, fake_pool, monkeypatch):
        fake_pool["next_rows"] = [
            {
                "source_doc_id": "doc-1", "source_filename": "x.pdf",
                "source_format": "pdf", "source_uri": None,
                "ingested_at": "2026-05-31T10:00:00+00:00",
                "chunks_count": 3, "tokens_total": 900,
            },
        ]
        client = TestClient(_make_app(monkeypatch))
        r = client.get("/api/v1/knowledge-sources/ks-1/documents")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["documents"][0]["source_filename"] == "x.pdf"

    def test_list_endpoint_returns_404_for_unknown_ks(self, fake_pool, monkeypatch):
        client = TestClient(_make_app(monkeypatch))
        r = client.get("/api/v1/knowledge-sources/ghost/documents")
        assert r.status_code == 404

    def test_delete_endpoint_returns_count(self, fake_pool, monkeypatch):
        fake_pool["next_execute_response"] = "DELETE 4"
        client = TestClient(_make_app(monkeypatch))
        r = client.delete("/api/v1/knowledge-sources/ks-1/documents/doc-a")
        assert r.status_code == 200
        assert r.json()["chunks_deleted"] == 4

    def test_delete_endpoint_legacy_doc_id_works(self, fake_pool, monkeypatch):
        fake_pool["next_execute_response"] = "DELETE 10"
        client = TestClient(_make_app(monkeypatch))
        r = client.delete(
            f"/api/v1/knowledge-sources/ks-1/documents/{LEGACY_DOC_ID}"
        )
        assert r.status_code == 200
        # SQL apropriada disparada
        assert "metadata IS NULL" in fake_pool["last_query"]

    def test_delete_endpoint_returns_404_for_unknown_ks(
        self, fake_pool, monkeypatch,
    ):
        client = TestClient(_make_app(monkeypatch))
        r = client.delete("/api/v1/knowledge-sources/ghost/documents/anything")
        assert r.status_code == 404


# ─── 4. Sentinel constant ──────────────────────────────────────


def test_legacy_doc_id_constant_value():
    """Sentinel é parte do contrato com a UI — não pode mudar sem coordenação."""
    assert LEGACY_DOC_ID == "_legacy_"
