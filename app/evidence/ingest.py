"""Pipeline de ingestão (Onda 3).

Recebe texto cru, chunca, embeda, persiste em Postgres + Qdrant.
Idempotente quando `replace=True` (default): apaga chunks anteriores antes
de inserir os novos.

Falhas tratadas com semantica clara:
- source não existe → 404
- Azure embeddings indisponível → 503 com mensagem específica
- Qdrant offline mas Postgres OK → retorna `partial=true`; usuário pode rodar
  /reindex depois quando Qdrant voltar
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from app.core.database import _get_pool, evidence_chunks_repo, knowledge_repo
from app.core.otel import get_tracer
from app.evidence.chunker import chunk_text
from app.evidence.embedder import embed_texts
from app.evidence.qdrant_store import upsert_chunks as qdrant_upsert, delete_by_source as qdrant_delete

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


class IngestError(Exception):
    """Erro de ingestão com status code HTTP recomendado."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


async def ingest_text(source_id: str, text: str, replace: bool = True) -> dict:
    """Ingere `text` na knowledge_source `source_id`.

    Args:
        source_id: id da knowledge_source destino. Deve existir.
        text: conteúdo a indexar. Não vazio.
        replace: True (default) apaga chunks/pontos anteriores antes de inserir.

    Returns:
        {
          "source_id": ...,
          "chunks_created": N,
          "tokens_total": N,
          "qdrant_upserted": N,    # 0 se Qdrant offline
          "duration_ms": N,
          "partial": bool,         # True se Qdrant falhou; só Postgres tem dados
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

        # 1. Chunca
        chunks = chunk_text(text)
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
            raise IngestError(
                "Azure OpenAI embeddings indisponível. Verifique AZURE_OPENAI_API_KEY/ENDPOINT.",
                status_code=503,
            )
        if len(vectors) != len(chunks):
            raise IngestError(
                f"Embeddings devolveu {len(vectors)} vetores para {len(chunks)} chunks.",
                status_code=500,
            )

        # 3. Replace: limpa state anterior (Postgres + Qdrant)
        pool = _get_pool()
        if replace:
            with _tracer.start_as_current_span("ingest.delete_old"):
                async with pool.acquire() as con:
                    await con.execute(
                        "DELETE FROM evidence_chunks WHERE knowledge_source_id = $1",
                        source_id,
                    )
                # Qdrant: best-effort. Se offline, deleções acumulam e o próximo
                # replace ou /reindex limpa.
                await qdrant_delete(source_id)

        # 4. Insere chunks no Postgres + monta payload do Qdrant
        chunk_ids: list[str] = []
        async with pool.acquire() as con:
            async with con.transaction():
                for c in chunks:
                    cid = str(uuid.uuid4())
                    chunk_ids.append(cid)
                    await con.execute(
                        """
                        INSERT INTO evidence_chunks (id, knowledge_source_id, ordinal, text, token_count, char_count)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        cid, source_id, c.ordinal, c.text, c.token_count, c.char_count,
                    )

        # 5. Upsert no Qdrant (best-effort — Qdrant pode estar offline)
        qdrant_payload = [
            {
                "id": chunk_ids[i],
                "embedding": vectors[i],
                "source_id": source_id,
                "ordinal": chunks[i].ordinal,
            }
            for i in range(len(chunks))
        ]
        with _tracer.start_as_current_span("ingest.qdrant_upsert") as qspan:
            qdrant_n = await qdrant_upsert(qdrant_payload)
            qspan.set_attribute("qdrant.upserted", qdrant_n)

        partial = qdrant_n != len(chunks)
        if partial:
            logger.warning(
                f"Ingestão parcial: source={source_id} chunks={len(chunks)} qdrant={qdrant_n}. "
                "Postgres tem todos; Qdrant divergente. Re-execute quando Qdrant estiver OK."
            )

        # 6. Atualiza metadados da source
        await knowledge_repo.update(source_id, {
            "last_updated": datetime.now().isoformat(),
            "index_version": f"v3-{int(time.time())}",
        })

        duration_ms = int((time.time() - start) * 1000)
        result = {
            "source_id": source_id,
            "chunks_created": len(chunks),
            "tokens_total": tokens_total,
            "qdrant_upserted": qdrant_n,
            "duration_ms": duration_ms,
            "partial": partial,
        }
        logger.info(f"Ingest OK: {result}")
        return result


async def clear_source(source_id: str) -> dict:
    """Apaga todos os chunks de uma source (Postgres + Qdrant). Idempotente."""
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
        qdrant_ok = await qdrant_delete(source_id)
        return {
            "source_id": source_id,
            "postgres_deleted": pg_deleted,
            "qdrant_deleted": qdrant_ok,
        }


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

        # Pipeline padrão: chunk → embed → store
        result = await ingest_text(source_id, text, replace=replace)
        result["converter"] = "markitdown"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        result["source_format"] = ext
        result["source_filename"] = filename
        return result


async def ingest_url(
    source_id: str,
    url: str,
    replace: bool = True,
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

        result = await ingest_text(source_id, text, replace=replace)
        result["converter"] = "markitdown"
        result["source_url"] = url.strip()
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
