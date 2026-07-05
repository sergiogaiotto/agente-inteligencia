"""Pipeline de ingestão (Onda 3).

Recebe texto cru, chunca, embeda, persiste em Postgres + Qdrant.
Idempotente quando `replace=True` (default): apaga chunks anteriores antes
de inserir os novos.

Falhas tratadas com semantica clara:
- source não existe → 404
- embeddings indisponível (Azure ou qwen3) → 503 com mensagem fiel ao provider
- Qdrant offline mas Postgres OK → retorna `partial=true`; usuário pode rodar
  /reindex depois quando Qdrant voltar
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.config import get_settings
from app.core.database import _get_pool, evidence_chunks_repo, knowledge_repo
from app.core.datetime_utils import naive_utc_now
from app.core.otel import get_tracer
from app.evidence.chunker import chunk_text
from app.evidence.embedder import embed_texts, embeddings_unavailable_detail


# Sentinel para identificar documentos legados (sem metadata.source_doc_id).
# Usado no endpoint GET /documents e DELETE /documents/{doc_id} para que o
# operador consiga lidar com chunks ingeridos antes desta PR (PR #227).
LEGACY_DOC_ID = "_legacy_"

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


def _get_vector_store():
    """Retorna o módulo pgvector_store (único backend desde Onda Q).

    Histórico: até Onda Q (2026-05-30) existia branch pra qdrant_store via
    `rag_vector_backend == "qdrant"`. Qdrant foi descontinuado — pgvector
    cobre 100% dos casos com Postgres já presente na infra (sem 2º serviço).
    Helper mantido por convenção/compat de chamadas existentes.
    """
    from app.evidence import pgvector_store
    return pgvector_store


class IngestError(Exception):
    """Erro de ingestão com status code HTTP recomendado."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


async def ingest_text(
    source_id: str,
    text: str,
    replace: bool = True,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    *,
    source_doc_id: Optional[str] = None,
    source_filename: Optional[str] = None,
    source_format: Optional[str] = None,
    source_uri: Optional[str] = None,
) -> dict:
    """Ingere `text` na knowledge_source `source_id`.

    Args:
        source_id: id da knowledge_source destino. Deve existir.
        text: conteúdo a indexar. Não vazio.
        replace: comportamento de remoção antes da inserção:
            - True + source_doc_id passado pelo caller: apaga só chunks deste doc
              (re-ingestão idempotente do mesmo documento).
            - True + source_doc_id None (auto-gerado): apaga TUDO da KB
              (compat com comportamento legado: 1 ingest = 1 KB).
            - False: não apaga nada; adiciona novos chunks (PR #227 — múltiplos
              docs por KB).
        chunk_size: tokens por chunk. None (default) usa RAG_CHUNK_SIZE_TOKENS do .env.
        chunk_overlap: tokens de overlap entre chunks adjacentes. None usa default.
        source_doc_id: UUID do documento. Se None, é gerado. Quando o caller
            passa explicitamente, sinaliza "re-ingestão" → replace afeta só
            este doc, não toda a KB. PR #227.
        source_filename: nome de arquivo original (para exibição no inspector).
        source_format: extensão/tipo do source (ex: 'pdf', 'docx', 'url',
            'text/markdown'). PR #227.
        source_uri: URI canônico (URL fonte, ou path opcional). PR #227.

    Returns:
        {
          "source_id": ...,
          "source_doc_id": str,    # gerado ou passado (PR #227)
          "chunks_created": N,
          "tokens_total": N,
          "qdrant_upserted": N,
          "duration_ms": N,
          "partial": bool,
        }

    Raises:
        IngestError: source não existe (404), texto vazio (400), Azure embeddings
                     indisponível (503).
    """
    with _tracer.start_as_current_span("ingest.text") as span:
        span.set_attribute("source.id", source_id)
        span.set_attribute("text.length", len(text or ""))

        if not text or not text.strip():
            raise IngestError("Texto vazio.", status_code=400)

        # Verifica que source existe
        source = await knowledge_repo.find_by_id(source_id)
        if not source:
            raise IngestError(f"knowledge_source '{source_id}' não encontrada.", status_code=404)

        start = time.time()

        # 1. Chunca (size/overlap opcionais — None usa defaults do .env)
        chunks = chunk_text(text, size=chunk_size, overlap=chunk_overlap)
        if chunk_size or chunk_overlap:
            span.set_attribute("chunk.size_override", chunk_size or 0)
            span.set_attribute("chunk.overlap_override", chunk_overlap or 0)
        if not chunks:
            raise IngestError("Texto não gerou chunks após normalização.", status_code=400)
        span.set_attribute("chunks.count", len(chunks))
        tokens_total = sum(c.token_count for c in chunks)
        span.set_attribute("chunks.tokens_total", tokens_total)

        # 2. Embeda em batch único (Azure aguenta 2048+ chunks/req, mas qualquer
        # erro abortamos para preservar consistência).
        with _tracer.start_as_current_span("ingest.embed"):
            vectors = await embed_texts([c.text for c in chunks])
        if vectors is None:
            # Mensagem fiel ao provider ativo (azure|qwen3) — ver embedder.py.
            raise IngestError(embeddings_unavailable_detail(), status_code=503)
        if len(vectors) != len(chunks):
            raise IngestError(
                f"Embeddings devolveu {len(vectors)} vetores para {len(chunks)} chunks.",
                status_code=500,
            )

        # 3. Replace: limpa state anterior (Postgres + vector store).
        #    PR #227: comportamento depende de se o caller forneceu source_doc_id.
        #    - caller_provided_doc_id=True: re-ingest deste doc → apaga só seus chunks.
        #    - caller_provided_doc_id=False: comportamento legado → apaga TUDO da KB.
        caller_provided_doc_id = source_doc_id is not None
        if source_doc_id is None:
            source_doc_id = str(uuid.uuid4())
        span.set_attribute("ingest.source_doc_id", source_doc_id)
        span.set_attribute("ingest.caller_provided_doc_id", caller_provided_doc_id)

        # Monta payload de metadata (vai em cada chunk via JSONB)
        chunk_metadata = {
            "source_doc_id": source_doc_id,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        if source_filename:
            chunk_metadata["source_filename"] = source_filename
        if source_format:
            chunk_metadata["source_format"] = source_format
        if source_uri:
            chunk_metadata["source_uri"] = source_uri
        chunk_metadata_json = json.dumps(chunk_metadata, ensure_ascii=False)

        pool = _get_pool()
        vector_store = _get_vector_store()
        # Onda Q (2026-05-30): backend único pgvector — antes lia
        # rag_vector_backend (removido em Q.3).
        backend_name = "pgvector"
        span.set_attribute("rag.vector_backend", backend_name)
        if replace:
            with _tracer.start_as_current_span("ingest.delete_old"):
                async with pool.acquire() as con:
                    if caller_provided_doc_id:
                        # Apaga só chunks deste documento — não toca os outros
                        await con.execute(
                            "DELETE FROM evidence_chunks "
                            "WHERE knowledge_source_id = $1 "
                            "AND metadata->>'source_doc_id' = $2",
                            source_id, source_doc_id,
                        )
                    else:
                        # Legado: apaga TODOS os chunks da KB
                        await con.execute(
                            "DELETE FROM evidence_chunks WHERE knowledge_source_id = $1",
                            source_id,
                        )
                # Vector store: best-effort. Qdrant: deleta pontos. pgvector:
                # no-op (rows já foram apagadas pelo DELETE acima, vetor foi
                # junto na mesma transação). Mantemos a chamada por paridade
                # e pra cobertura de edge cases (race com query).
                await vector_store.delete_by_source(source_id)

        # 4. Insere chunks no Postgres + monta payload do vector store
        chunk_ids: list[str] = []
        async with pool.acquire() as con:
            async with con.transaction():
                for c in chunks:
                    cid = str(uuid.uuid4())
                    chunk_ids.append(cid)
                    await con.execute(
                        """
                        INSERT INTO evidence_chunks (id, knowledge_source_id, ordinal, text, token_count, char_count, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                        """,
                        cid, source_id, c.ordinal, c.text, c.token_count, c.char_count,
                        chunk_metadata_json,
                    )

        # 5. Associa vetores ao vector store ativo.
        # Qdrant: cria pontos independentes apontando para os chunk_ids.
        # pgvector: UPDATE evidence_chunks SET embedding=... WHERE id IN (...).
        vector_payload = [
            {
                "id": chunk_ids[i],
                "embedding": vectors[i],
                "source_id": source_id,
                "ordinal": chunks[i].ordinal,
            }
            for i in range(len(chunks))
        ]
        with _tracer.start_as_current_span("ingest.vector_upsert") as qspan:
            qspan.set_attribute("rag.vector_backend", backend_name)
            qspan.set_attribute("vector.expected", len(chunks))
            vector_n = await vector_store.upsert_chunks(vector_payload)
            qspan.set_attribute("vector.upserted", vector_n)
            qspan.set_attribute("vector.partial", vector_n != len(chunks))
            if vector_n != len(chunks):
                # Marca o span como erro pra alarmar via tracing (Tempo → Grafana).
                # Import local: trace é dep opcional, evita custo no happy path.
                from opentelemetry.trace import Status, StatusCode
                qspan.set_status(Status(StatusCode.ERROR, "vector upsert parcial ou abortado"))

        partial = vector_n != len(chunks)
        if partial:
            logger.warning(
                "Ingestão parcial: Postgres tem todos os chunks, vetores divergem",
                extra={
                    "event": "evidence.ingest.partial",
                    "rag_vector_backend": backend_name,
                    "source_id": source_id,
                    "chunks_expected": len(chunks),
                    "vector_upserted": vector_n,
                    "tokens_total": tokens_total,
                    "hint": f"Re-execute a ingestão; logs anteriores de {backend_name}.* têm a causa raiz",
                },
            )

        # 6. Atualiza metadados da source
        await knowledge_repo.update(source_id, {
            "last_updated": naive_utc_now().isoformat(),
            "index_version": f"v3-{int(time.time())}",
        })

        duration_ms = int((time.time() - start) * 1000)
        result = {
            "source_id": source_id,
            "source_doc_id": source_doc_id,
            "chunks_created": len(chunks),
            "tokens_total": tokens_total,
            # qdrant_upserted: retrocompat para UI e clients antigos.
            # vector_upserted: nome backend-agnóstico (preferir em novos consumers).
            # Ambos refletem o mesmo número.
            "qdrant_upserted": vector_n,
            "vector_upserted": vector_n,
            "rag_vector_backend": backend_name,
            "duration_ms": duration_ms,
            "partial": partial,
        }
        logger.info(
            "Ingest concluído",
            extra={"event": "evidence.ingest.completed", **result},
        )
        return result


# ─── Multi-doc: list/delete por documento (PR #227) ───────────────


async def list_documents_for_source(source_id: str) -> list[dict]:
    """Lista documentos ingeridos agrupados por `metadata.source_doc_id`.

    Cada documento agrega seus chunks: contagem, tokens totais, filename,
    formato, URI, timestamp da ingestão. Chunks sem metadata (legados, de
    antes da PR #227) são agrupados em um documento sentinela com
    `doc_id = LEGACY_DOC_ID` ("_legacy_") — assim o operador pode pelo menos
    apagá-los sem precisar usar `clear_source` (que apaga TUDO).

    Returns:
        Lista de dicts com:
          {
            "source_doc_id": str,
            "source_filename": str | None,
            "source_format": str | None,
            "source_uri": str | None,
            "ingested_at": str | None,    # ISO timestamp, mínimo entre chunks
            "chunks_count": int,
            "tokens_total": int,
            "is_legacy": bool,
          }
        Ordenada por `ingested_at` desc (mais recente primeiro).
    """
    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT
                COALESCE(metadata->>'source_doc_id', $2) AS source_doc_id,
                MAX(metadata->>'source_filename')        AS source_filename,
                MAX(metadata->>'source_format')          AS source_format,
                MAX(metadata->>'source_uri')             AS source_uri,
                MIN(metadata->>'ingested_at')            AS ingested_at,
                COUNT(*)                                  AS chunks_count,
                COALESCE(SUM(token_count), 0)            AS tokens_total
            FROM evidence_chunks
            WHERE knowledge_source_id = $1
            GROUP BY COALESCE(metadata->>'source_doc_id', $2)
            ORDER BY MIN(metadata->>'ingested_at') DESC NULLS LAST
            """,
            source_id, LEGACY_DOC_ID,
        )

    out: list[dict] = []
    for r in rows:
        doc_id = r["source_doc_id"]
        out.append({
            "source_doc_id": doc_id,
            "source_filename": r["source_filename"],
            "source_format": r["source_format"],
            "source_uri": r["source_uri"],
            "ingested_at": r["ingested_at"],
            "chunks_count": int(r["chunks_count"] or 0),
            "tokens_total": int(r["tokens_total"] or 0),
            "is_legacy": doc_id == LEGACY_DOC_ID,
        })
    return out


async def delete_document(source_id: str, doc_id: str) -> dict:
    """Apaga todos os chunks de um documento específico da KB. Idempotente.

    Para `doc_id == LEGACY_DOC_ID` ("_legacy_"), apaga os chunks SEM
    `metadata.source_doc_id` (ingeridos antes da PR #227).

    Returns:
        {
          "source_id": ..., "source_doc_id": ...,
          "chunks_deleted": int,
        }
    """
    pool = _get_pool()
    async with pool.acquire() as con:
        if doc_id == LEGACY_DOC_ID:
            res = await con.execute(
                "DELETE FROM evidence_chunks "
                "WHERE knowledge_source_id = $1 "
                "AND (metadata IS NULL OR metadata->>'source_doc_id' IS NULL)",
                source_id,
            )
        else:
            res = await con.execute(
                "DELETE FROM evidence_chunks "
                "WHERE knowledge_source_id = $1 "
                "AND metadata->>'source_doc_id' = $2",
                source_id, doc_id,
            )
    try:
        deleted = int(res.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        deleted = 0

    logger.info(
        "document_delete_completed",
        extra={
            "event": "evidence.document.delete.completed",
            "source_id": source_id,
            "source_doc_id": doc_id,
            "chunks_deleted": deleted,
        },
    )
    return {
        "source_id": source_id,
        "source_doc_id": doc_id,
        "chunks_deleted": deleted,
    }


async def clear_source(source_id: str) -> dict:
    """Apaga todos os chunks de uma source (Postgres + vector store ativo).
    Idempotente."""
    with _tracer.start_as_current_span("ingest.clear") as span:
        span.set_attribute("source.id", source_id)
        pool = _get_pool()
        async with pool.acquire() as con:
            res = await con.execute(
                "DELETE FROM evidence_chunks WHERE knowledge_source_id = $1",
                source_id,
            )
        # asyncpg devolve "DELETE n"
        try:
            pg_deleted = int(res.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            pg_deleted = 0
        vector_store = _get_vector_store()
        vector_ok = await vector_store.delete_by_source(source_id)
        return {
            "source_id": source_id,
            "postgres_deleted": pg_deleted,
            # qdrant_deleted: retrocompat. vector_deleted: nome backend-agnóstico.
            "qdrant_deleted": vector_ok,
            "vector_deleted": vector_ok,
        }


# ─── Reindex global (recreate collection + re-embarcar todos os chunks) ──────

async def reindex_all(
    *,
    recreate_collection: bool = True,
    batch_size: int = 64,
) -> dict:
    """Re-embarca todos os chunks do Postgres no Qdrant.

    Caso de uso principal: usuário trocou o provider de embedding pela UI
    (Azure 1536 → Qwen3 1024), o que invalidou a collection existente. Esta
    função recria a collection com a dim correta e re-popula a partir do
    Postgres (que é o source-of-truth do texto).

    Args:
        recreate_collection: se True (default), DROPA e RECRIA a collection
            antes — apaga todos os vetores antigos. Use False só se você acabou
            de criar a collection do zero e quer só popular.
        batch_size: chunks por chamada de embed_texts/qdrant_upsert. Default 64
            equilibra latência (1 round-trip por batch) e memória.

    Returns:
        {
          "ok": bool,
          "recreated": bool,                 # se recreate_collection foi True
          "dim_before": int | None,          # dim antiga da collection (None se não existia)
          "dim_after": int,                  # dim nova (== provider ativo)
          "chunks_total": int,               # chunks no Postgres
          "chunks_embedded": int,            # embeddings gerados com sucesso
          "chunks_upserted": int,            # vetores que o Qdrant aceitou
          "sources_count": int,              # qtas knowledge_sources distintas
          "batches": int,                    # qtos batches processados
          "duration_ms": int,
          "errors": list[dict],              # batches que falharam, com {batch_idx, error_type, chunk_ids}
        }

    Idempotência:
        Com recreate_collection=True, é idempotente — sempre converge pro mesmo
        estado (todos os chunks do Postgres no Qdrant com dim correta). Com
        recreate_collection=False, depende do estado prévio do Qdrant.

    Backend:
        Roteado via Settings.rag_vector_backend. Qdrant: recria collection +
        upsert pontos. pgvector: DROP+ADD coluna embedding + UPDATE em batch.
        Mesma interface — caller não diferencia.
    """
    # Onda Q (2026-05-30): get_active_embedding_dim migrou de qdrant_store
    # pra embedder.py (backend-neutral, só lê settings). qdrant_store
    # removido — backend agora é sempre pgvector.
    from app.evidence.embedder import get_active_embedding_dim, resolve_effective_provider

    # Resolve o provider de embedding EFETIVO antes de recriar a coluna: se o
    # configurado (ex.: qwen3) está inacessível, o roteamento cai no fallback
    # (ex.: azure, dim 1536). Sem isso, recreate criaria a coluna com a dim do
    # provider CONFIGURADO (1024) e o re-embed via fallback (1536) daria drift.
    await resolve_effective_provider()

    vector_store = _get_vector_store()
    backend_name = "pgvector"  # Onda Q: backend único após remoção do Qdrant

    start = time.time()
    with _tracer.start_as_current_span("ingest.reindex_all") as span:
        span.set_attribute("reindex.recreate", recreate_collection)
        span.set_attribute("reindex.batch_size", batch_size)
        span.set_attribute("rag.vector_backend", backend_name)

        result: dict = {
            "ok": False,
            "recreated": False,
            "dim_before": None,
            "dim_after": get_active_embedding_dim(),
            "chunks_total": 0,
            "chunks_embedded": 0,
            "chunks_upserted": 0,
            "sources_count": 0,
            "batches": 0,
            "duration_ms": 0,
            "errors": [],
            "rag_vector_backend": backend_name,
        }

        # 1. Recreate da collection / coluna (destrutivo).
        if recreate_collection:
            recreate_res = await vector_store.recreate_collection()
            if not recreate_res.get("ok"):
                span.set_attribute("reindex.path", "recreate_failed")
                result["errors"].append({
                    "stage": "recreate_collection",
                    "error_type": recreate_res.get("error_type"),
                    "error_message": recreate_res.get("error_message"),
                })
                result["duration_ms"] = int((time.time() - start) * 1000)
                logger.error(
                    "Reindex abortado: recreate_collection falhou",
                    extra={
                        "event": "evidence.reindex.aborted",
                        **{k: v for k, v in result.items() if k != "errors"},
                    },
                )
                return result
            result["recreated"] = True
            result["dim_before"] = recreate_res.get("dim_before")
            result["dim_after"] = recreate_res.get("dim_after")

        # 2. Carrega todos os chunks do Postgres ordenados por source (locality).
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT id, knowledge_source_id, ordinal, text
                FROM evidence_chunks
                ORDER BY knowledge_source_id, ordinal
                """,
            )
        chunks_total = len(rows)
        result["chunks_total"] = chunks_total
        result["sources_count"] = len({r["knowledge_source_id"] for r in rows})
        span.set_attribute("reindex.chunks_total", chunks_total)
        span.set_attribute("reindex.sources_count", result["sources_count"])

        if chunks_total == 0:
            result["ok"] = True
            result["duration_ms"] = int((time.time() - start) * 1000)
            logger.info(
                "Reindex completo: nenhum chunk no Postgres para re-embarcar",
                extra={"event": "evidence.reindex.completed_empty", **{k: v for k, v in result.items() if k != "errors"}},
            )
            return result

        # 3. Processa em batches: embed → upsert.
        n_batches = (chunks_total + batch_size - 1) // batch_size
        for batch_idx in range(n_batches):
            lo = batch_idx * batch_size
            hi = min(lo + batch_size, chunks_total)
            batch = rows[lo:hi]
            texts = [r["text"] for r in batch]

            try:
                vectors = await embed_texts(texts)
            except Exception as e:
                logger.warning(
                    "Reindex batch: embed_texts lançou exceção",
                    extra={
                        "event": "evidence.reindex.batch_embed_failed",
                        "batch_idx": batch_idx,
                        "batch_size": len(batch),
                        "error_type": type(e).__name__,
                    },
                    exc_info=True,
                )
                result["errors"].append({
                    "stage": "embed",
                    "batch_idx": batch_idx,
                    "error_type": type(e).__name__,
                    "chunk_ids": [r["id"] for r in batch],
                })
                continue

            if not vectors or len(vectors) != len(batch):
                logger.warning(
                    "Reindex batch: embeddings retornaram quantidade inesperada",
                    extra={
                        "event": "evidence.reindex.batch_embed_short",
                        "batch_idx": batch_idx,
                        "batch_size": len(batch),
                        "vectors_returned": len(vectors) if vectors else 0,
                    },
                )
                result["errors"].append({
                    "stage": "embed",
                    "batch_idx": batch_idx,
                    "error_type": "ShortBatch",
                    "expected": len(batch),
                    "got": len(vectors) if vectors else 0,
                })
                continue

            result["chunks_embedded"] += len(vectors)

            payload = [
                {
                    "id": batch[i]["id"],
                    "embedding": vectors[i],
                    "source_id": batch[i]["knowledge_source_id"],
                    "ordinal": batch[i]["ordinal"],
                }
                for i in range(len(batch))
            ]
            upserted = await vector_store.upsert_chunks(payload)
            result["chunks_upserted"] += upserted
            if upserted != len(batch):
                # vector_store.upsert_chunks já logou a causa raiz —
                # aqui só registra no resumo do reindex.
                result["errors"].append({
                    "stage": "upsert",
                    "batch_idx": batch_idx,
                    "expected": len(batch),
                    "got": upserted,
                })
            result["batches"] += 1

        result["ok"] = result["chunks_upserted"] == chunks_total
        result["duration_ms"] = int((time.time() - start) * 1000)
        span.set_attribute("reindex.chunks_embedded", result["chunks_embedded"])
        span.set_attribute("reindex.chunks_upserted", result["chunks_upserted"])
        span.set_attribute("reindex.errors_count", len(result["errors"]))
        span.set_attribute("reindex.ok", result["ok"])

        if not result["ok"]:
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, "reindex parcial"))

        logger.info(
            "Reindex concluído",
            extra={
                "event": "evidence.reindex.completed",
                **{k: v for k, v in result.items() if k != "errors"},
                "errors_count": len(result["errors"]),
            },
        )
        return result


async def list_chunks(source_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Lista chunks de uma source (debug/UI). Sem o tsvector (TEXT pesado)."""
    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT id, knowledge_source_id, ordinal, text, token_count, char_count, created_at
            FROM evidence_chunks
            WHERE knowledge_source_id = $1
            ORDER BY ordinal
            LIMIT $2 OFFSET $3
            """,
            source_id, limit, offset,
        )
        return [dict(r) for r in rows]


# ─── Onda 6: ingest multi-formato via markitdown ─────────────────────────

async def ingest_file(
    source_id: str,
    data: bytes,
    filename: str,
    replace: bool = True,
    mime_type: Optional[str] = None,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
) -> dict:
    """Ingere arquivo binário (PDF/DOCX/PPTX/XLSX/HTML/MD/CSV/audio/imagem/...)
    convertendo via markitdown → markdown → pipeline padrão (chunk + embed + store).

    Args:
        source_id: knowledge_source destino.
        data: bytes do arquivo.
        filename: nome original (extensão guia o converter do markitdown).
        replace: idempotência — apaga chunks anteriores antes.
        mime_type: opcional, override da detecção por extensão.

    Returns:
        Mesmo shape de `ingest_text` + {"converter": "markitdown", "source_format": ext}.

    Raises:
        IngestError: source não existe (404), arquivo vazio (400), markitdown
                     indisponível (503), conversão falhou (500).
    """
    from app.evidence.converters import convert_bytes, ConverterError

    with _tracer.start_as_current_span("ingest.file") as span:
        span.set_attribute("source.id", source_id)
        span.set_attribute("file.name", filename or "(sem nome)")
        span.set_attribute("file.size", len(data or b""))

        if not data:
            raise IngestError("Arquivo vazio.", status_code=400)
        if not filename:
            raise IngestError("filename é obrigatório (markitdown usa extensão).", status_code=400)

        # Confirma source existe ANTES de gastar conversão (evita custo perdido).
        if not await knowledge_repo.find_by_id(source_id):
            raise IngestError(f"knowledge_source '{source_id}' não encontrada.", status_code=404)

        try:
            text = convert_bytes(data, filename, mime_type=mime_type)
        except ConverterError as e:
            raise IngestError(str(e), status_code=e.status_code)

        if not text:
            raise IngestError(
                f"Conversão de '{filename}' produziu markdown vazio. "
                "Arquivo pode estar vazio, criptografado ou em formato não suportado.",
                status_code=400,
            )
        span.set_attribute("converter.markdown_chars", len(text))

        # Pipeline padrão: chunk → embed → store. PR #227: propaga metadata
        # do arquivo para que o documento fique rastreável no inspector
        # (UI Documentos + DELETE por doc_id).
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        result = await ingest_text(
            source_id, text, replace=replace,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            source_filename=filename,
            source_format=ext,
        )
        result["converter"] = "markitdown"
        result["source_format"] = ext
        result["source_filename"] = filename
        return result


async def ingest_url(
    source_id: str,
    url: str,
    replace: bool = True,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
) -> dict:
    """Ingere URL (página web, PDF online, YouTube transcript, RSS, ...) via
    markitdown → markdown → pipeline padrão.

    Args:
        source_id: knowledge_source destino.
        url: URL absoluta http(s).
        replace: idempotência.

    Returns:
        Mesmo shape de ingest_text + {"converter": "markitdown", "source_url": url}.
    """
    from app.evidence.converters import convert_url, ConverterError

    with _tracer.start_as_current_span("ingest.url") as span:
        span.set_attribute("source.id", source_id)
        span.set_attribute("url", (url or "")[:200])

        if not url or not url.strip():
            raise IngestError("URL vazia.", status_code=400)

        if not await knowledge_repo.find_by_id(source_id):
            raise IngestError(f"knowledge_source '{source_id}' não encontrada.", status_code=404)

        try:
            text = convert_url(url)
        except ConverterError as e:
            raise IngestError(str(e), status_code=e.status_code)

        if not text:
            raise IngestError(
                f"URL '{url}' retornou markdown vazio após conversão. "
                "Página pode exigir login, ser SPA pura, ou ter conteúdo só em iframes.",
                status_code=400,
            )
        span.set_attribute("converter.markdown_chars", len(text))

        # PR #227: propaga URL como metadata do documento ingerido.
        # filename derivado do path da URL ou domínio para exibição no inspector.
        url_clean = url.strip()
        derived_filename = url_clean.rsplit("/", 1)[-1] or url_clean[:60]
        result = await ingest_text(
            source_id, text, replace=replace,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            source_filename=derived_filename,
            source_format="url",
            source_uri=url_clean,
        )
        result["converter"] = "markitdown"
        result["source_url"] = url_clean
        return result


async def source_stats(source_id: str) -> dict:
    """Estatísticas operacionais de uma source: contagem de chunks, total de tokens,
    timestamp do último chunk criado, last_updated da source.

    Útil pra UI mostrar "N chunks · ingerido há Xh" sem buscar todos os chunks."""
    pool = _get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            SELECT
              COUNT(*) AS chunks_count,
              COALESCE(SUM(token_count), 0) AS tokens_total,
              COALESCE(SUM(char_count), 0) AS chars_total,
              MAX(created_at) AS last_chunk_at
            FROM evidence_chunks
            WHERE knowledge_source_id = $1
            """,
            source_id,
        )
    source = await knowledge_repo.find_by_id(source_id)
    return {
        "source_id": source_id,
        "chunks_count": int(row["chunks_count"] or 0) if row else 0,
        "tokens_total": int(row["tokens_total"] or 0) if row else 0,
        "chars_total": int(row["chars_total"] or 0) if row else 0,
        "last_chunk_at": row["last_chunk_at"].isoformat() if row and row["last_chunk_at"] else None,
        "last_updated": (source or {}).get("last_updated"),
        "index_version": (source or {}).get("index_version"),
    }
