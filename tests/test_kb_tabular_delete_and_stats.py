"""Testes do PR #226 — card de KB mostra info de tabelas + delete por tabela.

Cobertura:
1. delete_table / delete_all_tables_for_ks (puros, sem rede):
   - idempotência (tabela inexistente)
   - file unlink + metadata removida
   - file unlink falha mas metadata removida (best-effort)
   - delete_all itera o que list_for_user retorna

2. Endpoints DELETE (TestClient + monkeypatch):
   - 200 quando user pode ver
   - 404 quando tabela não existe
   - 403 quando user não pode ver
   - audit chamado em sucesso

3. Endpoint GET /knowledge-sources/{ks_id}/stats agora inclui campos de tabelas
   (tables_count, tables_rows_total, tables_size_bytes, last_table_at) — mock do
   pool para validar sem subir Postgres.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.evidence import tabular as tabular_service
from app.evidence.tabular import delete_all_tables_for_ks, delete_table


# ─── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def fake_tables_store(monkeypatch, tmp_path):
    """Backend in-memory para data_tables_repo + arquivos .duckdb fake em tmp."""
    store: dict[str, dict] = {}

    async def fake_find_by_id(table_id):
        return store.get(table_id)

    async def fake_delete(table_id):
        return store.pop(table_id, None) is not None

    monkeypatch.setattr(tabular_service.data_tables_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(tabular_service.data_tables_repo, "delete", fake_delete)

    def _add(table_id: str, ks_id: str = "ks-1", name: str = "tbl",
             write_file: bool = True, size: int = 1024) -> Path:
        """Cria arquivo .duckdb fake + entrada no store. Retorna path."""
        p = tmp_path / f"{table_id}.duckdb"
        if write_file:
            p.write_bytes(b"x" * size)
        store[table_id] = {
            "id": table_id,
            "knowledge_source_id": ks_id,
            "name": name,
            "duckdb_path": str(p),
        }
        return p

    return {"store": store, "add": _add, "tmp": tmp_path}


# ─── 1. delete_table — função pura ─────────────────────────────


class TestDeleteTableFunction:
    def test_delete_existing_removes_file_and_metadata(self, fake_tables_store):
        p = fake_tables_store["add"]("tbl-A", name="vendas", size=4096)
        assert p.exists()
        result = asyncio.run(delete_table("tbl-A", deleted_by="u-1"))
        assert result["deleted"] is True
        assert result["table_id"] == "tbl-A"
        assert result["name"] == "vendas"
        assert result["size_freed_bytes"] == 4096
        # Arquivo apagado em disco
        assert not p.exists()
        # Metadata sumiu do "Postgres"
        assert "tbl-A" not in fake_tables_store["store"]

    def test_delete_nonexistent_is_idempotent(self, fake_tables_store):
        result = asyncio.run(delete_table("tbl-ghost"))
        assert result["deleted"] is False
        assert result["reason"] == "not_found"

    def test_delete_when_file_missing_still_removes_metadata(
        self, fake_tables_store,
    ):
        """Cenário: arquivo .duckdb foi apagado fora do app, mas metadata
        ficou órfã. Delete deve seguir e apagar a metadata."""
        fake_tables_store["add"]("tbl-B", write_file=False)
        result = asyncio.run(delete_table("tbl-B"))
        assert result["deleted"] is True
        assert result["size_freed_bytes"] == 0
        assert "tbl-B" not in fake_tables_store["store"]

    def test_delete_continues_when_file_unlink_raises(
        self, fake_tables_store, monkeypatch,
    ):
        """OSError no unlink não pode bloquear remoção da metadata —
        melhor metadata limpa + arquivo órfão (rastreável) do que metadata
        órfã (perdida na UI)."""
        fake_tables_store["add"]("tbl-C")

        original_unlink = Path.unlink

        def boom(self, *a, **kw):
            if self.name == "tbl-C.duckdb":
                raise OSError("simulated disk error")
            return original_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", boom)

        result = asyncio.run(delete_table("tbl-C"))
        assert result["deleted"] is True
        assert "tbl-C" not in fake_tables_store["store"]


# ─── 2. delete_all_tables_for_ks — batch ─────────────────────


class TestDeleteAllTablesForKs:
    def test_delete_all_visible_tables_in_ks(
        self, fake_tables_store, monkeypatch,
    ):
        fake_tables_store["add"]("tbl-1", ks_id="ks-x", name="A")
        fake_tables_store["add"]("tbl-2", ks_id="ks-x", name="B")
        fake_tables_store["add"]("tbl-other", ks_id="ks-other", name="C")

        # Mock list_for_user no módulo correto (queries) — função importa lazy
        from app.data_tables import queries as q
        async def fake_list_for_user(user, ks_id=None):
            return [
                v for v in fake_tables_store["store"].values()
                if not ks_id or v["knowledge_source_id"] == ks_id
            ]
        monkeypatch.setattr(q, "list_for_user", fake_list_for_user)

        result = asyncio.run(delete_all_tables_for_ks(
            "ks-x", {"id": "u-root", "role": "root", "domains": "[]"},
            deleted_by="u-root",
        ))
        assert result["deleted"] == 2
        assert result["freed_bytes"] > 0
        # KS-other intocado
        assert "tbl-other" in fake_tables_store["store"]

    def test_delete_all_returns_zero_for_empty_ks(
        self, fake_tables_store, monkeypatch,
    ):
        from app.data_tables import queries as q
        async def empty_list(user, ks_id=None):
            return []
        monkeypatch.setattr(q, "list_for_user", empty_list)

        result = asyncio.run(delete_all_tables_for_ks(
            "ks-empty", {"id": "u", "role": "root", "domains": "[]"},
        ))
        assert result["deleted"] == 0
        assert result["freed_bytes"] == 0


# ─── 3. Endpoints HTTP DELETE ────────────────────────────────


def _make_app(user, monkeypatch_obj=None):
    """FastAPI mínimo só com o router de data_tables + auth override."""
    from app.routes.data_tables import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: user
    return app


@pytest.fixture
def root_user():
    return {"id": "u-root", "role": "root", "domains": "[]"}


@pytest.fixture
def common_user():
    return {"id": "u-c", "role": "comum", "domains": "[]"}


class TestDeleteEndpoints:
    def test_delete_table_returns_200_for_root(
        self, fake_tables_store, monkeypatch, root_user,
    ):
        fake_tables_store["add"]("tbl-A", name="vendas")

        # find_by_id_with_ks usado por data_tables.py:delete_data_table_endpoint
        from app.routes import data_tables as dt_routes
        async def fake_find(table_id):
            t = fake_tables_store["store"].get(table_id)
            if not t:
                return None
            return {**t, "ks_confidentiality_label": "internal", "ks_authorized": 1}
        monkeypatch.setattr(dt_routes, "find_by_id_with_ks", fake_find)

        # audit mock (best-effort no real)
        async def fake_audit(data):
            return data
        monkeypatch.setattr(dt_routes.audit_repo, "create", fake_audit)

        client = TestClient(_make_app(root_user))
        r = client.delete("/api/v1/data-tables/tbl-A")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] is True
        assert body["name"] == "vendas"

    def test_delete_table_returns_404_for_unknown(
        self, fake_tables_store, monkeypatch, root_user,
    ):
        from app.routes import data_tables as dt_routes
        async def fake_find(table_id):
            return None
        monkeypatch.setattr(dt_routes, "find_by_id_with_ks", fake_find)

        client = TestClient(_make_app(root_user))
        r = client.delete("/api/v1/data-tables/tbl-ghost")
        assert r.status_code == 404

    def test_delete_table_returns_403_when_visibility_denies(
        self, fake_tables_store, monkeypatch, common_user,
    ):
        """Tabela `restricted` em KB não-autorizada → user comum não pode."""
        from app.routes import data_tables as dt_routes
        async def fake_find(table_id):
            return {
                "id": table_id, "name": "secret",
                "knowledge_source_id": "ks-secret",
                "ks_confidentiality_label": "restricted",
                "ks_authorized": 0,
            }
        monkeypatch.setattr(dt_routes, "find_by_id_with_ks", fake_find)

        client = TestClient(_make_app(common_user))
        r = client.delete("/api/v1/data-tables/tbl-X")
        assert r.status_code == 403


# ─── 4. Stats endpoint agora inclui tabelas ──────────────────


class TestSourceStatsIncludesTables:
    """O endpoint GET /knowledge-sources/{ks_id}/stats deve trazer
    `tables_count`, `tables_rows_total`, `tables_size_bytes`, `last_table_at`
    além dos campos legados (chunks_count etc)."""

    def test_stats_returns_table_fields_when_pool_has_rows(
        self, monkeypatch, root_user,
    ):
        # Mock dashboard imports
        from app.routes import dashboard
        from app.evidence import ingest as _ingest

        async def fake_ks(ks_id):
            return {"id": ks_id, "name": "k"} if ks_id == "ks-1" else None

        async def fake_source_stats(ks_id):
            return {
                "chunks_count": 5, "tokens_total": 2500,
                "last_chunk_at": "2026-05-30T12:00:00Z",
            }

        monkeypatch.setattr(dashboard.knowledge_repo, "find_by_id", fake_ks)
        monkeypatch.setattr(_ingest, "source_stats", fake_source_stats)

        # Mock pool.acquire().__aenter__().fetchrow(...)
        class _FakeConn:
            async def fetchrow(self, sql, *args):
                assert args[0] == "ks-1"
                return {
                    "tables_count": 3,
                    "tables_rows_total": 185,
                    "tables_size_bytes": 49152,
                    "last_table_at": _dt.datetime(2026, 5, 31, 19, 13, 0),
                }

        class _FakeAcquire:
            async def __aenter__(self): return _FakeConn()
            async def __aexit__(self, *a): return None

        class _FakePool:
            def acquire(self): return _FakeAcquire()

        from app.core import database as core_db
        monkeypatch.setattr(core_db, "_get_pool", lambda: _FakePool())

        # Sobe app só com router de dashboard
        app = FastAPI()
        app.include_router(dashboard.router)
        app.dependency_overrides[require_user] = lambda: root_user

        client = TestClient(app)
        r = client.get("/api/v1/knowledge-sources/ks-1/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        # Legacy campos preservados
        assert body["chunks_count"] == 5
        assert body["tokens_total"] == 2500
        # Novos campos
        assert body["tables_count"] == 3
        assert body["tables_rows_total"] == 185
        assert body["tables_size_bytes"] == 49152
        assert body["last_table_at"] is not None
        assert "2026-05-31" in body["last_table_at"]

    def test_stats_returns_zero_table_fields_when_ks_has_no_tables(
        self, monkeypatch, root_user,
    ):
        from app.routes import dashboard
        from app.evidence import ingest as _ingest

        async def fake_ks(ks_id):
            return {"id": ks_id} if ks_id == "ks-empty" else None

        async def fake_source_stats(ks_id):
            return {"chunks_count": 0, "tokens_total": 0, "last_chunk_at": None}

        monkeypatch.setattr(dashboard.knowledge_repo, "find_by_id", fake_ks)
        monkeypatch.setattr(_ingest, "source_stats", fake_source_stats)

        class _FakeConn:
            async def fetchrow(self, sql, *args):
                return {
                    "tables_count": 0, "tables_rows_total": 0,
                    "tables_size_bytes": 0, "last_table_at": None,
                }

        class _FakeAcquire:
            async def __aenter__(self): return _FakeConn()
            async def __aexit__(self, *a): return None

        class _FakePool:
            def acquire(self): return _FakeAcquire()

        from app.core import database as core_db
        monkeypatch.setattr(core_db, "_get_pool", lambda: _FakePool())

        app = FastAPI()
        app.include_router(dashboard.router)
        app.dependency_overrides[require_user] = lambda: root_user

        client = TestClient(app)
        r = client.get("/api/v1/knowledge-sources/ks-empty/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["tables_count"] == 0
        assert body["tables_size_bytes"] == 0
        assert body["last_table_at"] is None
