"""Wrapper async do Qdrant (Onda 3 — RAG real).

Encapsula AsyncQdrantClient com:
- Singleton lazy (cria cliente na primeira chamada).
- ensure_collection() idempotente — cria a collection no primeiro uso.
- Operações CRUD (upsert, search, delete_by_source).
- Graceful degradation: se Qdrant offline, todos os métodos retornam vazio/False
  com warning no log; nunca propagam exception para quebrar a API.

Convenções:
- 1 collection global (`agente_evidence`), distance=Cosine, dim=1536 (text-embedding-3-small).
- Ponto = chunk. Payload mínimo: {"knowledge_source_id", "ordinal", "chunk_id"}.
  O texto NÃO vai no payload — fica só no Postgres (evita duplicidade e custo).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Dimensão de text-embedding-3-small. Se trocar de modelo, atualizar aqui
# e re-criar a collection (ou mudar nome via setting).
EMBEDDING_DIM = 1536

# Singleton + lock para inicialização concorrente
_client = None
_init_lock = asyncio.Lock()
_collection_ready = False


async def get_client():
    """Retorna o AsyncQdrantClient singleton. Cria na primeira chamada.

    Importa qdrant_client lazy: se a lib não estiver instalada (improvável,
    está no requirements), retornamos None e callers caem em fallback.
    """
    global _client
    if _client is not None:
        return _client
    async with _init_lock:
        if _client is not None:
            return _client
        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError:
            logger.warning(
                "qdrant_client não instalado — RAG vetorial desligado",
                extra={"event": "qdrant.client.import_failed"},
            )
            return None
        settings = get_settings()
        try:
            _client = AsyncQdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key or None,
                # Timeout curto para falhar rápido — fallback BM25 assume.
                timeout=10,
            )
            return _client
        except Exception as e:
            logger.warning(
                "Falha ao criar AsyncQdrantClient — RAG vetorial desligado",
                extra={
                    "event": "qdrant.client.init_failed",
                    "qdrant_url": settings.qdrant_url,
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            return None


async def ensure_collection() -> bool:
    """Cria a collection se não existir. Idempotente. Retorna True se pronta."""
    global _collection_ready
    if _collection_ready:
        return True
    client = await get_client()
    if client is None:
        return False
    settings = get_settings()
    try:
        from qdrant_client.models import Distance, VectorParams
        # get_collections é mais barato que get_collection (404)
        collections = await client.get_collections()
        names = {c.name for c in collections.collections}
        if settings.qdrant_collection not in names:
            await client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
            logger.info(f"Qdrant collection criada: {settings.qdrant_collection} dim={EMBEDDING_DIM}")
        _collection_ready = True
        return True
    except Exception as e:
        logger.warning(
            "Falha em ensure_collection — Qdrant pode estar offline ou inacessível",
            extra={
                "event": "qdrant.collection.ensure_failed",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "embedding_dim": EMBEDDING_DIM,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return False


async def upsert_chunks(chunks: list[dict]) -> int:
    """Insere/atualiza N pontos no Qdrant. Retorna quantidade inserida.

    Cada chunk dict deve ter: id, embedding (list[float]), source_id, ordinal.
    """
    if not chunks:
        return 0
    settings = get_settings()
    if not await ensure_collection():
        # ensure_collection já logou a causa raiz com contexto.
        # Loga aqui também porque ingest.py precisa correlacionar
        # "upsert abortado" com "Ingestão parcial" no mesmo evento.
        source_ids = list({c.get("source_id") for c in chunks if c.get("source_id")})
        logger.warning(
            "upsert_chunks abortado: collection indisponível",
            extra={
                "event": "qdrant.upsert.aborted_no_collection",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "chunk_count": len(chunks),
                "source_ids": source_ids,
            },
        )
        return 0
    client = await get_client()
    try:
        from qdrant_client.models import PointStruct
        points = [
            PointStruct(
                id=c["id"],
                vector=c["embedding"],
                payload={
                    "knowledge_source_id": c["source_id"],
                    "ordinal": c["ordinal"],
                    "chunk_id": c["id"],
                },
            )
            for c in chunks
        ]
        await client.upsert(collection_name=settings.qdrant_collection, points=points, wait=True)
        return len(points)
    except Exception as e:
        source_ids = list({c.get("source_id") for c in chunks if c.get("source_id")})
        logger.warning(
            "upsert_chunks falhou — chunks ficaram só no Postgres",
            extra={
                "event": "qdrant.upsert.failed",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "chunk_count": len(chunks),
                "embedding_dim": len(chunks[0].get("embedding") or []) if chunks else None,
                "source_ids": source_ids,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return 0


async def search(query_vector: list[float], top_n: int = 20, source_ids: Optional[list[str]] = None) -> list[dict]:
    """Busca top_n vizinhos. Filtra por source_ids se fornecido.

    Retorna lista de dicts: [{"chunk_id": ..., "source_id": ..., "ordinal": ..., "score": ...}, ...].
    Lista vazia se Qdrant offline — caller cai em BM25-only.
    """
    if not await ensure_collection():
        return []
    client = await get_client()
    settings = get_settings()
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchAny
        qfilter = None
        if source_ids:
            qfilter = Filter(
                must=[FieldCondition(key="knowledge_source_id", match=MatchAny(any=source_ids))]
            )
        # query_points é o método novo (substitui search). Funciona em qdrant-client >= 1.10.
        result = await client.query_points(
            collection_name=settings.qdrant_collection,
            query=query_vector,
            limit=top_n,
            query_filter=qfilter,
            with_payload=True,
        )
        return [
            {
                "chunk_id": p.payload.get("chunk_id") or str(p.id),
                "source_id": p.payload.get("knowledge_source_id"),
                "ordinal": p.payload.get("ordinal"),
                "score": p.score,
            }
            for p in result.points
        ]
    except Exception as e:
        logger.warning(
            "qdrant search falhou — caller cai em BM25-only",
            extra={
                "event": "qdrant.search.failed",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "top_n": top_n,
                "source_filter_count": len(source_ids) if source_ids else 0,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return []


async def delete_by_source(source_id: str) -> bool:
    """Remove todos os pontos de uma source. Usado em re-ingestão (replace=True)."""
    if not await ensure_collection():
        return False
    client = await get_client()
    settings = get_settings()
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
        flt = Filter(must=[FieldCondition(key="knowledge_source_id", match=MatchValue(value=source_id))])
        await client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=FilterSelector(filter=flt),
            wait=True,
        )
        return True
    except Exception as e:
        logger.warning(
            "delete_by_source falhou — pontos antigos podem persistir até próximo replace/reindex",
            extra={
                "event": "qdrant.delete.failed",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "source_id": source_id,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return False


async def collection_info() -> Optional[dict]:
    """Diagnóstico: retorna info da collection (ou None se offline). Útil para healthcheck."""
    if not await ensure_collection():
        return None
    client = await get_client()
    settings = get_settings()
    try:
        info = await client.get_collection(settings.qdrant_collection)
        # Qdrant 1.17+ removeu `vectors_count`; só points_count e status restaram.
        return {
            "name": settings.qdrant_collection,
            "points_count": getattr(info, "points_count", None),
            "status": str(getattr(info, "status", "unknown")),
        }
    except Exception as e:
        logger.warning(
            "collection_info falhou",
            extra={
                "event": "qdrant.collection_info.failed",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return None
