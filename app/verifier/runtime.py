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
    # Custo REAL do juiz (instrumentação de TCO): tokens + USD da chamada do
    # LLM-as-Judge. 0 quando o juiz não rodou (profile fast) ou o modelo é
    # self-hosted (preço 0). Torna a linha "Juiz/verificador" do TCO MEDIDA.
    judge_tokens: int = 0
    judge_cost_usd: float = 0.0
    # Q5 anti-auto-preferência (33.9.0): modelo que gerou o draft + flag de que o
    # MESMO modelo gerou e julgou (o juiz pode se favorecer). Torna o viés visível.
    generator_model: str = ""
    self_judged: bool = False
    # Wave Contract Retry (PR atual)
    contract_retried: bool = False
    """True quando o Verifier detectou compliant=false e re-chamou o LLM
    com instrução de correção. False = sem retry (1ª chamada passou OU
    setting desabilitado OU sem llm_provider disponível)."""
    contract_original_errors: list[str] = field(default_factory=list)
    """Erros do PRIMEIRO attempt (antes do retry). contract_errors fica
    com erros do attempt FINAL (segundo, se retry aconteceu). Pra auditoria
    e debugging — operador vê se a retry corrigiu ou não."""
    contract_retry_draft: str = ""
    """Draft corrigido pelo LLM no retry. Preservado pra audit (se retry
    falhou, operador precisa ver o que LLM produziu na 2ª tentativa).
    Vazio quando contract_retried=False."""


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
        # Wave Contract Retry (PR atual): caller passa o provider+model
        # usados pra gerar o draft, pra que Verifier possa re-chamar SE
        # ContractValidator falhar. None → retry desabilitado (mesmo provider
        # antigo continua funcionando, apenas perde o ganho do retry).
        llm_provider_name: Optional[str] = None,
        llm_model: Optional[str] = None,
        # Auditoria por agente/pipeline (24.10.0): dono do julgamento.
        # Persistidos em verifications p/ o drill-down da página Qualidade
        # (agregação por agente/pipeline sem JOIN frágil).
        agent_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
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
            contract_retried = False
            contract_original_errors: list[str] = []
            contract_retry_draft = ""
            retry_tokens = 0        # FIN-2 (33.8.0): custo da 2ª geração (contract retry)
            retry_cost_usd = 0.0
            if output_contract:
                from app.verifier.contract_validator import validate_contract
                cr = validate_contract(draft, output_contract)
                contract_compliant = cr.compliant
                contract_errors = cr.errors
                span.set_attribute("verifier.contract_compliant", contract_compliant)

                # Wave Contract Retry: 1ª tentativa violou → tenta de novo
                # com o LLM (instrução de correção). Acontece só se:
                # - setting habilitado (default True)
                # - caller passou llm_provider_name (pra reconstruir o provider)
                # - tem erros específicos pra mostrar pro LLM (não vazio)
                if (
                    not contract_compliant
                    and settings.verifier_contract_retry_enabled
                    and llm_provider_name
                    and contract_errors
                ):
                    contract_original_errors = list(contract_errors)
                    logger.info(
                        "verifier: contract failed — iniciando retry com LLM",
                        extra={
                            "event": "verifier.contract.retry_initiated",
                            "first_attempt_errors": contract_errors[:3],
                            "llm_provider": llm_provider_name,
                            "llm_model": llm_model or "(default)",
                        },
                    )
                    span.add_event("verifier.contract.retry_initiated")
                    try:
                        new_draft, _retry_usage = await self._retry_contract_with_llm(
                            original_draft=draft,
                            errors=contract_errors,
                            output_contract=output_contract,
                            user_question=user_question,
                            llm_provider_name=llm_provider_name,
                            llm_model=llm_model,
                            max_tokens=settings.verifier_contract_retry_max_tokens,
                        )
                        # FIN-2 (33.8.0): custo da 2ª geração do draft (antes o
                        # usage era descartado) — somado ao custo do verifier abaixo.
                        _rin = int(_retry_usage.get("prompt_tokens") or _retry_usage.get("input_tokens") or 0)
                        _rout = int(_retry_usage.get("completion_tokens") or _retry_usage.get("output_tokens") or 0)
                        retry_tokens = _rin + _rout
                        from app.core.llm_pricing import compute_cost
                        retry_cost_usd = compute_cost(llm_provider_name, llm_model, _rin, _rout)
                    except Exception as e:
                        # Retry falhou (rede/LLM erro) — segue com result original.
                        logger.warning(
                            "verifier: retry chamada LLM falhou",
                            extra={
                                "event": "verifier.contract.retry_call_failed",
                                "error_type": type(e).__name__,
                            },
                            exc_info=True,
                        )
                        span.add_event("verifier.contract.retry_call_failed")
                        new_draft = ""

                    if new_draft:
                        # Re-valida o draft corrigido.
                        cr2 = validate_contract(new_draft, output_contract)
                        contract_retried = True
                        contract_retry_draft = new_draft
                        if cr2.compliant:
                            # Sucesso: usa novo draft como o oficial.
                            draft = new_draft
                            contract_compliant = True
                            contract_errors = []
                            span.set_attribute("verifier.contract_retry_success", True)
                            span.add_event("verifier.contract.retry_succeeded")
                            logger.info(
                                "verifier: retry corrigiu o contrato — draft substituído",
                                extra={"event": "verifier.contract.retry_succeeded"},
                            )
                        else:
                            # Retry também falhou: mantém errors do 2º attempt
                            # (mais úteis pra debugging — mostra que LLM
                            # ignorou a correção). Original preservado em
                            # contract_original_errors pra audit.
                            contract_errors = cr2.errors
                            span.set_attribute("verifier.contract_retry_success", False)
                            span.add_event("verifier.contract.retry_failed_final")
                            logger.warning(
                                "verifier: retry NÃO corrigiu — operador deve revisar",
                                extra={
                                    "event": "verifier.contract.retry_failed_final",
                                    "original_errors": contract_original_errors[:3],
                                    "retry_errors": cr2.errors[:3],
                                },
                            )

            # ─── 2. MultiDimJudge (LLM) ─────────────────────────
            # Profile fast: pula judge (só contract). Standard/rigorous: roda judge.
            run_judge = profile != "fast"
            dimensions: dict[str, dict] = {}
            unsupported_claims: list[str] = []
            judge_model = ""
            judge_tokens = 0
            judge_cost_usd = 0.0

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
                    judge_tokens = int(j.get("judge_tokens") or 0)
                    judge_cost_usd = float(j.get("judge_cost_usd") or 0.0)
                    span.set_attribute("verifier.judge_ok", True)
                except Exception as e:
                    logger.warning(f"MultiDimJudge falhou: {type(e).__name__}: {e}")
                    span.set_attribute("verifier.judge_ok", False)
                    span.set_attribute("verifier.judge_error", str(e)[:200])

            # FIN-1+FIN-2 (33.8.0): custo do verifier = juiz + retry de contrato.
            # Soma o retry (2ª geração do draft, antes descartada) ao judge_cost/
            # tokens p/ o TCO auditável (verifications + SSOT) incluir os dois.
            # Fora do `if run_judge` → conta o retry mesmo no profile fast.
            judge_tokens += retry_tokens
            judge_cost_usd += retry_cost_usd

            # Q5 (33.9.0): anti-auto-preferência — flag quando o MESMO modelo gerou
            # o draft (llm_model do agente) E o julgou (judge_model). O juiz pode se
            # favorecer; a flag torna o viés AUDITÁVEL (não muda o gate).
            generator_model = (llm_model or "").strip()
            self_judged = bool(
                judge_model and generator_model
                and judge_model.strip().lower() == generator_model.lower()
            )
            if self_judged:
                logger.warning(
                    "verifier.self_judged",
                    extra={"event": "verifier.self_judged", "model": judge_model},
                )

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
                judge_tokens=judge_tokens,
                judge_cost_usd=judge_cost_usd,
                generator_model=generator_model,
                self_judged=self_judged,
                # Wave Contract Retry
                contract_retried=contract_retried,
                contract_original_errors=contract_original_errors,
                contract_retry_draft=contract_retry_draft,
            )

            # ─── 4. Persistência ────────────────────────────────
            if persist:
                try:
                    await self._persist(
                        result, turn_id, interaction_id, profile,
                        agent_id=agent_id, pipeline_id=pipeline_id,
                        user_question=user_question, draft=draft,
                    )
                except Exception as e:
                    logger.warning(f"verifier persist falhou: {e}")

            return result

    @staticmethod
    async def _retry_contract_with_llm(
        *,
        original_draft: str,
        errors: list[str],
        output_contract: str,
        user_question: str,
        llm_provider_name: str,
        llm_model: Optional[str] = None,
        max_tokens: int = 2000,
    ) -> tuple[str, dict]:
        """Re-chama o LLM com instrução de correção do contrato.

        Prompt é minimalista e cirúrgico:
        - Inclui o draft anterior (pra LLM ver o que produziu)
        - Inclui os erros específicos do ContractValidator
        - Inclui o contrato esperado (pra LLM relembrar o shape)
        - Pede regeneração mantendo conteúdo factual

        Returns:
            (novo_draft, usage) — draft pode ser vazio se LLM devolveu nada; usage
            é o dict de tokens do provider (FIN-2 33.8.0: antes descartado). Não
            valida — caller revalida com ContractValidator.
        """
        from app.core.llm_providers import get_provider

        provider = get_provider(llm_provider_name, model=(llm_model or None))

        errors_block = "\n".join(f"- {e}" for e in errors[:5]) or "(violação não detalhada)"
        system = (
            "Você é um corretor de saída JSON. Recebe um draft que VIOLOU o "
            "contrato de saída e gera versão corrigida, preservando o conteúdo "
            "factual e ajustando APENAS o que viola o contrato. NÃO explique. "
            "Responda APENAS com o JSON corrigido (sem markdown, sem ```)."
        )
        user = (
            f"### Contrato esperado (Output Contract)\n{output_contract}\n\n"
            f"### Erros detectados pelo validador\n{errors_block}\n\n"
            f"### Pergunta original do usuário (contexto)\n{user_question or '(não fornecida)'}\n\n"
            f"### Draft anterior (que violou o contrato)\n{original_draft}\n\n"
            "### Tarefa\n"
            "Regere o JSON corrigindo APENAS os pontos de violação acima. "
            "Preserve campos e valores que já estavam corretos. Devolva JSON "
            "puro, sem comentários e sem ```."
        )

        # Tenta usar structured output se provider suportar (PR anterior).
        # Reduz chance de o retry também violar formato JSON.
        kwargs: dict = {"max_tokens": max_tokens}
        if getattr(provider, "supports_structured_output", False):
            # Tenta extrair schema do contract pra usar response_format.
            try:
                # Import lazy do helper do engine — evita import circular.
                from app.agents.engine import _extract_json_schema_from_contract
                schema = _extract_json_schema_from_contract(output_contract)
                if schema:
                    # name precisa casar com ^[a-zA-Z0-9_-]+$ — sanitiza title cru
                    # do SKILL.md. Mesma helper do engine.py:_build_response_format.
                    # Strict mode exige required completo + additionalProperties=false
                    # em todos os objetos — coercionamos antes de enviar.
                    from app.core.text_utils import (
                        coerce_to_openai_strict_schema,
                        sanitize_schema_name,
                    )
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": sanitize_schema_name(
                                schema.get("title"), fallback="CorrectedOutput"
                            ),
                            "schema": coerce_to_openai_strict_schema(schema),
                            "strict": True,
                        },
                    }
            except Exception:
                pass  # silent — sem response_format, segue só com prompt

        resp = await provider.generate(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        return (resp.get("content") or "").strip(), (resp.get("usage") or {})

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

    async def _persist(
        self, result: VerificationResult, turn_id, interaction_id, profile: str,
        agent_id: Optional[str] = None, pipeline_id: Optional[str] = None,
        user_question: str = "", draft: str = "",
    ):
        """Insere em `verifications`. Best-effort — falha não derruba o pipeline.

        Auditoria (24.10.0): grava também o dono (agent_id/pipeline_id), o par
        pergunta/resposta JULGADO (DLP-redacted — o draft aqui é o que o juiz
        viu, pós contract-retry) e o rastro do retry de contrato.
        """
        from app.core.database import _get_pool
        from app.core.dlp import redact_for_persist
        pool = _get_pool()
        rid = str(uuid.uuid4())
        d = result.dimensions or {}

        def _g(name: str, key: str) -> Any:
            return (d.get(name) or {}).get(key)

        q_red = redact_for_persist(user_question or "")[:4000] or None
        draft_red = redact_for_persist(draft or "")[:8000] or None

        async with pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO verifications
                  (id, turn_id, interaction_id,
                   agent_id, pipeline_id, question_redacted, draft_redacted,
                   factuality_score, factuality_reason,
                   completeness_score, completeness_reason,
                   tone_score, tone_reason,
                   safety_score, safety_reason,
                   contract_compliant, contract_errors,
                   contract_retried, contract_original_errors,
                   ok, confidence, unsupported_claims,
                   judge_model, profile, duration_ms,
                   judge_tokens, judge_cost_usd, generator_model, self_judged)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                        $16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29)
                """,
                rid, turn_id, interaction_id,
                agent_id, pipeline_id, q_red, draft_red,
                _g("factuality", "score"), _g("factuality", "reason"),
                _g("completeness", "score"), _g("completeness", "reason"),
                _g("tone_adherence", "score"), _g("tone_adherence", "reason"),
                _g("safety", "score"), _g("safety", "reason"),
                result.contract_compliant, json.dumps(result.contract_errors)[:8000],
                result.contract_retried,
                json.dumps(result.contract_original_errors)[:8000],
                result.ok, result.confidence, json.dumps(result.unsupported_claims)[:8000],
                result.judge_model, profile, result.duration_ms,
                int(result.judge_tokens or 0), float(result.judge_cost_usd or 0.0),
                str(result.generator_model or ""), bool(result.self_judged),
            )

        # FIN-1 (33.8.0): custo do verifier (juiz + retry de contrato) no SSOT de
        # custo org-wide — linha SEPARADA (source='judge', mesma interaction_id)
        # que o /dashboard/costs SOMA. Linha separada (INSERT, não UPDATE) evita
        # corrida com a linha 'invoke'. Off-path (schedule_analytics) — cobre o
        # verify SÍNCRONO (no caminho, mas não bloqueia) e o async_dispatcher.
        try:
            _toks = int(result.judge_tokens or 0)
            _cost = float(result.judge_cost_usd or 0.0)
            if interaction_id and (_toks > 0 or _cost > 0):
                from app.core.analytics_tasks import schedule_analytics
                from app.core.cost_ledger import record_invocation_cost
                schedule_analytics(record_invocation_cost(
                    interaction_id=interaction_id,
                    agent_id=agent_id, pipeline_id=pipeline_id,
                    source="judge", cost_usd=_cost, tokens_used=_toks,
                    latency_ms=(result.duration_ms or 0),
                ))
        except Exception as e:  # nunca derruba a persistência do verifier
            logger.warning("event=judge_cost_ledger_failed error=%s", str(e)[:200])


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
            from app.core.llm_providers import generate_with_hosted_fallback
            # Papel "judge" do Roteamento LLM (24.9.0) — antes hardcoded
            # get_provider("openai"). Falha na leitura do roteamento → azure.
            try:
                from app.llm_routing import resolve_llm_for_task
                provider_name, model = await resolve_llm_for_task("judge")
            except Exception:
                provider_name, model = "azure", None
            response, _, _ = await generate_with_hosted_fallback([
                {"role": "system", "content": "Verificador de evidência. Responda apenas em JSON válido."},
                {"role": "user", "content": verification_prompt},
            ], provider_name, model, purpose="verifier.legacy")
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
