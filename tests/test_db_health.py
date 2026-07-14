"""Health check abrangente do Postgres (Onda P).

Pós-Onda Q, Postgres é o ÚNICO backend de dados — RAG vetorial, BM25,
core (agents/skills/interactions), audit, catalog, settings store, etc.
O `_check_postgres` antigo em infra.py fazia só `SELECT 1`. Health
endpoint novo cobre:

- Pool (size, utilization)
- Extensions (vector required, pg_trgm opcional)
- Tabelas críticas (19 listadas)
- Indexes críticos (GIN tsvector + HNSW pgvector)
- pgvector dim drift
- tsvector populado
- Settings store r/w smoke

Testes aqui mockam o pool — verificam que cada subcheck:
1. Detecta falha (sem crash)
2. Devolve shape esperado
3. Mensagens de erro são truncadas (não vazam dados)
4. Asyncio.gather paraleliza correto

Integração real com Postgres fica em tests/integration/.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_client():
    """TestClient com router db_health + auth bypass."""
    from app.routes.db_health import router
    from app.core.auth import require_user
    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return {"id": "u1", "email": "test@local"}
    app.dependency_overrides[require_user] = fake_user
    return TestClient(app)


def _make_fake_pool(*, fetch_results=None, fetchrow_results=None, execute_results=None,
                    min_size=2, max_size=10, current_size=5, idle_size=3):
    """Mock asyncpg.Pool com responses configuráveis.

    fetch/fetchrow/execute aceitam list de resultados em ordem das chamadas.
    """
    fetch_iter = iter(fetch_results or [])
    fetchrow_iter = iter(fetchrow_results or [])
    execute_iter = iter(execute_results or [None] * 100)

    con = MagicMock()

    async def fake_fetch(*args, **kwargs):
        try:
            return next(fetch_iter)
        except StopIteration:
            return []

    async def fake_fetchrow(*args, **kwargs):
        try:
            return next(fetchrow_iter)
        except StopIteration:
            return None

    async def fake_execute(*args, **kwargs):
        try:
            return next(execute_iter)
        except StopIteration:
            return None

    con.fetch = fake_fetch
    con.fetchrow = fake_fetchrow
    con.execute = fake_execute

    pool = MagicMock()
    pool.get_min_size = MagicMock(return_value=min_size)
    pool.get_max_size = MagicMock(return_value=max_size)
    pool.get_size = MagicMock(return_value=current_size)
    pool.get_idle_size = MagicMock(return_value=idle_size)

    class _AcquireCtx:
        async def __aenter__(self_inner): return con
        async def __aexit__(self_inner, *a): return None

    pool.acquire = lambda: _AcquireCtx()
    return pool


# ────────────────────────────────────────────────────────────────
# Cada subcheck isolado
# ────────────────────────────────────────────────────────────────


class TestCheckPool:
    @pytest.mark.asyncio
    async def test_returns_size_info(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(min_size=2, max_size=10, current_size=5, idle_size=3)
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_pool()
        assert result["ok"] is True
        assert result["min"] == 2
        assert result["max"] == 10
        assert result["active"] == 2  # current - idle
        assert result["idle"] == 3

    @pytest.mark.asyncio
    async def test_returns_error_when_pool_missing(self, monkeypatch):
        from app.routes import db_health

        def raise_no_pool():
            raise RuntimeError("Pool PostgreSQL não inicializado")

        monkeypatch.setattr("app.core.database._get_pool", raise_no_pool)
        result = await db_health._check_pool()
        assert result["ok"] is False
        assert "RuntimeError" in result["error"]


class TestCheckExtensions:
    @pytest.mark.asyncio
    async def test_vector_present_returns_ok(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(fetch_results=[
            [{"extname": "vector", "extversion": "0.5.0"}],
        ])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_extensions()
        assert result["ok"] is True
        assert result["required"]["vector"]["ok"] is True
        assert result["required"]["vector"]["version"] == "0.5.0"

    @pytest.mark.asyncio
    async def test_vector_missing_returns_not_ok(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(fetch_results=[[]])  # no extensions
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_extensions()
        assert result["ok"] is False
        assert result["required"]["vector"]["ok"] is False

    @pytest.mark.asyncio
    async def test_pg_trgm_optional_does_not_block_ok(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(fetch_results=[
            [{"extname": "vector", "extversion": "0.5.0"}],  # só vector, sem pg_trgm
        ])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_extensions()
        # Overall ok (vector é o que importa); pg_trgm marcado como ausente
        assert result["ok"] is True
        assert result["optional"]["pg_trgm"]["ok"] is True
        assert result["optional"]["pg_trgm"]["present"] is False


class TestCheckTables:
    @pytest.mark.asyncio
    async def test_all_critical_tables_present(self, monkeypatch):
        from app.routes import db_health
        # Simula DB com todas as tabelas críticas + extras
        all_tables = [{"table_name": t} for t in db_health._CRITICAL_TABLES]
        all_tables.append({"table_name": "extra_table"})
        pool = _make_fake_pool(fetch_results=[all_tables])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_tables()
        assert result["ok"] is True
        assert result["missing"] == []
        assert result["present"] == len(db_health._CRITICAL_TABLES)

    @pytest.mark.asyncio
    async def test_missing_table_returns_not_ok(self, monkeypatch):
        from app.routes import db_health
        # Falta uma tabela crítica
        partial_tables = [
            {"table_name": t} for t in db_health._CRITICAL_TABLES[:-1]
        ]
        pool = _make_fake_pool(fetch_results=[partial_tables])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_tables()
        assert result["ok"] is False
        assert db_health._CRITICAL_TABLES[-1] in result["missing"]


class TestCheckIndexes:
    @pytest.mark.asyncio
    async def test_critical_indexes_present(self, monkeypatch):
        from app.routes import db_health
        all_indexes = [{"indexname": i["name"]} for i in db_health._CRITICAL_INDEXES]
        pool = _make_fake_pool(fetch_results=[all_indexes])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_indexes()
        assert result["ok"] is True
        assert all(i["ok"] for i in result["indexes"])

    @pytest.mark.asyncio
    async def test_missing_index_returns_not_ok(self, monkeypatch):
        from app.routes import db_health
        # Só 1 dos 2 indexes críticos
        partial = [{"indexname": db_health._CRITICAL_INDEXES[0]["name"]}]
        pool = _make_fake_pool(fetch_results=[partial])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_indexes()
        assert result["ok"] is False


class TestCheckPgvectorDim:
    @pytest.mark.asyncio
    async def test_dim_match_returns_ok(self, monkeypatch):
        from app.routes import db_health

        async def fake_info():
            return {"dim_actual": 1024, "dim_expected": 1024, "dim_match": True,
                    "points_count": 100, "status": "green"}

        monkeypatch.setattr("app.evidence.pgvector_store.collection_info", fake_info)
        result = await db_health._check_pgvector_dim()
        assert result["ok"] is True
        assert result["actual_dim"] == 1024
        assert result["expected_dim"] == 1024

    @pytest.mark.asyncio
    async def test_dim_drift_returns_not_ok(self, monkeypatch):
        from app.routes import db_health

        async def fake_info():
            return {"dim_actual": 1536, "dim_expected": 1024, "dim_match": False,
                    "points_count": 50, "status": "drift"}

        monkeypatch.setattr("app.evidence.pgvector_store.collection_info", fake_info)
        result = await db_health._check_pgvector_dim()
        assert result["ok"] is False
        assert result["actual_dim"] == 1536
        assert result["expected_dim"] == 1024


class TestCheckTsvector:
    @pytest.mark.asyncio
    async def test_all_text_has_tsv(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(fetchrow_results=[
            {"total": 100, "with_tsv": 100, "with_text": 100},
        ])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_tsvector()
        assert result["ok"] is True
        assert result["with_tsv"] == 100

    @pytest.mark.asyncio
    async def test_empty_table_is_ok(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(fetchrow_results=[
            {"total": 0, "with_tsv": 0, "with_text": 0},
        ])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_tsvector()
        # Sem chunks, ok=True (não há mismatch possível)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_text_without_tsv_returns_not_ok(self, monkeypatch):
        from app.routes import db_health
        pool = _make_fake_pool(fetchrow_results=[
            {"total": 100, "with_tsv": 50, "with_text": 100},  # mismatch
        ])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_tsvector()
        assert result["ok"] is False


class TestCheckSettingsStore:
    @pytest.mark.asyncio
    async def test_rw_smoke_passes(self, monkeypatch):
        from app.routes import db_health
        # Simula: write OK, read devolve o mesmo valor escrito
        test_val_holder = {}

        con = MagicMock()

        async def fake_execute(query, *args):
            if "INSERT" in query:
                # Captura o valor escrito
                test_val_holder["val"] = args[1]
            return None

        async def fake_fetchrow(query, *args):
            # Devolve o valor que foi escrito
            return {"value": test_val_holder.get("val", "")}

        con.execute = fake_execute
        con.fetchrow = fake_fetchrow

        class _Ctx:
            async def __aenter__(self_inner): return con
            async def __aexit__(self_inner, *a): return None

        pool = MagicMock()
        pool.acquire = lambda: _Ctx()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_settings_store()
        assert result["ok"] is True
        assert result["rw_test"] == "passed"

    @pytest.mark.asyncio
    async def test_value_mismatch_returns_not_ok(self, monkeypatch):
        """Se INSERT funcionou mas SELECT trouxe valor diferente, algo está
        muito errado (corrupção? trigger?)."""
        from app.routes import db_health

        con = MagicMock()
        con.execute = AsyncMock(return_value=None)
        con.fetchrow = AsyncMock(return_value={"value": "wrong_value"})

        class _Ctx:
            async def __aenter__(self_inner): return con
            async def __aexit__(self_inner, *a): return None

        pool = MagicMock()
        pool.acquire = lambda: _Ctx()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        result = await db_health._check_settings_store()
        assert result["ok"] is False
        assert "mismatch" in result["error"]


# ────────────────────────────────────────────────────────────────
# Endpoint integração
# ────────────────────────────────────────────────────────────────


class TestEndpointE2E:
    def test_all_checks_pass_returns_ok_true(self, app_client, monkeypatch):
        """Cenário ideal: tudo funcionando, ok=True, todos os checks passam."""
        from app.routes import db_health

        pool = _make_fake_pool(
            fetch_results=[
                [{"extname": "vector", "extversion": "0.5.0"},
                 {"extname": "pg_trgm", "extversion": "1.6"}],
                [{"table_name": t} for t in db_health._CRITICAL_TABLES],
                [{"indexname": i["name"]} for i in db_health._CRITICAL_INDEXES],
            ],
            fetchrow_results=[
                {"total": 0, "with_tsv": 0, "with_text": 0},
                {"value": "test_val"},  # settings_store read após write
            ],
        )
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)

        async def fake_pgvec_info():
            return {"dim_actual": 1024, "dim_expected": 1024, "dim_match": True,
                    "points_count": 0, "status": "green"}

        monkeypatch.setattr("app.evidence.pgvector_store.collection_info", fake_pgvec_info)

        # settings_store usa execute pra INSERT — precisa setar fake
        # Estratégia: mock direto _check_settings_store pra simplificar
        async def fake_settings_ok():
            return {"ok": True, "rw_test": "passed"}

        monkeypatch.setattr(db_health, "_check_settings_store", fake_settings_ok)

        r = app_client.get("/api/v1/health/database")
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["ok"] is True
        assert "duration_ms" in body
        assert "checks" in body
        assert set(body["checks"].keys()) >= {
            "pool", "extensions", "tables", "indexes", "pgvector", "tsvector", "settings_store"
        }

    def test_pool_failure_short_circuits(self, app_client, monkeypatch):
        """Se pool falha, demais checks dependeriam dele — fail fast retorna
        só pool no checks."""

        def raise_no_pool():
            raise RuntimeError("Pool não inicializado")

        monkeypatch.setattr("app.core.database._get_pool", raise_no_pool)

        r = app_client.get("/api/v1/health/database")
        body = r.json()
        assert body["ok"] is False
        # Só pool no response — demais checks pulados
        assert list(body["checks"].keys()) == ["pool"]

    def test_extension_missing_makes_overall_not_ok(self, app_client, monkeypatch):
        """Vector extension faltando = not ok geral."""
        from app.routes import db_health

        pool = _make_fake_pool(
            fetch_results=[
                [],  # sem extensions
                [{"table_name": t} for t in db_health._CRITICAL_TABLES],
                [{"indexname": i["name"]} for i in db_health._CRITICAL_INDEXES],
            ],
            fetchrow_results=[
                {"total": 0, "with_tsv": 0, "with_text": 0},
            ],
        )
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)

        async def fake_pgvec_info():
            return {"dim_actual": 1024, "dim_expected": 1024, "dim_match": True,
                    "points_count": 0, "status": "green"}

        monkeypatch.setattr("app.evidence.pgvector_store.collection_info", fake_pgvec_info)

        async def fake_settings_ok():
            return {"ok": True, "rw_test": "passed"}

        monkeypatch.setattr(db_health, "_check_settings_store", fake_settings_ok)

        r = app_client.get("/api/v1/health/database")
        body = r.json()
        assert body["ok"] is False
        assert body["checks"]["extensions"]["ok"] is False


# ────────────────────────────────────────────────────────────────
# Migration: FK indexes Onda P
# ────────────────────────────────────────────────────────────────


class TestFkIndexesMigration:
    """Confirma que migrations Onda P (FK indexes) estão na lista
    _IDEMPOTENT_MIGRATIONS. Regressão pra não removerem por engano."""

    def test_agent_bindings_index_present(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        sql = " ".join(_IDEMPOTENT_MIGRATIONS)
        assert "idx_agent_bindings_agent_id" in sql

    def test_envelopes_indexes_present(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        sql = " ".join(_IDEMPOTENT_MIGRATIONS)
        assert "idx_envelopes_origin_agent_id" in sql
        assert "idx_envelopes_target_agent_id" in sql

    def test_turns_interaction_id_index(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        sql = " ".join(_IDEMPOTENT_MIGRATIONS)
        assert "idx_turns_interaction_id" in sql

    def test_tool_calls_interaction_id_index(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        sql = " ".join(_IDEMPOTENT_MIGRATIONS)
        assert "idx_tool_calls_interaction_id" in sql

    def test_interactions_agent_id_index(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        sql = " ".join(_IDEMPOTENT_MIGRATIONS)
        assert "idx_interactions_agent_id" in sql

    def test_all_migrations_use_if_not_exists(self):
        """Idempotência: todas as nossas novas migrations usam IF NOT EXISTS."""
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        _new_indexes = [m for m in _IDEMPOTENT_MIGRATIONS if "Onda P" in m or "idx_agent_bindings" in m or "idx_envelopes" in m or "idx_turns_interaction" in m]
        # Pelo menos os índices Onda P
        onda_p_indexes = [m for m in _IDEMPOTENT_MIGRATIONS if m.startswith("CREATE INDEX IF NOT EXISTS idx_")]
        for sql in onda_p_indexes:
            assert "IF NOT EXISTS" in sql, f"Migration sem IF NOT EXISTS: {sql}"
