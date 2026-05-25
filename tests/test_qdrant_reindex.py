"""Testes do reindex de Qdrant + dim dinâmica do embedder ativo.

Cobertura:
- get_active_embedding_dim(): Azure 1536, Qwen3 1024 default, Qwen3 custom.
- _extract_collection_dim(): VectorParams direto vs dict de nomeados.
- ensure_collection(): drift de dim → log + retorna False sem dropar.
- recreate_collection(): drop+create idempotente, falhas com error_type.
- collection_info(): retorna dim_match True/False sem passar por ensure.
- reindex_all(): batches, errors parciais, idempotência.

Mocks: AsyncMock pro AsyncQdrantClient + monkeypatch dos módulos qdrant_client
e embed_texts (porque o pipeline real depende de rede Azure/Qwen3).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import config as _config
from app.evidence import qdrant_store


@pytest.fixture
def fresh_settings(monkeypatch):
    """Limpa lru_cache de get_settings — tests podem patchar Settings fields
    e ainda assim get_settings() retorna instância nova."""
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_qdrant_singletons():
    """Garante que cada teste começa com singletons do qdrant_store limpos."""
    qdrant_store._client = None
    qdrant_store._collection_ready = False
    yield
    qdrant_store._client = None
    qdrant_store._collection_ready = False


# ═════════════════════════════════════════════════════════════════
# get_active_embedding_dim
# ═════════════════════════════════════════════════════════════════


class TestGetActiveEmbeddingDim:
    def test_azure_provider_returns_1536(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "azure")
        assert qdrant_store.get_active_embedding_dim() == 1536

    def test_qwen3_provider_default_returns_1024(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # 0 = default modelo
        assert qdrant_store.get_active_embedding_dim() == 1024

    def test_qwen3_provider_custom_dimensions(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "768")
        assert qdrant_store.get_active_embedding_dim() == 768

    def test_unknown_provider_falls_back_to_1536(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "provider-desconhecido")
        assert qdrant_store.get_active_embedding_dim() == 1536

    def test_case_insensitive_provider(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "QWEN3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        assert qdrant_store.get_active_embedding_dim() == 1024


# ═════════════════════════════════════════════════════════════════
# _extract_collection_dim
# ═════════════════════════════════════════════════════════════════


class TestExtractCollectionDim:
    def test_shape_unnamed_vector_params(self):
        # Shape 1: VectorParams direto (collection com 1 vetor único sem nome)
        info = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=1024, distance="Cosine"),
                )
            )
        )
        assert qdrant_store._extract_collection_dim(info) == 1024

    def test_shape_named_vectors_dict(self):
        # Shape 2: dict[str, VectorParams] (vetores nomeados)
        info = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={
                        "text": SimpleNamespace(size=1536, distance="Cosine"),
                    }
                )
            )
        )
        assert qdrant_store._extract_collection_dim(info) == 1536

    def test_invalid_shape_returns_none(self):
        info = SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=None)))
        assert qdrant_store._extract_collection_dim(info) is None

    def test_missing_attribute_returns_none(self):
        info = SimpleNamespace()  # sem config sequer
        assert qdrant_store._extract_collection_dim(info) is None


# ═════════════════════════════════════════════════════════════════
# ensure_collection — dim mismatch detection
# ═════════════════════════════════════════════════════════════════


def _mock_client_with_collection(collection_name: str, existing_dim: int | None):
    """Cria AsyncMock de AsyncQdrantClient simulando uma collection com `existing_dim`.

    Se existing_dim is None, simula collection inexistente (get_collections retorna []).
    """
    client = MagicMock()
    # get_collections é async
    if existing_dim is None:
        client.get_collections = AsyncMock(
            return_value=SimpleNamespace(collections=[])
        )
    else:
        client.get_collections = AsyncMock(
            return_value=SimpleNamespace(
                collections=[SimpleNamespace(name=collection_name)]
            )
        )
    # get_collection devolve info com size
    client.get_collection = AsyncMock(
        return_value=SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=existing_dim, distance="Cosine"),
                )
            ),
            points_count=42,
            status="green",
        )
    )
    client.create_collection = AsyncMock(return_value=None)
    client.delete_collection = AsyncMock(return_value=None)
    return client


class TestEnsureCollectionDimMismatch:
    @pytest.mark.asyncio
    async def test_dim_mismatch_returns_false_does_not_drop(self, monkeypatch, fresh_settings, caplog):
        """Trocou Azure (1536) → Qwen3 (1024) sem reindexar: collection antiga
        sobrevive, ensure retorna False, log de dim_mismatch é emitido."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # → 1024 esperado
        monkeypatch.setenv("QDRANT_COLLECTION", "agente_evidence")
        client = _mock_client_with_collection("agente_evidence", existing_dim=1536)
        qdrant_store._client = client  # bypass get_client()

        import logging
        with caplog.at_level(logging.ERROR, logger="app.evidence.qdrant_store"):
            ok = await qdrant_store.ensure_collection()
        assert ok is False
        # Nenhuma operação destrutiva
        client.delete_collection.assert_not_called()
        client.create_collection.assert_not_called()
        # Log estruturado emitido
        assert any("dim_mismatch" in r.message or
                   getattr(r, "event", "") == "qdrant.collection.dim_mismatch"
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_dim_match_returns_true(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # → 1024
        monkeypatch.setenv("QDRANT_COLLECTION", "agente_evidence")
        client = _mock_client_with_collection("agente_evidence", existing_dim=1024)
        qdrant_store._client = client

        ok = await qdrant_store.ensure_collection()
        assert ok is True
        assert qdrant_store._collection_ready is True

    @pytest.mark.asyncio
    async def test_collection_missing_is_created_with_expected_dim(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")  # → 1024
        monkeypatch.setenv("QDRANT_COLLECTION", "agente_evidence")
        client = _mock_client_with_collection("agente_evidence", existing_dim=None)
        qdrant_store._client = client

        ok = await qdrant_store.ensure_collection()
        assert ok is True
        # Chamou create com size correto (vetor único unnamed)
        client.create_collection.assert_awaited_once()
        kwargs = client.create_collection.await_args.kwargs
        assert kwargs["collection_name"] == "agente_evidence"
        # vectors_config é um VectorParams — pega o size
        assert kwargs["vectors_config"].size == 1024


# ═════════════════════════════════════════════════════════════════
# recreate_collection
# ═════════════════════════════════════════════════════════════════


class TestRecreateCollection:
    @pytest.mark.asyncio
    async def test_existing_collection_is_dropped_then_created(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        monkeypatch.setenv("QDRANT_COLLECTION", "agente_evidence")
        client = _mock_client_with_collection("agente_evidence", existing_dim=1536)
        qdrant_store._client = client

        res = await qdrant_store.recreate_collection()
        assert res["ok"] is True
        assert res["dim_before"] == 1536
        assert res["dim_after"] == 1024
        assert res["points_deleted"] == 42
        client.delete_collection.assert_awaited_once_with(collection_name="agente_evidence")
        client.create_collection.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nonexistent_collection_only_creates(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        monkeypatch.setenv("QDRANT_COLLECTION", "agente_evidence")
        client = _mock_client_with_collection("agente_evidence", existing_dim=None)
        qdrant_store._client = client

        res = await qdrant_store.recreate_collection()
        assert res["ok"] is True
        assert res["dim_before"] is None
        assert res["dim_after"] == 1024
        client.delete_collection.assert_not_called()
        client.create_collection.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_failure_returns_error_with_type(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        client = _mock_client_with_collection("agente_evidence", existing_dim=None)

        class FakeError(Exception):
            pass

        client.create_collection = AsyncMock(side_effect=FakeError("disk full"))
        qdrant_store._client = client

        res = await qdrant_store.recreate_collection()
        assert res["ok"] is False
        assert res["error_type"] == "FakeError"
        assert "disk full" in res["error_message"]

    @pytest.mark.asyncio
    async def test_invalidates_collection_ready_cache(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        client = _mock_client_with_collection("agente_evidence", existing_dim=1024)
        qdrant_store._client = client
        qdrant_store._collection_ready = True  # finge que tava cacheado

        await qdrant_store.recreate_collection()
        # Após recreate, próximo upsert/search deve revalidar
        assert qdrant_store._collection_ready is False


# ═════════════════════════════════════════════════════════════════
# collection_info — diagnóstico sem passar por ensure_collection
# ═════════════════════════════════════════════════════════════════


class TestCollectionInfo:
    @pytest.mark.asyncio
    async def test_dim_match_true_when_provider_matches(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        monkeypatch.setenv("QDRANT_COLLECTION", "agente_evidence")
        client = _mock_client_with_collection("agente_evidence", existing_dim=1024)
        qdrant_store._client = client

        info = await qdrant_store.collection_info()
        assert info is not None
        assert info["dim_actual"] == 1024
        assert info["dim_expected"] == 1024
        assert info["dim_match"] is True
        assert info["exists"] is True

    @pytest.mark.asyncio
    async def test_dim_match_false_on_drift(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        client = _mock_client_with_collection("agente_evidence", existing_dim=1536)
        qdrant_store._client = client

        info = await qdrant_store.collection_info()
        assert info["dim_actual"] == 1536
        assert info["dim_expected"] == 1024
        assert info["dim_match"] is False
        # Importante: collection_info NÃO deve disparar drop/create
        client.delete_collection.assert_not_called()
        client.create_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_collection_reports_exists_false(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        client = _mock_client_with_collection("agente_evidence", existing_dim=None)
        qdrant_store._client = client

        info = await qdrant_store.collection_info()
        assert info["exists"] is False
        assert info["status"] == "missing"
        assert info["dim_actual"] is None

    @pytest.mark.asyncio
    async def test_returns_none_when_client_unavailable(self, monkeypatch, fresh_settings):
        # _client é None, get_client tenta importar qdrant_client e falha
        qdrant_store._client = None

        async def _fake_get_client():
            return None
        monkeypatch.setattr(qdrant_store, "get_client", _fake_get_client)

        info = await qdrant_store.collection_info()
        assert info is None


# ═════════════════════════════════════════════════════════════════
# reindex_all
# ═════════════════════════════════════════════════════════════════


class TestReindexAll:
    @pytest.mark.asyncio
    async def test_empty_postgres_returns_ok_no_op(self, monkeypatch, fresh_settings):
        """Sem chunks no Postgres: ok=True, nada upsertado."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import ingest as ingest_mod

        # Recreate é chamado (recreate_collection=True default)
        async def _fake_recreate():
            return {"ok": True, "dim_before": 1536, "dim_after": 1024, "collection": "agente_evidence",
                    "distance": "Cosine", "points_deleted": 0}
        monkeypatch.setattr(qdrant_store, "recreate_collection", _fake_recreate)

        # Postgres pool mock: fetch retorna []
        fake_con = MagicMock()
        fake_con.fetch = AsyncMock(return_value=[])

        class _PoolCtx:
            async def __aenter__(self_): return fake_con
            async def __aexit__(self_, *a): return False

        fake_pool = MagicMock()
        fake_pool.acquire = MagicMock(return_value=_PoolCtx())
        monkeypatch.setattr(ingest_mod, "_get_pool", lambda: fake_pool)

        res = await ingest_mod.reindex_all()
        assert res["ok"] is True
        assert res["recreated"] is True
        assert res["chunks_total"] == 0
        assert res["chunks_upserted"] == 0
        assert res["dim_after"] == 1024
        assert res["errors"] == []

    @pytest.mark.asyncio
    async def test_happy_path_batches_all_chunks(self, monkeypatch, fresh_settings):
        """3 chunks, batch_size=2 → 2 batches. Embed + upsert OK em todos."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import ingest as ingest_mod

        async def _fake_recreate():
            return {"ok": True, "dim_before": None, "dim_after": 1024, "collection": "agente_evidence",
                    "distance": "Cosine", "points_deleted": None}
        monkeypatch.setattr(qdrant_store, "recreate_collection", _fake_recreate)

        # 3 rows simulando chunks
        rows = [
            {"id": "c1", "knowledge_source_id": "s1", "ordinal": 0, "text": "alpha"},
            {"id": "c2", "knowledge_source_id": "s1", "ordinal": 1, "text": "beta"},
            {"id": "c3", "knowledge_source_id": "s2", "ordinal": 0, "text": "gamma"},
        ]
        fake_con = MagicMock()
        fake_con.fetch = AsyncMock(return_value=rows)

        class _PoolCtx:
            async def __aenter__(self_): return fake_con
            async def __aexit__(self_, *a): return False

        fake_pool = MagicMock()
        fake_pool.acquire = MagicMock(return_value=_PoolCtx())
        monkeypatch.setattr(ingest_mod, "_get_pool", lambda: fake_pool)

        # Embed: retorna 1 vetor por texto
        async def _fake_embed(texts):
            return [[0.1] * 1024 for _ in texts]
        monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)

        # Qdrant upsert: aceita todos
        async def _fake_upsert(payload):
            return len(payload)
        monkeypatch.setattr(qdrant_store, "upsert_chunks", _fake_upsert)

        res = await ingest_mod.reindex_all(batch_size=2)
        assert res["ok"] is True
        assert res["chunks_total"] == 3
        assert res["chunks_embedded"] == 3
        assert res["chunks_upserted"] == 3
        assert res["sources_count"] == 2
        assert res["batches"] == 2  # 2 chunks + 1 chunk
        assert res["errors"] == []

    @pytest.mark.asyncio
    async def test_embed_failure_in_batch_continues_other_batches(self, monkeypatch, fresh_settings):
        """1 batch falha no embed; o outro segue. ok=False mas chunks_upserted>0."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import ingest as ingest_mod

        async def _fake_recreate():
            return {"ok": True, "dim_before": None, "dim_after": 1024, "collection": "agente_evidence",
                    "distance": "Cosine", "points_deleted": None}
        monkeypatch.setattr(qdrant_store, "recreate_collection", _fake_recreate)

        rows = [
            {"id": "c1", "knowledge_source_id": "s1", "ordinal": 0, "text": "alpha"},
            {"id": "c2", "knowledge_source_id": "s1", "ordinal": 1, "text": "beta"},
        ]
        fake_con = MagicMock()
        fake_con.fetch = AsyncMock(return_value=rows)

        class _PoolCtx:
            async def __aenter__(self_): return fake_con
            async def __aexit__(self_, *a): return False

        fake_pool = MagicMock()
        fake_pool.acquire = MagicMock(return_value=_PoolCtx())
        monkeypatch.setattr(ingest_mod, "_get_pool", lambda: fake_pool)

        # Embed: 1º batch falha (None), 2º OK
        call_count = {"n": 0}
        async def _fake_embed(texts):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # falha
            return [[0.1] * 1024 for _ in texts]
        monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)

        async def _fake_upsert(payload):
            return len(payload)
        monkeypatch.setattr(qdrant_store, "upsert_chunks", _fake_upsert)

        res = await ingest_mod.reindex_all(batch_size=1)
        assert res["chunks_total"] == 2
        assert res["chunks_embedded"] == 1  # só o 2º batch
        assert res["chunks_upserted"] == 1
        assert res["ok"] is False
        assert len(res["errors"]) == 1
        assert res["errors"][0]["stage"] == "embed"

    @pytest.mark.asyncio
    async def test_recreate_failure_aborts_reindex(self, monkeypatch, fresh_settings):
        """recreate_collection=True falha → reindex aborta sem mexer no Postgres."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import ingest as ingest_mod

        async def _fake_recreate():
            return {"ok": False, "error_type": "ConnectError", "error_message": "qdrant down"}
        monkeypatch.setattr(qdrant_store, "recreate_collection", _fake_recreate)

        res = await ingest_mod.reindex_all(recreate_collection=True)
        assert res["ok"] is False
        assert res["recreated"] is False  # tentou, mas não rolou
        assert res["chunks_total"] == 0  # nem chegou a query Postgres
        assert len(res["errors"]) == 1
        assert res["errors"][0]["stage"] == "recreate_collection"
        assert res["errors"][0]["error_type"] == "ConnectError"

    @pytest.mark.asyncio
    async def test_no_recreate_skips_drop(self, monkeypatch, fresh_settings):
        """recreate_collection=False: pula recreate, popula collection existente."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import ingest as ingest_mod

        recreate_called = {"n": 0}
        async def _fake_recreate():
            recreate_called["n"] += 1
            return {"ok": True}
        monkeypatch.setattr(qdrant_store, "recreate_collection", _fake_recreate)

        fake_con = MagicMock()
        fake_con.fetch = AsyncMock(return_value=[])
        class _PoolCtx:
            async def __aenter__(self_): return fake_con
            async def __aexit__(self_, *a): return False
        fake_pool = MagicMock()
        fake_pool.acquire = MagicMock(return_value=_PoolCtx())
        monkeypatch.setattr(ingest_mod, "_get_pool", lambda: fake_pool)

        res = await ingest_mod.reindex_all(recreate_collection=False)
        assert recreate_called["n"] == 0
        assert res["recreated"] is False
        assert res["ok"] is True  # 0 chunks também conta como ok
