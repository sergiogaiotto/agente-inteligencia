"""Verifier — orquestra ContractValidator + MultiDimJudge + persiste em `verifications`.

Substitui o EvidenceChecker monolítico anterior. Agora:
- Roda ContractValidator (Python puro, sem LLM) ANTES → falha precoce em formato
- Roda MultiDimJudge (LLM com rubrica multi-dimensional) quando faz sentido por profile
- Agrega resultado de forma transparente: ok = min(scores) >= threshold AND safety AND contract
- Persiste em tabela `verifications` para query analítica

Toggle: VERIFIER_V2_ENABLED=False (default) → fallback para _LegacyVerifier (comportamento Onda 0).
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.config import get_settings
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


# ───────────────────────────────────────────────────────────────
# Resultado (compat com EvidenceChecker.verify)
# ───────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    """Resultado da verificação. Mantém campos legacy + novos."""
    # Legacy (EvidenceChecker original) — preservados para back-compat
    ok: bool = False
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    risk_high: bool = False
    fraud_suspected: bool = False
    # Novos (Verifier v2)
    dimensions: dict[str, dict] = field(default_factory=dict)
    """{factuality: {score, reason}, completeness: {...}, tone_adherence: {...}, safety: {...}}"""
    unsupported_claims: list[str] = field(default_factory=list)
    contract_compliant: bool = True
    contract_errors: list[str] = field(default_factory=list)
    judge_model: str = ""
    duration_ms: int = 0


# ───────────────────────────────────────────────────────────────
# Verifier v2 (orchestrator)
# ───────────────────────────────────────────────────────────────

class Verifier:
    """Orquestra ContractValidator + MultiDimJudge.

    Backward compat:
        Aceita assinatura legacy `verify(draft, evidences, skill_guardrails)` —
        nesse caso, faz wrap interno para a nova assinatura.
    """

    async def verify(
        self,
        draft: str,
        evidences: Optional[list] = None,
        # Aliases legacy
        skill_guardrails: str = "",
        # Novos
        output_contract: Optional[str] = None,
        guardrails: Optional[str] = None,
        user_question: str = "",
        profile: str = "standard",
        turn_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
        persist: bool = True,
    ) -> VerificationResult:
        """Verifica um draft.

        Se VERIFIER_V2_ENABLED=False → cai em _LegacyVerifier (comportamento Onda 0).
        Caso contrário, orquestra ContractValidator + MultiDimJudge.
        """
        settings = get_settings()
        evidences = evidences or []
        # Compat: skill_guardrails (legacy) → guardrails (novo)
        gr = guardrails if guardrails is not None else skill_guardrails

        with _tracer.start_as_current_span("verifier.evaluate") as span:
            span.set_attribute("verifier.profile", profile)
            span.set_attribute("verifier.evidences_count", len(evidences))
            span.set_attribute("verifier.has_contract", bool(output_contract))

            if not settings.verifier_v2_enabled:
                span.set_attribute("verifier.path", "legacy")
                return await _LegacyVerifier().verify(draft, evidences, gr)

            span.set_attribute("verifier.path", "v2")
            started = time.perf_counter()

            # ─── 1. ContractValidator (síncrono, sem LLM) ────────
            contract_compliant = True
            contract_errors: list[str] = []
            if output_contract:
                from app.verifier.contract_validator import validate_contract
                cr = validate_contract(draft, output_contract)
                contract_compliant = cr.compliant
                contract_errors = cr.errors
                span.set_attribute("verifier.contract_compliant", contract_compliant)

            # ─── 2. MultiDimJudge (LLM) ─────────────────────────
            # Profile fast: pula judge (só contract). Standard/rigorous: roda judge.
            run_judge = profile != "fast"
            dimensions: dict[str, dict] = {}
            unsupported_claims: list[str] = []
            judge_model = ""

            if run_judge:
                from app.verifier.multi_dim_judge import MultiDimJudge
                judge = MultiDimJudge()
                try:
                    j = await judge.evaluate(
                        draft=draft,
                        evidences=evidences,
                        user_question=user_question,
                        output_contract=output_contract or "",
                        guardrails=gr,
                    )
                    dimensions = j.get("dimensions", {})
                    unsupported_claims = j.get("unsupported_claims", []) or []
                    judge_model = j.get("model", "")
                    span.set_attribute("verifier.judge_ok", True)
                except Exception as e:
                    logger.warning(f"MultiDimJudge falhou: {type(e).__name__}: {e}")
                    span.set_attribute("verifier.judge_ok", False)
                    span.set_attribute("verifier.judge_error", str(e)[:200])

            # ─── 3. Agregação ────────────────────────────────────
            scores = self._extract_scores(dimensions)
            confidence = self._compute_confidence(scores)
            ok = self._compute_ok(scores, contract_compliant, settings)

            duration_ms = int((time.perf_counter() - started) * 1000)
            span.set_attribute("verifier.ok", ok)
            span.set_attribute("verifier.confidence", round(confidence, 3))
            span.set_attribute("verifier.duration_ms", duration_ms)

            issues: list[str] = []
            if not contract_compliant:
                issues.extend([f"contract: {e}" for e in contract_errors[:3]])
            for dim_name, threshold in (
                ("factuality", settings.verifier_factuality_threshold),
                ("completeness", settings.verifier_completeness_threshold),
                ("tone_adherence", settings.verifier_tone_threshold),
            ):
                d = dimensions.get(dim_name) or {}
                s = d.get("score")
                if isinstance(s, (int, float)) and s < threshold:
                    issues.append(f"{dim_name}={s:.1f} < {threshold} ({d.get('reason','')})")
            if unsupported_claims:
                issues.append(f"unsupported_claims: {len(unsupported_claims)}")

            safety_dim = dimensions.get("safety") or {}
            safety_score = safety_dim.get("score")
            risk_high = bool(safety_score == 0)

            result = VerificationResult(
                ok=ok,
                confidence=confidence,
                issues=issues,
                risk_high=risk_high,
                fraud_suspected=False,  # Reservado para futuro
                dimensions=dimensions,
                unsupported_claims=unsupported_claims,
                contract_compliant=contract_compliant,
                contract_errors=contract_errors,
                judge_model=judge_model,
                duration_ms=duration_ms,
            )

            # ─── 4. Persistência ────────────────────────────────
            if persist:
                try:
                    await self._persist(result, turn_id, interaction_id, profile)
                except Exception as e:
                    logger.warning(f"verifier persist falhou: {e}")

            return result

    @staticmethod
    def _extract_scores(dimensions: dict) -> dict[str, Optional[float]]:
        out = {}
        for k in ("factuality", "completeness", "tone_adherence", "safety"):
            d = dimensions.get(k) or {}
            s = d.get("score")
            out[k] = float(s) if isinstance(s, (int, float)) else None
        return out

    @staticmethod
    def _compute_confidence(scores: dict) -> float:
        """Confidence agregado em [0, 1]: média de factuality+completeness+tone normalizada por 5."""
        scored = [s for k, s in scores.items() if s is not None and k != "safety"]
        if not scored:
            return 0.0
        return min(1.0, sum(scored) / (len(scored) * 5))

    @staticmethod
    def _compute_ok(scores: dict, contract_compliant: bool, settings) -> bool:
        """ok = min(scores) >= threshold AND safety==1 AND contract_compliant."""
        if not contract_compliant:
            return False
        # safety: se evaluado, precisa ser 1
        safety = scores.get("safety")
        if safety is not None and safety < 1:
            return False
        # Demais dimensões: se evaluadas, precisam passar threshold
        thresholds = {
            "factuality": settings.verifier_factuality_threshold,
            "completeness": settings.verifier_completeness_threshold,
            "tone_adherence": settings.verifier_tone_threshold,
        }
        for k, th in thresholds.items():
            s = scores.get(k)
            if s is not None and s < th:
                return False
        # Se nenhuma dimensão foi avaliada (judge não rodou), ok depende só do contract.
        return True

    async def persist_evidences(self, evidences, turn_id):
        """Compat: método legacy para persistir evidências em `evidences` table.
        Mantido porque pode ter callers externos (scripts, testes). Não chamado
        em runtime do engine atual.
        """
        from app.core.database import evidences_repo
        for ev in evidences:
            await evidences_repo.create({
                "id": str(uuid.uuid4()),
                "snippet_id": getattr(ev, "evidence_id", "")[:8],
                "snippet_text": getattr(ev, "snippet_text", ""),
                "relevance_score": getattr(ev, "relevance_score", 0),
                "confidentiality_label": getattr(ev, "confidentiality", "internal"),
                "knowledge_source_id": getattr(ev, "source_id", ""),
                "turn_id": turn_id,
            })

    async def _persist(self, result: VerificationResult, turn_id, interaction_id, profile: str):
        """Insere em `verifications`. Best-effort — falha não derruba o pipeline."""
        from app.core.database import _get_pool
        pool = _get_pool()
        rid = str(uuid.uuid4())
        d = result.dimensions or {}

        def _g(name: str, key: str) -> Any:
            return (d.get(name) or {}).get(key)

        async with pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO verifications
                  (id, turn_id, interaction_id,
                   factuality_score, factuality_reason,
                   completeness_score, completeness_reason,
                   tone_score, tone_reason,
                   safety_score, safety_reason,
                   contract_compliant, contract_errors,
                   ok, confidence, unsupported_claims,
                   judge_model, profile, duration_ms)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                """,
                rid, turn_id, interaction_id,
                _g("factuality", "score"), _g("factuality", "reason"),
                _g("completeness", "score"), _g("completeness", "reason"),
                _g("tone_adherence", "score"), _g("tone_adherence", "reason"),
                _g("safety", "score"), _g("safety", "reason"),
                result.contract_compliant, json.dumps(result.contract_errors)[:8000],
                result.ok, result.confidence, json.dumps(result.unsupported_claims)[:8000],
                result.judge_model, profile, result.duration_ms,
            )


# ───────────────────────────────────────────────────────────────
# Legacy (Onda 0 — comportamento original do EvidenceChecker)
# ───────────────────────────────────────────────────────────────

class _LegacyVerifier:
    """Comportamento original do EvidenceChecker. Mantido para back-compat
    quando VERIFIER_V2_ENABLED=False.

    Migrei a lógica do app/evidence/runtime.py:EvidenceChecker para cá. O
    código original lá será re-exportado deste módulo.
    """

    async def verify(self, draft: str, evidences: list, skill_guardrails: str = "") -> VerificationResult:
        if not evidences:
            return VerificationResult(
                ok=False,
                confidence=0.0,
                issues=["Nenhuma evidência disponível para verificação"],
            )

        evidence_text = "\n".join(
            f"[E{i+1}] (score={getattr(e, 'relevance_score', 0):.2f}, fonte={getattr(e, 'source_name', '?')}): "
            f"{getattr(e, 'snippet_text', str(e))}"
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
            from app.core.llm_providers import get_provider
            provider = get_provider("openai")
            response = await provider.generate([
                {"role": "system", "content": "Verificador de evidência. Responda apenas em JSON válido."},
                {"role": "user", "content": verification_prompt},
            ])
            content = response.get("content", "")
            json_match = content
            if "```" in content:
                m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
                if m:
                    json_match = m.group(1)
            data = json.loads(json_match.strip())
            return VerificationResult(**{k: v for k, v in data.items() if k in {"ok", "confidence", "issues", "risk_high", "fraud_suspected"}})
        except Exception as e:
            logger.warning(f"_LegacyVerifier falhou, usando heurística: {e}")
            avg_score = sum(getattr(e_, "relevance_score", 0) for e_ in evidences) / len(evidences)
            return VerificationResult(
                ok=avg_score >= 0.3,
                confidence=avg_score,
                issues=[] if avg_score >= 0.3 else ["Evidência com score de relevância insuficiente"],
            )

    # Compat: persist_evidences ainda existe no path do engine (não está sendo chamado, mas back-compat)
    async def persist_evidences(self, evidences, turn_id):
        from app.core.database import evidences_repo
        for ev in evidences:
            await evidences_repo.create({
                "id": str(uuid.uuid4()),
                "snippet_id": getattr(ev, "evidence_id", "")[:8],
                "snippet_text": getattr(ev, "snippet_text", ""),
                "relevance_score": getattr(ev, "relevance_score", 0),
                "confidentiality_label": getattr(ev, "confidentiality", "internal"),
                "knowledge_source_id": getattr(ev, "source_id", ""),
                "turn_id": turn_id,
            })


# Singleton — engine.py importa este nome
verifier = Verifier()
