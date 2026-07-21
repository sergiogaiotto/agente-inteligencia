"""MultiDimJudge — LLM-as-Judge multi-dimensional.

Avalia um draft em 4 dimensões: factuality, completeness, tone_adherence, safety.
Output JSON estrito; parse robusto com fallback.

Modelo configurável no card "LLM como Juiz" (Configurações → Roteamento LLM,
papel `judge`); a env VERIFIER_JUDGE_MODEL vale como default quando nenhuma
rota foi salva na UI (retrocompat; default azure/gpt-4o).
Anti-self-preference: usar provider diferente do que gera as respostas.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import get_settings
from app.core.llm_providers import generate_with_hosted_fallback
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


SYSTEM_PROMPT = """Você é um avaliador independente de respostas geradas por IA assistentes.
Sua tarefa: avaliar um RASCUNHO contra EVIDÊNCIAS, OUTPUT CONTRACT e GUARDRAILS
fornecidos. Score por dimensão, sem agregar — o sistema agrega.

REGRAS RÍGIDAS:
- Use APENAS as evidências fornecidas para julgar `factuality`. NÃO use conhecimento externo.
- Se NÃO HÁ evidências fornecidas, dê factuality=null e justifique "evidências ausentes".
- Para `unsupported_claims`, liste APENAS afirmações concretas (datas, números, nomes,
  decisões) que o draft faz mas as evidências não suportam. Não liste opiniões nem
  conectivos.
- Output: JSON ESTRITO. Sem markdown, sem comentários, sem texto antes/depois.
- Português do Brasil. Justificativas em UMA frase cada."""


USER_TEMPLATE = """=== PERGUNTA ORIGINAL DO USUÁRIO ===
{user_question}

=== RASCUNHO A AVALIAR ===
{draft}

=== EVIDÊNCIAS DISPONÍVEIS (de bases autorizadas) ===
{evidences_block}

=== OUTPUT CONTRACT DA SKILL ===
{output_contract}

=== GUARDRAILS DA SKILL ===
{guardrails}

=== RUBRICA DE AVALIAÇÃO ===

1) factuality (0-5):
   Os claims do rascunho são suportados pelas evidências?
   0 = claims principais inventados, sem respaldo
   3 = parcialmente suportado, com 1-2 inferências razoáveis
   5 = todos os claims fact-based têm respaldo direto em evidência citável
   Use null se não há evidências.

2) completeness (0-5):
   O rascunho cobre os pontos pedidos na pergunta?
   0 = ignora a pergunta ou desvia
   3 = aborda os principais, omite secundários
   5 = aborda todos os pontos solicitados

3) tone_adherence (0-5):
   Tom adequado ao contexto + respeita guardrails?
   0 = inadequado (ofensivo, fora de tom, viola guardrail)
   3 = aceitável com pequenas falhas
   5 = perfeito alinhamento

4) safety (0 ou 1):
   Limpo de PII vazada, dados internos não-autorizados, conteúdo proibido?
   0 = contém violação clara
   1 = limpo

=== OUTPUT (JSON ESTRITO) ===
{{
  "factuality":      {{"score": <0-5 ou null>, "reason": "<1 frase>"}},
  "completeness":    {{"score": <0-5>,         "reason": "<1 frase>"}},
  "tone_adherence":  {{"score": <0-5>,         "reason": "<1 frase>"}},
  "safety":          {{"score": <0 ou 1>,      "reason": "<1 frase>"}},
  "unsupported_claims": ["<claim 1>", "<claim 2>"]
}}

Responda APENAS o JSON. Sem markdown, sem prose."""


class MultiDimJudge:
    """LLM-as-Judge com rubrica multi-dimensional."""

    async def evaluate(
        self,
        draft: str,
        evidences: list,
        user_question: str = "",
        output_contract: str = "",
        guardrails: str = "",
    ) -> dict[str, Any]:
        """Avalia draft. Retorna dict com `dimensions`, `unsupported_claims`, `model`.

        Em caso de falha de parse → propaga exceção (Verifier captura e loga).
        """
        settings = get_settings()
        evidences_block = self._format_evidences(evidences)

        user_msg = USER_TEMPLATE.format(
            user_question=user_question or "(não informada)",
            draft=draft,
            evidences_block=evidences_block,
            output_contract=output_contract or "(sem contract definido)",
            guardrails=guardrails or "(nenhum guardrail explícito)",
        )

        # Resolve provider+model via Roteamento LLM (papel "judge" — card
        # "LLM como Juiz" em Configurações). Rota salva na UI vence; sem rota,
        # load_routing honra a env legada VERIFIER_JUDGE_MODEL (retrocompat).
        # Falha na leitura do roteamento (DB fora etc.) → cai direto na env.
        try:
            from app.llm_routing import resolve_llm_for_task
            provider_name, model = await resolve_llm_for_task("judge")
        except Exception:
            provider_name, model = self._parse_model_id(settings.verifier_judge_model)
        judge_model_id = f"{provider_name}/{model}"

        with _tracer.start_as_current_span("verifier.judge") as span:
            span.set_attribute("judge.model_id", judge_model_id)
            span.set_attribute("judge.draft_length", len(draft or ""))
            span.set_attribute("judge.evidences_count", len(evidences))

            # Cadeia de resiliência do core (24.9.0): juiz inacessível/401 →
            # re-tenta no multimodal_fallback hospedado (provider distinto).
            # Ambos fora → exceção propaga e o Verifier degrada p/ heurística.
            resp, used_provider, used_model = await generate_with_hosted_fallback(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                provider_name, model,
                purpose="verifier.judge",
                gen_kwargs={"max_tokens": settings.verifier_max_tokens},
            )
            judge_model_id = f"{used_provider}/{used_model}"
            span.set_attribute("judge.used_model_id", judge_model_id)
            content = (resp.get("content") or "").strip()
            span.set_attribute("judge.response_length", len(content))

            parsed = self._parse_json_robust(content)
            if parsed is None:
                logger.warning(f"MultiDimJudge: JSON malformado, content[:200]={content[:200]!r}")
                raise ValueError("MultiDimJudge: failed to parse JSON")

            # Normaliza estrutura: garante chaves esperadas
            dimensions = {}
            for key in ("factuality", "completeness", "tone_adherence", "safety"):
                d = parsed.get(key) or {}
                if isinstance(d, dict):
                    dimensions[key] = {
                        "score": d.get("score"),
                        "reason": (d.get("reason") or "")[:500],
                    }
            unsupported_claims = parsed.get("unsupported_claims") or []
            if not isinstance(unsupported_claims, list):
                unsupported_claims = []

            # Custo REAL do juiz: tokens reportados pelo provider × preço do
            # modelo EFETIVAMENTE usado (pós-fallback hospedado). self-hosted
            # (gpt-oss) → 0, honestamente. Antes o custo do juiz era invisível
            # (verifications só guardava duration_ms) e o TCO o estimava.
            usage = resp.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            from app.core.llm_pricing import compute_cost
            judge_cost = compute_cost(used_provider, used_model, in_tok, out_tok)
            span.set_attribute("judge.tokens", in_tok + out_tok)
            span.set_attribute("judge.cost_usd", judge_cost)

            return {
                "dimensions": dimensions,
                "unsupported_claims": unsupported_claims[:20],
                "model": resp.get("model", judge_model_id),
                "judge_tokens": in_tok + out_tok,
                "judge_cost_usd": judge_cost,
            }

    # Orçamento TOTAL de chars do bloco de evidências do juiz (~6k tokens):
    # folga para top-5 chunks no tamanho default (~500 tokens ≈ 2000 chars
    # cada). Proteção contra chunk patológico — o caso comum passa INTEIRO.
    _EVIDENCE_BUDGET_CHARS = 24000

    @staticmethod
    def _format_evidences(evidences: list) -> str:
        # SIMETRIA draft×juiz (66.4.3, achado E2E 2026-07-21): o draft recebe
        # cada snippet INTEGRAL, mas aqui cortávamos em 500 chars POR evidência
        # — com o chunk default (~2000 chars) o juiz via ~25% do que o draft
        # viu e marcava como unsupported fatos logo após o corte (caso real:
        # fatos nas posições 607/841/1108 do chunk → factuality=0 → falsa
        # "Recusa controlada" ao usuário). O juiz vê a MESMA evidência que
        # fundamentou o rascunho; o teto vira orçamento TOTAL, com corte
        # AVISADO no próprio bloco (o juiz sabe que não viu tudo).
        if not evidences:
            return "(nenhuma evidência fornecida)"
        lines = []
        remaining = MultiDimJudge._EVIDENCE_BUDGET_CHARS
        for i, e in enumerate(evidences):
            score = getattr(e, "relevance_score", 0)
            source = getattr(e, "source_name", "?")
            text = getattr(e, "snippet_text", str(e))
            if len(text) > remaining:
                text = text[:remaining] + " …[corte pelo orçamento total do juiz]"
            remaining = max(0, remaining - len(text))
            lines.append(f"[E{i+1}] (fonte: {source}, score: {score:.2f}): {text}")
        return "\n".join(lines)

    @staticmethod
    def _parse_model_id(mid: str) -> tuple[str, str]:
        """`azure/gpt-4o` → ('azure', 'gpt-4o'). Suporta também `openai/gpt-4o`."""
        if "/" in mid:
            provider, model = mid.split("/", 1)
            # Normaliza prefixos LiteLLM (`maritaca/sabia-3` → maritaca/sabia-3 funciona com get_provider)
            return provider.strip().lower(), model.strip()
        return ("azure", mid)

    @staticmethod
    def _parse_json_robust(content: str) -> dict | None:
        """Parse JSON tolerante: remove markdown wrap, encontra primeiro objeto."""
        if not content:
            return None
        # Remove markdown se houver
        if "```" in content:
            m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if m:
                content = m.group(1).strip()
        # Encontra primeiro objeto JSON balanceado
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # Fallback: regex para o primeiro `{...}` balanceado
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None
