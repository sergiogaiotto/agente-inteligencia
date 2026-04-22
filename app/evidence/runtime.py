"""Runtime de Evidência — §14.

Retriever (busca híbrida), Reranker, Evidence Checker.
Princípio: toda recomendação ancorada em evidência de bases autorizadas.
"""

import uuid
import json
import logging
from dataclasses import dataclass, field
from app.core.database import knowledge_repo, evidences_repo
from app.core.llm_providers import get_provider

logger = logging.getLogger(__name__)


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
    issues: list = field(default_factory=list)    # inconsistency, conflict, coverage_gap
    risk_high: bool = False
    fraud_suspected: bool = False


class Retriever:
    """Busca híbrida em bases autorizadas de conhecimento (§14.1).

    Implementação simplificada com busca textual em SQLite.
    Em produção: BM25 + busca vetorial no Vector DB.
    """

    async def search(self, query: str, skill_evidence_policy: dict = None, top_n: int = 5) -> list[EvidenceResult]:
        """Busca evidências nas bases autorizadas."""
        # Busca em todas as bases autorizadas
        sources = await knowledge_repo.find_all(authorized=1, limit=50)
        if not sources:
            return []

        results = []
        keywords = query.lower().split()

        for source in sources:
            # Busca textual simplificada (em produção: BM25 + vetorial)
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

        # Ordena por relevância
        results.sort(key=lambda e: e.relevance_score, reverse=True)
        return results[:top_n]


class Reranker:
    """Reranker cross-encoder (§14.1).

    Em produção: modelo cross-encoder dedicado.
    Implementação simplificada: reordena por score de relevância.
    """

    async def rerank(self, query: str, evidences: list[EvidenceResult], top_n: int = 5) -> list[EvidenceResult]:
        # Boost para evidências com termos exatos da query
        for ev in evidences:
            query_terms = set(query.lower().split())
            snippet_terms = set(ev.snippet_text.lower().split())
            overlap = len(query_terms & snippet_terms)
            ev.relevance_score = min(ev.relevance_score + (overlap * 0.1), 1.0)

        evidences.sort(key=lambda e: e.relevance_score, reverse=True)
        return evidences[:top_n]


class EvidenceChecker:
    """Verificador de Evidência independente (§14.2).

    Verifica consistência, regras de negócio, conflitos e cobertura.
    Implementado como LLM separado ou regras determinísticas.
    """

    def __init__(self, provider_name: str = "openai"):
        self.provider = get_provider(provider_name)

    async def verify(self, draft: str, evidences: list[EvidenceResult], skill_guardrails: str = "") -> VerificationResult:
        """Verifica rascunho contra evidências."""
        if not evidences:
            return VerificationResult(
                ok=False,
                confidence=0.0,
                issues=["Nenhuma evidência disponível para verificação"],
            )

        # Verificação via LLM
        evidence_text = "\n".join(
            f"[E{i+1}] (score={e.relevance_score:.2f}, fonte={e.source_name}): {e.snippet_text}"
            for i, e in enumerate(evidences)
        )

        verification_prompt = f"""Você é um verificador de evidência independente. Analise o rascunho abaixo contra as evidências fornecidas.

RASCUNHO:
{draft}

EVIDÊNCIAS:
{evidence_text}

GUARDRAILS:
{skill_guardrails or 'Nenhum guardrail específico.'}

Avalie:
1. CONSISTÊNCIA: O rascunho é semanticamente consistente com as evidências? Há contradições?
2. COBERTURA: Todas as afirmações do rascunho são cobertas por evidências?
3. CONFLITO: Evidências são mutuamente contraditórias?
4. RISCO: Há indicativo de risco alto ou fraude?

Responda em JSON:
{{"ok": true/false, "confidence": 0.0-1.0, "issues": ["lista de problemas"], "risk_high": false, "fraud_suspected": false}}
"""

        try:
            response = await self.provider.generate([
                {"role": "system", "content": "Verificador de evidência. Responda apenas em JSON válido."},
                {"role": "user", "content": verification_prompt},
            ])
            content = response.get("content", "")
            # Extrair JSON da resposta
            json_match = content
            if "```" in content:
                import re
                m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
                if m:
                    json_match = m.group(1)

            data = json.loads(json_match.strip())
            return VerificationResult(**data)
        except Exception as e:
            logger.warning(f"Evidence checker falhou, usando heurística: {e}")
            # Fallback: heurística baseada em scores
            avg_score = sum(e.relevance_score for e in evidences) / len(evidences)
            return VerificationResult(
                ok=avg_score >= 0.3,
                confidence=avg_score,
                issues=[] if avg_score >= 0.3 else ["Evidência com score de relevância insuficiente"],
            )

    async def persist_evidences(self, evidences: list[EvidenceResult], turn_id: str):
        """Persiste evidências vinculadas ao turno."""
        for ev in evidences:
            await evidences_repo.create({
                "id": ev.evidence_id,
                "snippet_id": ev.evidence_id[:8],
                "snippet_text": ev.snippet_text,
                "relevance_score": ev.relevance_score,
                "confidentiality_label": ev.confidentiality,
                "knowledge_source_id": ev.source_id,
                "turn_id": turn_id,
            })


# Instâncias singleton
retriever = Retriever()
reranker = Reranker()
evidence_checker = EvidenceChecker()