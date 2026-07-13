"""Backend pgvector para RAG vetorial (único backend desde Onda Q).

Histórico: existia também app.evidence.qdrant_store, removido em
Onda Q (2026-05-30). pgvector cobre 100% dos casos sem 2º serviço
(Postgres já está na infra). Caller (ingest.py, runtime.py) chama
direto (sem roteador condicional).

Modelagem:
- Coluna `embedding vector(N)` adicionada dinamicamente em evidence_chunks
  (N = get_active_embedding_dim, ex: 1024 Qwen3, 1536 Azure).
- Index HNSW (cosine) construído na criação da coluna.
- Texto + tsvector + metadata seguem em evidence_chunks (já existem).

Vantagens vs Qdrant:
- Transação atômica chunk+vetor (mesmo INSERT). Impossível ficar divergente.
- Filtro por knowledge_source_id usa B-tree index existente (sem FieldCondition).
- 1 backup só (pg_dump cobre tudo). 1 sistema a menos rodando.
- OTEL via opentelemetry-instrumentation-asyncpg ganha spans grátis.

Trade-offs:
- HNSW do pgvector é mais lento de construir que Qdrant (~minutos/1M vs segundos).
  Para <1M chunks a diferença é insignificante.
- Trocar dim do embedder = recreate_embedding_column (destrutivo). Endpoint
  /api/v1/evidence/reindex faz o ciclo completo (drop+create+repopulate).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.config import get_settings
from app.core.database import _get_pool
# Onda Q (2026-05-30): get_active_embedding_dim migrou de qdrant_store
# pra embedder.py (lugar backend-neutral). Antes era importado do
# qdrant — refactor histórico evitando duplicação.
from app.evidence.embedder import get_active_embedding_dim

logger = logging.getLogger(__name__)

EMBEDDING_COLUMN = "embedding"
EMBEDDING_INDEX = "idx_evidence_chunks_embedding"


# ─── Coluna + index ──────────────────────────────────────────────


async def _column_dim() -> Optional[int]:
    """Inspeciona pg_catalog pra ler a dimensão atual da coluna `embedding`.

    Retorna None se coluna não existe (caso normal antes do primeiro reindex).
    Retorna int se existe — usado pra detectar drift vs get_active_embedding_dim().
    """
    pool = _get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            SELECT atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE c.relname = 'evidence_chunks'
              AND a.attname = $1
              AND t.typname = 'vector'
              AND a.attnum > 0
              AND NOT a.attisdropped
            """,
            EMBEDDING_COLUMN,
        )
    if not row:
        return None
    # pgvector codifica dim em atttypmod (sem o overhead do -1)
    typmod = row["atttypmod"]
    if typmod is None or typmod < 1:
        return None
    return int(typmod)


async def ensure_embedding_column() -> bool:
    """Idempotente: garante que a coluna embedding existe com a dim ATUAL do
    provider de embedding ativo. Retorna True se pronto para upsert/search.

    Cenários:
    - Coluna não existe → cria com dim correta + index HNSW.
    - Coluna existe com dim correta → no-op, retorna True.
    - Coluna existe com dim DIFERENTE → loga ERROR, retorna False. Caller
      (upsert/search) cai em fallback. Operador chama POST /evidence/reindex
      para drop+recreate destrutivo.

    Importante: NÃO faz drop automático em drift. Operação destrutiva tem
    que ser explícita (mesma política do qdrant_store).
    """
    expected_dim = get_active_embedding_dim()
    actual_dim = await _column_dim()

    if actual_dim is None:
        # Coluna não existe → cria do zero.
        try:
            pool = _get_pool()
            async with pool.acquire() as con:
                # CREATE EXTENSION é redundante (migration já fez) mas inofensivo
                await con.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await con.execute(
                    f"ALTER TABLE evidence_chunks "
                    f"ADD COLUMN IF NOT EXISTS {EMBEDDING_COLUMN} vector({expected_dim})"
                )
                # HNSW: melhor recall que IVFFlat, build constante.
                # Tunables m e ef_construction: defaults (m=16, ef=64) são bons
                # pra <10M vetores. Podem virar settings se virar gargalo.
                await con.execute(
                    f"CREATE INDEX IF NOT EXISTS {EMBEDDING_INDEX} "
                    f"ON evidence_chunks USING hnsw ({EMBEDDING_COLUMN} vector_cosine_ops)"
                )
            logger.info(
                "pgvector: coluna embedding criada",
                extra={
                    "event": "pgvector.column.created",
                    "column": EMBEDDING_COLUMN,
                    "embedding_dim": expected_dim,
                    "index": EMBEDDING_INDEX,
                    "index_type": "hnsw",
                    "distance": "cosine",
                },
            )
            return True
        except Exception as e:
            logger.error(
                "pgvector: falha ao criar coluna embedding",
                extra={
                    "event": "pgvector.column.create_failed",
                    "embedding_dim_expected": expected_dim,
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            return False

    if actual_dim != expected_dim:
        logger.error(
            "pgvector: coluna embedding com dimensão divergente do embedder ativo — "
            "chame POST /api/v1/evidence/reindex para recriar com a dim correta",
            extra={
                "event": "pgvector.column.dim_mismatch",
                "column": EMBEDDING_COLUMN,
                "dim_actual": actual_dim,
                "dim_expected": expected_dim,
                "embedding_provider": (get_settings().embedding_provider or "azure"),
                "hint": "POST /api/v1/evidence/reindex {\"recreate_collection\": true}",
            },
        )
        return False

    return True


async def recreate_embedding_column() -> dict:
    """Drop + recreate da coluna embedding (e seu index) com a dim ATUAL do
    embedder ativo. Operação DESTRUTIVA — todos os vetores são apagados.

    Equivalente a qdrant_store.recreate_collection(). Caller (reindex_all)
    chama e depois re-embarca a partir do Postgres.

    Returns:
        {
          "ok": bool,
          "column": "embedding",
          "dim_before": int | None,    # None se coluna não existia
          "dim_after": int,
          "distance": "cosine",
          "points_deleted": int | None,  # qtos chunks tinham embedding antes (None se desconhecido)
        }
        ou {"ok": False, "error_type": str, "error_message": str}.
    """
    expected_dim = get_active_embedding_dim()
    dim_before: Optional[int] = None
    points_before: Optional[int] = None
    try:
        # Pré-check: dim atual + contagem de pontos.
        dim_before = await _column_dim()
        if dim_before is not None:
            pool = _get_pool()
            async with pool.acquire() as con:
                points_before = await con.fetchval(
                    f"SELECT COUNT(*) FROM evidence_chunks WHERE {EMBEDDING_COLUMN} IS NOT NULL"
                )

        pool = _get_pool()
        async with pool.acquire() as con:
            # Drop index + coluna se existir. Ambos IF EXISTS — idempotente.
            await con.execute(f"DROP INDEX IF EXISTS {EMBEDDING_INDEX}")
            await con.execute(
                f"ALTER TABLE evidence_chunks DROP COLUMN IF EXISTS {EMBEDDING_COLUMN}"
            )
            # Recria
            await con.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await con.execute(
                f"ALTER TABLE evidence_chunks ADD COLUMN {EMBEDDING_COLUMN} vector({expected_dim})"
            )
            await con.execute(
                f"CREATE INDEX {EMBEDDING_INDEX} "
                f"ON evidence_chunks USING hnsw ({EMBEDDING_COLUMN} vector_cosine_ops)"
            )

        logger.info(
            "pgvector: coluna embedding recriada",
            extra={
                "event": "pgvector.column.recreated",
                "dim_before": dim_before,
                "dim_after": expected_dim,
                "points_deleted": points_before,
            },
        )
        return {
            "ok": True,
            "column": EMBEDDING_COLUMN,
            "dim_before": dim_before,
            "dim_after": expected_dim,
            "distance": "cosine",
            "points_deleted": points_before,
        }
    except Exception as e:
        logger.error(
            "pgvector: recreate_embedding_column falhou",
            extra={
                "event": "pgvector.column.recreate_failed",
                "dim_expected": expected_dim,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return {
            "ok": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
        }


# ─── CRUD ────────────────────────────────────────────────────────


async def upsert_chunks(chunks: list[dict]) -> int:
    """Insere/atualiza embeddings de chunks já existentes no Postgres.

    IMPORTANTE: pgvector NÃO duplica o chunk em outra tabela — atualiza a
    coluna `embedding` da row já gravada por ingest_text. Por isso este
    upsert assume que evidence_chunks já tem a row com id=c["id"]; do
    contrário a UPDATE não afeta nada (não cria row nova).

    Diferença chave vs qdrant_store.upsert_chunks: lá criava pontos
    independentes; aqui só associa o vetor à row existente. Ingest.py
    sabe da diferença e ajusta a sequência (ver roteador de backend).

    Args:
        chunks: dicts {id, embedding, source_id, ordinal}.

    Returns:
        Quantidade de rows efetivamente atualizadas. 0 se coluna indisponível
        ou se nenhum id casou.
    """
    if not chunks:
        return 0
    if not await ensure_embedding_column():
        source_ids = list({c.get("source_id") for c in chunks if c.get("source_id")})
        try:
            actual_dim = await _column_dim()
        except Exception:
            actual_dim = None
        # ERROR (não warning): o chamador segue com HTTP 200/partial=true e o
        # sintoma some de vista — busca vetorial degrada para BM25-only até
        # alguém reindexar. Este é o único rastro acionável do drift.
        logger.error(
            "pgvector upsert_chunks abortado: coluna indisponível/dimensão em "
            "drift — vetores NÃO gravados (ingest partial; busca cai em BM25)",
            extra={
                "event": "pgvector.upsert.blocked_dim_mismatch",
                "chunk_count": len(chunks),
                "source_ids": source_ids,
                "dim_actual": actual_dim,
                "dim_expected": get_active_embedding_dim(),
                "hint": "POST /api/v1/evidence/reindex (ou botão Reindexar em /rag)",
            },
        )
        return 0
    try:
        pool = _get_pool()
        n = 0
        async with pool.acquire() as con:
            async with con.transaction():
                # UPDATE em batch via UNNEST seria mais rápido, mas executemany
                # é claro e mantém ordem. Loop simples; volume típico < 100.
                for c in chunks:
                    res = await con.execute(
                        f"UPDATE evidence_chunks SET {EMBEDDING_COLUMN} = $1 WHERE id = $2",
                        c["embedding"],
                        c["id"],
                    )
                    # asyncpg devolve "UPDATE n"
                    try:
                        n += int(res.rsplit(" ", 1)[-1])
                    except (ValueError, IndexError):
                        pass
        return n
    except Exception as e:
        source_ids = list({c.get("source_id") for c in chunks if c.get("source_id")})
        logger.warning(
            "pgvector upsert_chunks falhou — embeddings não foram associados",
            extra={
                "event": "pgvector.upsert.failed",
                "chunk_count": len(chunks),
                "embedding_dim": len(chunks[0].get("embedding") or []) if chunks else None,
                "source_ids": source_ids,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return 0


async def search(query_vector: list[float], top_n: int = 20,
                 source_ids: Optional[list[str]] = None) -> list[dict]:
    """Busca top_n vizinhos por similaridade cosseno.

    Mesma assinatura/retorno de qdrant_store.search — chunk_id, source_id,
    ordinal, score (0..1, maior = mais similar). Score = 1 - cosine_distance.

    Filtro `source_ids` usa B-tree index idx_evidence_chunks_source (já existia
    para BM25). Pré-filtro SQL → KNN só nos candidatos qualificados.

    Autorização: só chunks de knowledge_sources com `authorized = 1` — mesma
    regra que o braço BM25 (runtime._bm25_search) já aplicava via JOIN. Sem
    isso, uma base desautorizada continuava recuperável pelo braço vetorial.

    Lista vazia se coluna não existe / drift de dim / Postgres offline.
    Caller cai em BM25-only.
    """
    if not await ensure_embedding_column():
        return []
    try:
        pool = _get_pool()
        async with pool.acquire() as con:
            if source_ids:
                # `<=>` é o operador cosine de pgvector (retorna distância 0..2;
                # quanto MENOR mais similar).
                rows = await con.fetch(
                    f"""
                    SELECT id AS chunk_id, knowledge_source_id AS source_id,
                           ordinal,
                           1 - ({EMBEDDING_COLUMN} <=> $1) AS score
                    FROM evidence_chunks
                    WHERE {EMBEDDING_COLUMN} IS NOT NULL
                      AND knowledge_source_id = ANY($3::text[])
                      AND knowledge_source_id IN (
                          SELECT id FROM knowledge_sources WHERE authorized = 1)
                    ORDER BY {EMBEDDING_COLUMN} <=> $1
                    LIMIT $2
                    """,
                    query_vector, top_n, source_ids,
                )
            else:
                rows = await con.fetch(
                    f"""
                    SELECT id AS chunk_id, knowledge_source_id AS source_id,
                           ordinal,
                           1 - ({EMBEDDING_COLUMN} <=> $1) AS score
                    FROM evidence_chunks
                    WHERE {EMBEDDING_COLUMN} IS NOT NULL
                      AND knowledge_source_id IN (
                          SELECT id FROM knowledge_sources WHERE authorized = 1)
                    ORDER BY {EMBEDDING_COLUMN} <=> $1
                    LIMIT $2
                    """,
                    query_vector, top_n,
                )
        return [
            {
                "chunk_id": r["chunk_id"],
                "source_id": r["source_id"],
                "ordinal": r["ordinal"],
                "score": float(r["score"]) if r["score"] is not None else 0.0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(
            "pgvector search falhou — caller cai em BM25-only",
            extra={
                "event": "pgvector.search.failed",
                "top_n": top_n,
                "source_filter_count": len(source_ids) if source_ids else 0,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return []


async def delete_by_source(source_id: str) -> bool:
    """Remove o embedding (SET NULL) de todos os chunks de uma source.

    NÃO deleta os chunks — texto + tsvector continuam (BM25 ainda funciona).
    Caller (ingest replace=True) faz DELETE FROM evidence_chunks separado.
    Esta função existe pra paridade com qdrant_store.delete_by_source().

    Returns True se ok, False em erro (coluna pode não existir ainda — é OK).
    """
    if not await ensure_embedding_column():
        # Coluna não existe = nada para deletar, considera sucesso.
        return True
    try:
        pool = _get_pool()
        async with pool.acquire() as con:
            await con.execute(
                f"UPDATE evidence_chunks SET {EMBEDDING_COLUMN} = NULL WHERE knowledge_source_id = $1",
                source_id,
            )
        return True
    except Exception as e:
        logger.warning(
            "pgvector delete_by_source falhou — vetores antigos podem persistir até próximo replace/reindex",
            extra={
                "event": "pgvector.delete.failed",
                "source_id": source_id,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return False


# ─── Diagnóstico ─────────────────────────────────────────────────


async def collection_info() -> Optional[dict]:
    """Diagnóstico: estado da coluna embedding + drift detection.

    Paridade com qdrant_store.collection_info(). UI usa pra decidir se mostra
    alerta de "reindexar agora".

    Returns:
        {
          "name": "evidence_chunks.embedding",
          "exists": bool,                # coluna existe?
          "points_count": int | None,    # qtos chunks com embedding NOT NULL
          "status": str,                 # "green" | "missing" | "drift"
          "dim_actual": int | None,
          "dim_expected": int,
          "dim_match": bool,
          "backend": "pgvector",
        }
        ou None se Postgres offline (improvável aqui — RuntimeError de _get_pool).
    """
    expected_dim = get_active_embedding_dim()
    try:
        actual_dim = await _column_dim()
        if actual_dim is None:
            return {
                "name": "evidence_chunks.embedding",
                "exists": False,
                "points_count": 0,
                "status": "missing",
                "dim_actual": None,
                "dim_expected": expected_dim,
                "dim_match": False,
                "backend": "pgvector",
            }
        pool = _get_pool()
        async with pool.acquire() as con:
            points_count = await con.fetchval(
                f"SELECT COUNT(*) FROM evidence_chunks WHERE {EMBEDDING_COLUMN} IS NOT NULL"
            )
        dim_match = actual_dim == expected_dim
        return {
            "name": "evidence_chunks.embedding",
            "exists": True,
            "points_count": int(points_count or 0),
            "status": "green" if dim_match else "drift",
            "dim_actual": actual_dim,
            "dim_expected": expected_dim,
            "dim_match": dim_match,
            "backend": "pgvector",
        }
    except Exception as e:
        logger.warning(
            "pgvector collection_info falhou",
            extra={
                "event": "pgvector.collection_info.failed",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return None


# ─── Aliases para paridade com qdrant_store ──────────────────────


# qdrant_store.recreate_collection → pgvector recreate_embedding_column.
# Mesmo nome de função "publica" facilita o roteador em ingest.py.
recreate_collection = recreate_embedding_column

# qdrant_store.ensure_collection → pgvector ensure_embedding_column.
ensure_collection = ensure_embedding_column
