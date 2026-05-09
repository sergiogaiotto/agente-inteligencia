"""Runtime de Evidência — §14.

Retriever (busca híbrida BM25+vetorial), Reranker (LLM ou heurística),
Evidence Checker. Princípio: toda recomendação ancorada em evidência de
bases autorizadas.

Onda 3: substitui a busca textual ingênua original por:
- BM25 nativo (Postgres tsvector + GIN index)
- Vetorial (Qdrant + Azure embeddings)
- Reciprocal Rank Fusion (RRF) para fundir os dois rankings
- LLM-as-reranker opcional para precisão final

Backward compat:
- Mesma classe `Retriever`, mesma assinatura `search(query, skill_evidence_policy, top_n)`,
  mesmo retorno (list[EvidenceResult]).
- Engine.py NÃO precisa mudar.
- Quando `rag_v2_enabled=false` ou nenhuma source tem chunks ingeridos: cai no
  retriever legacy (busca em name+description das knowledge_sources).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.database import knowledge_repo, evidences_repo, _get_pool
from app.core.llm_providers import get_provider
from app.core.otel import get_tracer
from app.evidence.embedder import embed_query
from app.evidence.qdrant_store import search as qdrant_search

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


@dataclass
class EvidenceResult:
    evidence_id: str = ""
    snippet_text: str = ""
    relevance_score: float = 0.0
    source_name: str = ""
    source_id: str = ""
    confidentiality: str = "internal"


@dataclass
class VerificationResult:
    ok: bool = False
    confidence: float = 0.0
    issues: list = field(default_factory=list)
    risk_high: bool = False
    fraud_suspected: bool = False


# ───────────────────────────────────────────────────────────────
# Retriever
# ───────────────────────────────────────────────────────────────

class Retriever:
    """Busca híbrida em bases autorizadas (§14.1, Onda 3).

    Path principal:
        BM25 (Postgres tsvector) ┐
                                 ├── RRF ── top_n ── EvidenceResult[]
        Vetorial (Qdrant)         ┘

    Fallback (rag_v2_enabled=false OU nenhum chunk ingerido):
        Match de keyword em knowledge_sources.name + description (legacy).
    """

    async def search(
        self,
        query: str,
        skill_evidence_policy: dict | None = None,
        top_n: int = 5,
    ) -> list[EvidenceResult]:
        with _tracer.start_as_current_span("evidence.retrieve") as span:
            span.set_attribute("query.length", len(query or ""))
            span.set_attribute("retriever.top_n", top_n)

            settings = get_settings()
            if not settings.rag_v2_enabled:
                span.set_attribute("retriever.path", "legacy_disabled")
                return await self._legacy_search(query, top_n)

            # Há chunks ingeridos? Se não, fallback.
            has_chunks = await self._has_any_chunks()
            if not has_chunks:
                span.set_attribute("retriever.path", "legacy_no_chunks")
                return await self._legacy_search(query, top_n)

            span.set_attribute("retriever.path", "hybrid_v2")

            # BM25 e vetorial em paralelo
            bm25_task = asyncio.create_task(
                self._bm25_search(query, settings.rag_top_n_bm25)
            )
            vec_task = asyncio.create_task(
                self._vector_search(query, settings.rag_top_n_vector)
            )
            bm25_hits, vec_hits = await asyncio.gather(bm25_task, vec_task)

            span.set_attribute("retriever.bm25_count", len(bm25_hits))
            span.set_attribute("retriever.vector_count", len(vec_hits))

            # RRF fusion
            fused = self._rrf_fuse(bm25_hits, vec_hits, k=settings.rag_rrf_k)
            top = fused[:top_n]
            span.set_attribute("retriever.fused_count", len(top))

            # Hidrata: pega texto + metadados das sources
            return await self._hydrate(top)

    async def _has_any_chunks(self) -> bool:
        """Cheap check: existe ao menos 1 chunk em qualquer source autorizada?"""
        pool = _get_pool()
        async with pool.acquire() as con:
            r = await con.fetchval(
                "SELECT 1 FROM evidence_chunks LIMIT 1"
            )
            return r is not None

    async def _bm25_search(self, query: str, top_n: int) -> list[dict]:
        """BM25 nativo via tsvector + ts_rank_cd. plainto_tsquery converte
        a query em formato tsquery seguro (não exige sintaxe especial).
        """
        with _tracer.start_as_current_span("evidence.retrieve.bm25") as span:
            span.set_attribute("bm25.top_n", top_n)
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    """
                    SELECT ec.id AS chunk_id, ec.knowledge_source_id AS source_id,
                           ec.ordinal, ec.text,
                           ts_rank_cd(ec.tsv, plainto_tsquery('simple', $1)) AS rank
                    FROM evidence_chunks ec
                    JOIN knowledge_sources ks ON ks.id = ec.knowledge_source_id
                    WHERE ec.tsv @@ plainto_tsquery('simple', $1)
                      AND ks.authorized = 1
                    ORDER BY rank DESC
                    LIMIT $2
                    """,
                    query, top_n,
                )
                hits = [
                    {
                        "chunk_id": r["chunk_id"],
                        "source_id": r["source_id"],
                        "ordinal": r["ordinal"],
                        "text": r["text"],
                        "rank": float(r["rank"] or 0.0),
                    }
                    for r in rows
                ]
                span.set_attribute("bm25.hits", len(hits))
                return hits

    async def _vector_search(self, query: str, top_n: int) -> list[dict]:
        """Embeda a query e busca top_n vizinhos em Qdrant."""
        with _tracer.start_as_current_span("evidence.retrieve.vector") as span:
            span.set_attribute("vector.top_n", top_n)
            qvec = await embed_query(query)
            if qvec is None:
                span.set_attribute("vector.path", "embedder_unavailable")
                return []
            hits = await qdrant_search(qvec, top_n=top_n)
            span.set_attribute("vector.hits", len(hits))
            return hits

    @staticmethod
    def _rrf_fuse(bm25_hits: list[dict], vec_hits: list[dict], k: int = 60) -> list[dict]:
        """Reciprocal Rank Fusion.

        Para cada chunk_id, soma 1/(k + rank) das listas em que aparece.
        Hits do BM25 trazem `text` (Postgres); vetoriais trazem só metadados (Qdrant).
        Mantemos o que conseguimos.
        """
        fused: dict[str, dict] = {}
        for rank, h in enumerate(bm25_hits):
            cid = h["chunk_id"]
            entry = fused.setdefault(cid, {"chunk_id": cid, "source_id": h["source_id"],
                                            "ordinal": h.get("ordinal"), "text": h.get("text"),
                                            "rrf_score": 0.0})
            entry["rrf_score"] += 1.0 / (k + rank + 1)
        for rank, h in enumerate(vec_hits):
            cid = h["chunk_id"]
            entry = fused.setdefault(cid, {"chunk_id": cid, "source_id": h.get("source_id"),
                                            "ordinal": h.get("ordinal"), "text": None,
                                            "rrf_score": 0.0})
            entry["rrf_score"] += 1.0 / (k + rank + 1)
        return sorted(fused.values(), key=lambda e: e["rrf_score"], reverse=True)

    async def _hydrate(self, items: list[dict]) -> list[EvidenceResult]:
        """Para cada item, completa text (se veio só do Qdrant) + nome da source."""
        if not items:
            return []
        # Carrega chunks faltantes do Postgres (pode haver hits só-vetoriais)
        missing_ids = [it["chunk_id"] for it in items if not it.get("text")]
        text_by_id: dict[str, dict] = {}
        if missing_ids:
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, text, knowledge_source_id, ordinal FROM evidence_chunks WHERE id = ANY($1::text[])",
                    missing_ids,
                )
                text_by_id = {r["id"]: dict(r) for r in rows}

        # Carrega names das sources distintas
        source_ids = list({it["source_id"] for it in items if it.get("source_id")})
        sources_by_id: dict[str, dict] = {}
        if source_ids:
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, name, confidentiality_label FROM knowledge_sources WHERE id = ANY($1::text[])",
                    source_ids,
                )
                sources_by_id = {r["id"]: dict(r) for r in rows}

        results: list[EvidenceResult] = []
        for it in items:
            text = it.get("text") or text_by_id.get(it["chunk_id"], {}).get("text") or ""
            if not text:
                continue
            src = sources_by_id.get(it.get("source_id") or "", {})
            results.append(EvidenceResult(
                evidence_id=it["chunk_id"],
                snippet_text=text,
                relevance_score=float(it.get("rrf_score", 0.0)),
                source_name=src.get("name", ""),
                source_id=it.get("source_id") or "",
                confidentiality=src.get("confidentiality_label", "internal"),
            ))
        return results

    # ─── Fallback legacy (sem chunks ingeridos) ────────────────
    async def _legacy_search(self, query: str, top_n: int) -> list[EvidenceResult]:
        sources = await knowledge_repo.find_all(authorized=1, limit=50)
        if not sources:
            return []
        keywords = query.lower().split()
        results: list[EvidenceResult] = []
        for source in sources:
            name_lower = (source.get("name", "") + " " + source.get("description", "")).lower()
            score = sum(1 for kw in keywords if kw in name_lower) / max(len(keywords), 1)
            if score > 0:
                results.append(EvidenceResult(
                    evidence_id=str(uuid.uuid4()),
                    snippet_text=f"[{source['name']}] {source.get('description', '')}",
                    relevance_score=min(score, 1.0),
                    source_name=source["name"],
                    source_id=source["id"],
                    confidentiality=source.get("confidentiality_label", "internal"),
                ))
        results.sort(key=lambda e: e.relevance_score, reverse=True)
        return results[:top_n]


# ───────────────────────────────────────────────────────────────
# Reranker
# ───────────────────────────────────────────────────────────────

class Reranker:
    """Reranker (§14.1, Onda 3).

    Modo principal (rag_rerank_with_llm=true): pede ao LLM ordenar as evidências
    pela relevância à query, devolvendo índices + score 0..1.
    Trade-off: +500ms latência, +~$0.0005/query.

    Fallback (rag_rerank_with_llm=false ou LLM falha): heurística de overlap
    de termos da query com snippets — rápido, menos preciso.
    """

    async def rerank(self, query: str, evidences: list[EvidenceResult], top_n: int = 5) -> list[EvidenceResult]:
        with _tracer.start_as_current_span("evidence.rerank") as span:
            span.set_attribute("rerank.input_count", len(evidences))
            if not evidences:
                return []
            settings = get_settings()
            if settings.rag_rerank_with_llm:
                try:
                    reranked = await self._llm_rerank(query, evidences, top_n)
                    span.set_attribute("rerank.path", "llm")
                    span.set_attribute("rerank.output_count", len(reranked))
                    return reranked
                except Exception as e:
                    logger.warning(f"LLM rerank falhou: {e}; usando heurística")
                    span.set_attribute("rerank.path", "llm_failed_fallback")
            else:
                span.set_attribute("rerank.path", "heuristic")
            return self._heuristic_rerank(query, evidences, top_n)

    async def _llm_rerank(self, query: str, evidences: list[EvidenceResult], top_n: int) -> list[EvidenceResult]:
        # Prompt curto e direto. Output JSON list[{idx, score}].
        items_text = "\n".join(
            f"[{i}] {ev.snippet_text[:300]}"  # trunca para limitar prompt
            for i, ev in enumerate(evidences)
        )
        prompt = f"""Você é um reranker de evidências. Dada uma query e candidatos numerados,
ordene os candidatos pela relevância à query e atribua score 0..1.

QUERY: {query}

CANDIDATOS:
{items_text}

Responda APENAS com JSON no formato (sem markdown):
[{{"idx": 0, "score": 0.95}}, {{"idx": 3, "score": 0.72}}, ...]
Inclua TODOS os candidatos, ordenados do mais relevante ao menos."""

        provider = get_provider("azure")
        resp = await provider.generate([
            {"role": "system", "content": "Reranker JSON. Responda apenas JSON válido."},
            {"role": "user", "content": prompt},
        ])
        content = (resp.get("content") or "").strip()
        # Extrai JSON (LLM pode envolver em markdown apesar do prompt)
        if "```" in content:
            import re
            m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if m:
                content = m.group(1).strip()
        ranked = json.loads(content)

        # Aplica scores e ordena
        out: list[EvidenceResult] = []
        for entry in ranked:
            i = entry.get("idx")
            if not isinstance(i, int) or i < 0 or i >= len(evidences):
                continue
            ev = evidences[i]
            ev.relevance_score = float(entry.get("score", 0.0))
            out.append(ev)
        # Caso LLM tenha pulado algum, anexa no fim
        seen = {id(e) for e in out}
        for e in evidences:
            if id(e) not in seen:
                out.append(e)
        return out[:top_n]

    @staticmethod
    def _heuristic_rerank(query: str, evidences: list[EvidenceResult], top_n: int) -> list[EvidenceResult]:
        query_terms = set(query.lower().split())
        for ev in evidences:
            snippet_terms = set(ev.snippet_text.lower().split())
            overlap = len(query_terms & snippet_terms)
            ev.relevance_score = min(ev.relevance_score + (overlap * 0.05), 1.0)
        evidences.sort(key=lambda e: e.relevance_score, reverse=True)
        return evidences[:top_n]


# ───────────────────────────────────────────────────────────────
# Verifier — re-exportado de app/verifier para back-compat.
# A classe foi promovida a módulo próprio na refatoração que separou
# RAG (Retriever+Reranker) de Verification (judge layer).
# ───────────────────────────────────────────────────────────────

# `EvidenceChecker` é alias de `Verifier` (definido em app/verifier/runtime.py).
# `evidence_checker` (singleton) ainda funciona — aponta para o novo Verifier.
from app.verifier import EvidenceChecker, Verifier, evidence_checker  # noqa: F401


# Instâncias singleton (engine.py importa diretamente)
retriever = Retriever()
reranker = Reranker()
# `evidence_checker` vem re-exportado de app/verifier — não instanciamos aqui.
