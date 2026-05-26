"""Testes do backend pgvector (PR D — alternativa ao Qdrant).

Cobertura:
- ensure_embedding_column: dim correta → no-op; faltando → CREATE; drift → False sem drop.
- recreate_embedding_column: idempotente, retorna dim_before/dim_after.
- upsert_chunks: UPDATE em batch, conta linhas afetadas.
- search: SQL com filtro source_ids, score normalizado.
- delete_by_source: SET embedding=NULL por source.
- collection_info: dim_match true/false, status, exists.
- Roteador: ingest._get_vector_store e runtime._get_vector_search_fn seguem a flag.

Mocks: asyncpg pool + connection via AsyncMock. Não precisa de Postgres real.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import config as _config
from app.evidence import pgvector_store


@pytest.fixture
def fresh_settings(monkeypatch):
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


def _make_pool_with_con(con):
    """Cria um fake pool cujo pool.acquire() retorna o con dado."""
    pool = MagicMock()

    class _Ctx:
        async def __aenter__(self_): return con
        async def __aexit__(self_, *a): return False

    pool.acquire = MagicMock(return_value=_Ctx())
    return pool


def _mock_con(*, current_dim: int | None = None, points_count: int = 0):
    """Cria um asyncpg connection mock que devolve:
    - current_dim em _column_dim()
    - points_count em COUNT queries
    - update OK em UPDATE queries
    """
    con = MagicMock()
    if current_dim is None:
        con.fetchrow = AsyncMock(return_value=None)
    else:
        con.fetchrow = AsyncMock(return_value={"atttypmod": current_dim})
    con.fetchval = AsyncMock(return_value=points_count)
    con.execute = AsyncMock(return_value="UPDATE 1")
    con.fetch = AsyncMock(return_value=[])
    # transaction context manager (async)

    class _Tx:
        async def __aenter__(self_): return None
        async def __aexit__(self_, *a): return False
    con.transaction = MagicMock(return_value=_Tx())
    return con


# ═════════════════════════════════════════════════════════════════
# ensure_embedding_column
# ═════════════════════════════════════════════════════════════════


class TestEnsureEmbeddingColumn:
    @pytest.mark.asyncio
    async def test_column_missing_creates_with_expected_dim(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # → 1024
        con = _mock_con(current_dim=None)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        ok = await pgvector_store.ensure_embedding_column()
        assert ok is True
        # Chamou CREATE EXTENSION + ALTER + CREATE INDEX
        calls = [str(c.args[0]).lower() for c in con.execute.await_args_list]
        assert any("create extension" in c for c in calls)
        assert any("add column" in c and "vector(1024)" in c for c in calls)
        assert any("create index" in c and "hnsw" in c for c in calls)

    @pytest.mark.asyncio
    async def test_column_matches_returns_true_no_alter(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # → 1024
        con = _mock_con(current_dim=1024)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        ok = await pgvector_store.ensure_embedding_column()
        assert ok is True
        # Não deveria executar nenhum ALTER nesta chamada (só fetchrow do dim)
        # Sequência: fetchrow → return True. Nenhum execute foi chamado.
        assert con.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_dim_mismatch_returns_false_no_destructive(self, monkeypatch, fresh_settings, caplog):
        """Drift de dim: coluna atual=1536 mas provider espera 1024.
        Retorna False sem dropar a coluna."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # → 1024
        con = _mock_con(current_dim=1536)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        import logging
        with caplog.at_level(logging.ERROR, logger="app.evidence.pgvector_store"):
            ok = await pgvector_store.ensure_embedding_column()
        assert ok is False
        # Não há DROP/ALTER destrutivo
        assert con.execute.await_count == 0
        assert any("dim_mismatch" in str(r.__dict__.get("event", ""))
                   or "dim_mismatch" in r.message
                   for r in caplog.records)


# ═════════════════════════════════════════════════════════════════
# recreate_embedding_column
# ═════════════════════════════════════════════════════════════════


class TestRecreateEmbeddingColumn:
    @pytest.mark.asyncio
    async def test_recreate_existing_column(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1536, points_count=42)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        res = await pgvector_store.recreate_embedding_column()
        assert res["ok"] is True
        assert res["dim_before"] == 1536
        assert res["dim_after"] == 1024
        assert res["points_deleted"] == 42

        calls = [str(c.args[0]).lower() for c in con.execute.await_args_list]
        assert any("drop index" in c for c in calls)
        assert any("drop column" in c for c in calls)
        assert any("add column" in c and "vector(1024)" in c for c in calls)
        assert any("create index" in c and "hnsw" in c for c in calls)

    @pytest.mark.asyncio
    async def test_recreate_nonexistent_only_creates(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=None, points_count=0)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        res = await pgvector_store.recreate_embedding_column()
        assert res["ok"] is True
        assert res["dim_before"] is None
        assert res["dim_after"] == 1024
        assert res["points_deleted"] is None

    @pytest.mark.asyncio
    async def test_recreate_sql_failure_returns_error_type(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1536, points_count=10)

        class FakePGError(Exception):
            pass

        con.execute = AsyncMock(side_effect=FakePGError("disk full"))
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        res = await pgvector_store.recreate_embedding_column()
        assert res["ok"] is False
        assert res["error_type"] == "FakePGError"
        assert "disk full" in res["error_message"]


# ═════════════════════════════════════════════════════════════════
# upsert_chunks
# ═════════════════════════════════════════════════════════════════


class TestUpsertChunks:
    @pytest.mark.asyncio
    async def test_returns_count_of_updated_rows(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1024)
        # 3 UPDATEs → "UPDATE 1" cada
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        chunks = [
            {"id": f"c{i}", "embedding": [0.1] * 1024, "source_id": "s1", "ordinal": i}
            for i in range(3)
        ]
        n = await pgvector_store.upsert_chunks(chunks)
        assert n == 3

    @pytest.mark.asyncio
    async def test_aborts_when_column_unavailable(self, monkeypatch, fresh_settings):
        """Drift de dim → ensure retorna False → upsert retorna 0 sem efeito colateral."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        # Dim atual 1536 != esperado 1024
        con = _mock_con(current_dim=1536)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        chunks = [{"id": "c1", "embedding": [0.1] * 1024, "source_id": "s1", "ordinal": 0}]
        n = await pgvector_store.upsert_chunks(chunks)
        assert n == 0
        # Não chegou a UPDATE (só fetchrow do dim_check)
        assert con.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_empty_list_returns_zero(self, monkeypatch, fresh_settings):
        n = await pgvector_store.upsert_chunks([])
        assert n == 0


# ═════════════════════════════════════════════════════════════════
# search
# ═════════════════════════════════════════════════════════════════


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_with_source_filter_includes_in_query(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1024)
        # Resultado simulado: 2 rows
        con.fetch = AsyncMock(return_value=[
            {"chunk_id": "c1", "source_id": "s1", "ordinal": 0, "score": 0.95},
            {"chunk_id": "c2", "source_id": "s1", "ordinal": 1, "score": 0.83},
        ])
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        hits = await pgvector_store.search([0.1] * 1024, top_n=5, source_ids=["s1"])
        assert len(hits) == 2
        assert hits[0]["chunk_id"] == "c1"
        assert hits[0]["score"] == 0.95
        # Confere que a SQL passou pelo path com filtro (ANY)
        sql_called = str(con.fetch.await_args.args[0]).lower()
        assert "any(" in sql_called

    @pytest.mark.asyncio
    async def test_search_without_filter_omits_any(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1024)
        con.fetch = AsyncMock(return_value=[])
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        await pgvector_store.search([0.1] * 1024, top_n=5)
        sql_called = str(con.fetch.await_args.args[0]).lower()
        assert "any(" not in sql_called
        assert "embedding is not null" in sql_called

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_column_unavailable(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=None)  # coluna não existe
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        hits = await pgvector_store.search([0.1] * 1024, top_n=5)
        # ensure_embedding_column vai TENTAR criar (3 execs). Pra teste de search puro
        # com coluna ausente, vou validar que a busca em si não rodou ou rodou vazia
        # — a estratégia atual é "ensure cria coluna, search retorna []".
        # Resultado prático: hits=[] porque a tabela está vazia.
        assert hits == []


# ═════════════════════════════════════════════════════════════════
# delete_by_source
# ═════════════════════════════════════════════════════════════════


class TestDeleteBySource:
    @pytest.mark.asyncio
    async def test_sets_embedding_null(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1024)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        ok = await pgvector_store.delete_by_source("s1")
        assert ok is True
        # Verifica que executou UPDATE ... SET embedding = NULL
        calls = [str(c.args[0]).lower() for c in con.execute.await_args_list]
        assert any("update evidence_chunks set embedding = null" in c for c in calls)

    @pytest.mark.asyncio
    async def test_returns_true_when_column_missing(self, monkeypatch, fresh_settings):
        """Coluna ainda nem foi criada — delete por source é no-op de sucesso."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=None)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        ok = await pgvector_store.delete_by_source("s1")
        # ensure_embedding_column vai tentar criar a coluna e retornar True
        # (porque o mock não dá erro). Aí o UPDATE roda. ok = True final.
        assert ok is True


# ═════════════════════════════════════════════════════════════════
# collection_info
# ═════════════════════════════════════════════════════════════════


class TestCollectionInfo:
    @pytest.mark.asyncio
    async def test_dim_match_true(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1024, points_count=100)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        info = await pgvector_store.collection_info()
        assert info is not None
        assert info["exists"] is True
        assert info["dim_actual"] == 1024
        assert info["dim_expected"] == 1024
        assert info["dim_match"] is True
        assert info["status"] == "green"
        assert info["points_count"] == 100
        assert info["backend"] == "pgvector"

    @pytest.mark.asyncio
    async def test_dim_match_false_on_drift(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=1536, points_count=43)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        info = await pgvector_store.collection_info()
        assert info["dim_actual"] == 1536
        assert info["dim_expected"] == 1024
        assert info["dim_match"] is False
        assert info["status"] == "drift"
        # collection_info NÃO drop/altera nada
        assert con.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_column(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = _mock_con(current_dim=None)
        pool = _make_pool_with_con(con)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        info = await pgvector_store.collection_info()
        assert info["exists"] is False
        assert info["status"] == "missing"
        assert info["dim_actual"] is None
        assert info["points_count"] == 0


# ═════════════════════════════════════════════════════════════════
# Roteador de backend
# ═════════════════════════════════════════════════════════════════


class TestBackendRouter:
    def test_ingest_resolves_pgvector_explicit(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("RAG_VECTOR_BACKEND", "pgvector")
        from app.evidence import ingest
        store = ingest._get_vector_store()
        assert store.__name__.endswith("pgvector_store")

    def test_ingest_resolves_pgvector_by_default(self, monkeypatch, fresh_settings):
        """PR E: default mudou de qdrant para pgvector."""
        monkeypatch.delenv("RAG_VECTOR_BACKEND", raising=False)
        from app.evidence import ingest
        store = ingest._get_vector_store()
        assert store.__name__.endswith("pgvector_store")

    def test_ingest_resolves_qdrant_opt_in(self, monkeypatch, fresh_settings):
        """Qdrant continua disponível como opt-in explícito (rollback) até PR F."""
        monkeypatch.setenv("RAG_VECTOR_BACKEND", "qdrant")
        from app.evidence import ingest
        store = ingest._get_vector_store()
        assert store.__name__.endswith("qdrant_store")

    def test_ingest_resolves_pgvector_on_unknown_value(self, monkeypatch, fresh_settings):
        """Valor desconhecido (typo) cai em pgvector — default seguro da nova era."""
        monkeypatch.setenv("RAG_VECTOR_BACKEND", "milvus-faiss-pinecone")
        from app.evidence import ingest
        store = ingest._get_vector_store()
        assert store.__name__.endswith("pgvector_store")

    def test_runtime_resolves_pgvector_by_default(self, monkeypatch, fresh_settings):
        monkeypatch.delenv("RAG_VECTOR_BACKEND", raising=False)
        from app.evidence import runtime
        fn = runtime._get_vector_search_fn()
        assert fn.__module__ == "app.evidence.pgvector_store"

    def test_runtime_resolves_pgvector_explicit(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("RAG_VECTOR_BACKEND", "pgvector")
        from app.evidence import runtime
        fn = runtime._get_vector_search_fn()
        assert fn.__module__ == "app.evidence.pgvector_store"

    def test_runtime_resolves_qdrant_opt_in(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("RAG_VECTOR_BACKEND", "qdrant")
        from app.evidence import runtime
        fn = runtime._get_vector_search_fn()
        assert fn.__module__ == "app.evidence.qdrant_store"
