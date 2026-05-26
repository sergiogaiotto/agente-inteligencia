"""Wrapper async do Qdrant (Onda 3 — RAG real).

Encapsula AsyncQdrantClient com:
- Singleton lazy (cria cliente na primeira chamada).
- ensure_collection() idempotente — cria a collection no primeiro uso.
- Operações CRUD (upsert, search, delete_by_source).
- Graceful degradation: se Qdrant offline, todos os métodos retornam vazio/False
  com warning no log; nunca propagam exception para quebrar a API.

Convenções:
- 1 collection global (`agente_evidence`), distance=Cosine.
- Dimensão derivada do provider de embedding ATIVO (settings.embedding_provider):
    - "qwen3" → settings.qwen3_dimensions ou 1024 (default Qwen3-Embedding-0.6B)
    - "azure" (ou outro) → 1536 (text-embedding-3-small)
  Trocar provider via UI sem chamar recreate_collection() resulta em drift:
  novos vetores não casam com a collection antiga. ensure_collection() detecta
  e loga; chamador deve invocar recreate_collection() (ver POST /api/v1/evidence/reindex).
- Ponto = chunk. Payload mínimo: {"knowledge_source_id", "ordinal", "chunk_id"}.
  O texto NÃO vai no payload — fica só no Postgres (evita duplicidade e custo).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def get_active_embedding_dim() -> int:
    """Retorna a dimensão do embedder ATIVO conforme settings.

    Não faz HTTP — infere da config. Provider Qwen3 com `qwen3_dimensions=0`
    cai no default do modelo (1024 para Qwen3-Embedding-0.6B). Provider Azure
    (text-embedding-3-small) é fixo em 1536.

    Mudanças no settings só refletem aqui se settings forem recarregados
    (get_settings() faz cache). Em ambiente de produção com mudança via UI,
    o handler de settings deve invalidar o cache + recreate_collection().
    """
    settings = get_settings()
    provider = (getattr(settings, "embedding_provider", "azure") or "azure").lower()
    if provider == "qwen3":
        configured = int(getattr(settings, "qwen3_dimensions", 0) or 0)
        return configured or 1024  # default Qwen3-Embedding-0.6B
    # Azure text-embedding-3-small (e qualquer provider desconhecido cai aqui).
    return 1536


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
    """Cria a collection se não existir. Idempotente. Retorna True se pronta.

    Valida que a dimensão da collection bate com o embedder ativo
    (get_active_embedding_dim). Se houver drift (provider trocado via UI sem
    recreate), loga ERROR específico e retorna False — chamador (upsert/search)
    cai em fallback e UI mostra "Qdrant divergente". O fix é chamar
    recreate_collection() (via POST /api/v1/evidence/reindex).
    """
    global _collection_ready
    if _collection_ready:
        return True
    client = await get_client()
    if client is None:
        return False
    settings = get_settings()
    expected_dim = get_active_embedding_dim()
    try:
        from qdrant_client.models import Distance, VectorParams
        # get_collections é mais barato que get_collection (404)
        collections = await client.get_collections()
        names = {c.name for c in collections.collections}
        if settings.qdrant_collection not in names:
            await client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=VectorParams(size=expected_dim, distance=Distance.COSINE),
            )
            logger.info(
                "Qdrant collection criada",
                extra={
                    "event": "qdrant.collection.created",
                    "collection": settings.qdrant_collection,
                    "embedding_dim": expected_dim,
                    "distance": "Cosine",
                },
            )
        else:
            # Collection já existe — validar dim. Se divergente, alertar e
            # retornar False (caller cai em fallback). NÃO recriamos automático
            # porque isso DELETA TODOS OS VETORES — operação destrutiva tem
            # que ser explícita via recreate_collection() / endpoint /reindex.
            info = await client.get_collection(settings.qdrant_collection)
            actual_dim = _extract_collection_dim(info)
            if actual_dim is not None and actual_dim != expected_dim:
                logger.error(
                    "Qdrant collection com dimensão divergente do embedder ativo — "
                    "chame recreate_collection() (POST /api/v1/evidence/reindex)",
                    extra={
                        "event": "qdrant.collection.dim_mismatch",
                        "collection": settings.qdrant_collection,
                        "dim_actual": actual_dim,
                        "dim_expected": expected_dim,
                        "embedding_provider": (settings.embedding_provider or "azure"),
                        "hint": "POST /api/v1/evidence/reindex {\"recreate_collection\": true}",
                    },
                )
                return False
        _collection_ready = True
        return True
    except Exception as e:
        logger.warning(
            "Falha em ensure_collection — Qdrant pode estar offline ou inacessível",
            extra={
                "event": "qdrant.collection.ensure_failed",
                "qdrant_url": settings.qdrant_url,
                "collection": settings.qdrant_collection,
                "embedding_dim_expected": expected_dim,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return False


def _extract_collection_dim(info) -> Optional[int]:
    """Extrai a dim da collection de um CollectionInfo do qdrant-client.

    Lida com 2 shapes do `vectors_config`:
      - VectorParams direto (collection com 1 vetor unnamed) → .size
      - dict[str, VectorParams] (collection com vetores nomeados) → primeiro valor

    Retorna None se não conseguir extrair (Qdrant versão diferente / shape inesperado).
    """
    try:
        cfg = info.config.params.vectors
        # Shape 1: VectorParams direto
        size = getattr(cfg, "size", None)
        if isinstance(size, int):
            return size
        # Shape 2: dict de VectorParams nomeados
        if isinstance(cfg, dict) and cfg:
            first = next(iter(cfg.values()))
            return getattr(first, "size", None)
    except Exception:
        pass
    return None


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
    """Diagnóstico: retorna info da collection (ou None se Qdrant offline).

    NÃO passa por ensure_collection() porque a função é diagnóstica — precisa
    funcionar MESMO quando a collection está com dim divergente (que faria
    ensure_collection() retornar False). UI usa para mostrar o drift e oferecer
    botão de reindex.

    Retorno:
        {
          "name": str,
          "points_count": int | None,
          "status": str,
          "dim_actual": int | None,           # dim da collection atual no Qdrant
          "dim_expected": int,                # dim do provider de embedding ativo
          "dim_match": bool,                  # True se actual == expected (saudável)
          "exists": bool,                     # False se collection foi dropada
        }
        ou None se Qdrant offline.
    """
    client = await get_client()
    if client is None:
        return None
    settings = get_settings()
    expected_dim = get_active_embedding_dim()
    try:
        # Tenta achar a collection sem erro 404.
        collections = await client.get_collections()
        names = {c.name for c in collections.collections}
        if settings.qdrant_collection not in names:
            return {
                "name": settings.qdrant_collection,
                "points_count": 0,
                "status": "missing",
                "dim_actual": None,
                "dim_expected": expected_dim,
                "dim_match": False,
                "exists": False,
            }
        info = await client.get_collection(settings.qdrant_collection)
        actual_dim = _extract_collection_dim(info)
        return {
            "name": settings.qdrant_collection,
            "points_count": getattr(info, "points_count", None),
            "status": str(getattr(info, "status", "unknown")),
            "dim_actual": actual_dim,
            "dim_expected": expected_dim,
            "dim_match": (actual_dim == expected_dim) if actual_dim is not None else False,
            "exists": True,
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


async def recreate_collection() -> dict:
    """Dropa e recria a collection com a dim ATUAL do embedder ativo.

    Operação DESTRUTIVA: todos os vetores são apagados. Para reindexar a partir
    dos chunks do Postgres, use `app.evidence.ingest.reindex_all()` que chama
    esta função e depois re-embarca.

    Retorno (sucesso):
        {
          "ok": True,
          "collection": str,
          "dim_before": int | None,    # None se collection não existia
          "dim_after": int,            # nova dim (== get_active_embedding_dim())
          "distance": "Cosine",
          "points_deleted": int | None,  # quantos pontos foram apagados (None se desconhecido)
        }

    Retorno (falha):
        {"ok": False, "error_type": str, "error_message": str}
    """
    global _collection_ready
    client = await get_client()
    if client is None:
        return {"ok": False, "error_type": "QdrantUnavailable",
                "error_message": "Qdrant client não inicializou (offline ou import falhou)"}
    settings = get_settings()
    expected_dim = get_active_embedding_dim()
    # Coleta info antes de dropar (pra report).
    dim_before: Optional[int] = None
    points_before: Optional[int] = None
    existed = False
    try:
        collections = await client.get_collections()
        existed = settings.qdrant_collection in {c.name for c in collections.collections}
        if existed:
            info = await client.get_collection(settings.qdrant_collection)
            dim_before = _extract_collection_dim(info)
            points_before = getattr(info, "points_count", None)
    except Exception as e:
        # Não-fatal — segue tentando recreate mesmo sem info prévia.
        logger.warning(
            "recreate_collection: falha ao coletar info prévia (continuando)",
            extra={
                "event": "qdrant.recreate.precheck_failed",
                "error_type": type(e).__name__,
            },
        )

    try:
        from qdrant_client.models import Distance, VectorParams
        if existed:
            await client.delete_collection(collection_name=settings.qdrant_collection)
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(size=expected_dim, distance=Distance.COSINE),
        )
        # Invalida cache pra próximo upsert/search re-validar.
        _collection_ready = False
        logger.info(
            "Qdrant collection recriada",
            extra={
                "event": "qdrant.collection.recreated",
                "collection": settings.qdrant_collection,
                "dim_before": dim_before,
                "dim_after": expected_dim,
                "points_deleted": points_before,
                "existed": existed,
            },
        )
        return {
            "ok": True,
            "collection": settings.qdrant_collection,
            "dim_before": dim_before,
            "dim_after": expected_dim,
            "distance": "Cosine",
            "points_deleted": points_before,
        }
    except Exception as e:
        logger.error(
            "recreate_collection falhou",
            extra={
                "event": "qdrant.collection.recreate_failed",
                "collection": settings.qdrant_collection,
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
