"""RAGAS metrics — versão heurística, sem chamadas LLM adicionais.

Mapeia 4 métricas canônicas do RAGAS pra sinais que já temos no runtime:

| Métrica RAGAS         | Fonte aqui                                    | Tipo        |
|-----------------------|-----------------------------------------------|-------------|
| `context_relevancy`   | média dos `relevance_score` das evidências    | heurístico  |
| `context_precision`   | precisão média @k (score >= threshold)        | heurístico  |
| `faithfulness`        | `verification.dimensions.factuality.score/5`  | LLM-judge   |
| `answer_relevancy`    | `verification.dimensions.completeness.score/5`| LLM-judge   |

Justificativa do approach heurístico:
- Em `fast` profile NÃO roda Verifier v2 (LLM judge), então `faithfulness` e
  `answer_relevancy` ficam None com source="unavailable". Operador vê a UI
  decomposta independente do profile — context_* sempre aparece, answer_*
  só quando ligar Verifier v2 em standard/rigorous.
- `context_precision` puro do RAGAS usa LLM pra julgar relevância por chunk
  e checa se chunks relevantes vieram primeiro. Aqui usamos o próprio
  `relevance_score` do retriever como sinal de verdade (proxy razoável quando
  reranker é decente) — zero chamada LLM extra, latência ≈ 0.

Com ground truth (gold), as 2 métricas restantes são computadas por
`compute_gold_ragas` (LLM-judge, gated por `ragas_ground_truth_enabled`, 33.12.0):
- `context_recall` (fração do gabarito suportada pelos contextos recuperados)
- `answer_correctness` (correção da resposta gerada vs a resposta-padrão)
Sem gold (produção normal) OU com o toggle OFF, essas 2 ficam `source='unavailable'`.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_THRESHOLD = 0.3


def _coerce_score(s) -> Optional[float]:
    """Aceita int/float/string numérica; rejeita None/'/strings vazias."""
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _context_relevancy(evidences: list[dict] | list) -> dict:
    """Média simples dos relevance_score retornados pelo retriever.

    É numericamente o MESMO valor que o `evidence_score` mostrado em
    "Score de confiança" hoje — exposto aqui com o nome RAGAS canônico
    pra UX consistente com a literatura.
    """
    if not evidences:
        return {
            "score": 0.0,
            "source": "heuristic",
            "reason": "Sem evidências recuperadas — score 0 por convenção.",
        }
    scores = []
    for e in evidences:
        if hasattr(e, "relevance_score"):
            v = _coerce_score(getattr(e, "relevance_score", None))
        else:
            v = _coerce_score((e or {}).get("relevance_score"))
        if v is not None:
            scores.append(v)
    if not scores:
        return {
            "score": 0.0,
            "source": "heuristic",
            "reason": "Evidências sem `relevance_score` — retriever sem rerank?",
        }
    avg = sum(scores) / len(scores)
    return {
        "score": round(avg, 4),
        "source": "heuristic",
        "reason": f"Média de {len(scores)} relevance_score(s) do retriever.",
    }


def _context_precision(evidences: list[dict] | list, threshold: float) -> dict:
    """Precision @ k médio sobre posições com doc 'relevante' (score >= threshold).

    Fórmula canônica do RAGAS (sem o judge LLM): pra cada posição i onde o doc
    é relevante, computa precision@i = (# relevantes em top-i) / i. Média
    dessas precisões = context_precision.

    Intuição: penaliza quando ranking trouxe lixo PRIMEIRO e relevante DEPOIS.
    Score alto = relevantes vieram no topo (o que o operador espera do reranker).

    Edge cases:
    - 0 evidências → 0.0 ("nada recuperado")
    - Nenhum acima do threshold → 0.0 ("retriever falhou completamente")
    - Todos no topo, perfeitamente ordenados → 1.0
    """
    if not evidences:
        return {
            "score": 0.0,
            "source": "heuristic",
            "reason": "Sem evidências recuperadas.",
        }
    relevant_at_i: list[float] = []
    relevant_count = 0
    total = 0
    for i, e in enumerate(evidences, start=1):
        if hasattr(e, "relevance_score"):
            v = _coerce_score(getattr(e, "relevance_score", None))
        else:
            v = _coerce_score((e or {}).get("relevance_score"))
        if v is None:
            continue
        total += 1
        if v >= threshold:
            relevant_count += 1
            relevant_at_i.append(relevant_count / i)
    if not relevant_at_i:
        return {
            "score": 0.0,
            "source": "heuristic",
            "reason": (
                f"Nenhum chunk acima do threshold {threshold:.2f} — "
                "retriever não trouxe nada relevante."
            ),
        }
    avg_p = sum(relevant_at_i) / len(relevant_at_i)
    return {
        "score": round(avg_p, 4),
        "source": "heuristic",
        "reason": (
            f"Avg precision@k sobre {relevant_count} chunk(s) acima do "
            f"threshold {threshold:.2f} (de {total} avaliados)."
        ),
    }


def _from_judge_dimension(
    verification: Optional[dict],
    dim_key: str,
    max_score: float,
    needs_profile_msg: str,
) -> dict:
    """Lê uma dimensão do MultiDimJudge e normaliza pra [0..1].

    Quando verification é None (fast profile / Verifier desligado), retorna
    score=None com source="unavailable" — UI deve mostrar placeholder com
    hint de ativar o Verifier.
    """
    if not verification:
        return {
            "score": None,
            "source": "unavailable",
            "reason": needs_profile_msg,
        }
    dims = (verification or {}).get("dimensions") or {}
    raw = (dims.get(dim_key) or {}).get("score")
    norm = _coerce_score(raw)
    if norm is None:
        return {
            "score": None,
            "source": "judge",
            "reason": (
                f"Judge não pontuou `{dim_key}` "
                "(ex: factuality=null quando não há evidências)."
            ),
        }
    judge_reason = (dims.get(dim_key) or {}).get("reason") or ""
    return {
        "score": round(norm / max_score, 4),
        "source": "judge",
        "reason": judge_reason[:300],
    }


def compute_heuristic_ragas(
    evidences: list | None,
    verification: Optional[dict] = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> dict:
    """Calcula 4 métricas RAGAS sem chamada LLM extra.

    Args:
        evidences: lista de chunks recuperados. Cada item pode ser dict
            (com chave `relevance_score`) ou objeto (com atributo
            `relevance_score`). Retriever do projeto devolve ambos os
            shapes em pontos diferentes do pipeline — função tolera os
            dois.
        verification: serialized VerificationResult (com `dimensions`).
            None quando Verifier v2 não rodou (fast profile).
        threshold: corte de relevância vindo de ## Evidence Policy da
            skill ou default 0.3 do engine.

    Returns:
        dict com chaves: context_relevancy, context_precision,
        faithfulness, answer_relevancy. Cada valor é um sub-dict
        {score, source, reason}. Score é float [0..1] OU None quando
        métrica requer LLM judge que não rodou.
    """
    evs = evidences or []
    fast_hint = (
        "Ative VERIFIER_V2_ENABLED=true e use Execution Profile "
        "`standard` ou `rigorous` pra esta métrica."
    )
    return {
        "context_relevancy": _context_relevancy(evs),
        "context_precision": _context_precision(evs, threshold),
        "faithfulness": _from_judge_dimension(
            verification, "factuality", max_score=5.0,
            needs_profile_msg=fast_hint,
        ),
        "answer_relevancy": _from_judge_dimension(
            verification, "completeness", max_score=5.0,
            needs_profile_msg=fast_hint,
        ),
        "_meta": {
            "threshold_applied": threshold,
            "evidence_count": len(evs),
            "has_judge": verification is not None,
        },
    }


# ───────────────────────────────────────────────────────────────
# RAGAS COM GABARITO (33.12.0) — context_recall + answer_correctness
# As 2 métricas que exigem ground truth (resposta-padrão do gold). Cada uma =
# 1 chamada LLM-judge (LLM-cost) → só rodam quando ragas_ground_truth_enabled
# está ON (o caller — run_evaluation do harness — faz esse gate) E há gabarito.
# ───────────────────────────────────────────────────────────────

_AC_SYSTEM = (
    "Você é um avaliador RAGAS rigoroso. Meça ANSWER CORRECTNESS: compare a "
    "RESPOSTA GERADA com o GABARITO (resposta de referência) e estime o quão "
    "correta a resposta é — cobertura das afirmações corretas do gabarito e "
    "ausência de afirmações erradas/contraditórias. Ignore estilo/formato; foque "
    "no conteúdo factual. Responda SÓ com JSON: "
    '{"score": <número entre 0 e 1>, "reason": "<1 frase curta>"}. '
    "1.0 = factualmente equivalente ao gabarito; 0.0 = incorreta/contraditória."
)

_CR_SYSTEM = (
    "Você é um avaliador RAGAS rigoroso. Meça CONTEXT RECALL: dado o GABARITO "
    "(resposta de referência) e os CONTEXTOS recuperados, estime a fração das "
    "afirmações do gabarito que são SUPORTADAS/atribuíveis aos contextos. "
    'Responda SÓ com JSON: {"score": <número entre 0 e 1>, "reason": "<1 frase curta>"}. '
    "1.0 = toda afirmação do gabarito tem suporte nos contextos; 0.0 = nenhuma."
)


def _parse_score_obj(content: str) -> Optional[dict]:
    """Parse tolerante de {\"score\":..,\"reason\":..} (remove wrap markdown,
    acha o 1º objeto). Espelha MultiDimJudge._parse_json_robust. None se falhar."""
    import json
    import re
    if not content:
        return None
    c = content.strip()
    if "```" in c:
        m = re.search(r"```(?:json)?\s*(.*?)```", c, re.DOTALL)
        if m:
            c = m.group(1).strip()
    try:
        obj = json.loads(c)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", c, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


async def _judge_ragas_score(system_prompt: str, user_prompt: str, purpose: str) -> dict:
    """UMA chamada LLM-judge → {score:[0..1]|None, source, reason, tokens, cost_usd}.

    Espelha MultiDimJudge.evaluate: resolve o provider do papel 'judge'
    (resolve_llm_for_task), usa generate_with_hosted_fallback (fallback hospedado
    embutido) e cobra o custo com o provider/model EFETIVO pós-fallback. BEST-
    EFFORT: qualquer exceção/parse ruim → score=None (a métrica vira 'unavailable',
    NUNCA derruba o run do harness)."""
    from app.core.config import get_settings
    settings = get_settings()

    # Provider do papel 'judge' (mesma rota do MultiDimJudge); fallback à env.
    try:
        from app.llm_routing import resolve_llm_for_task
        provider_name, model = await resolve_llm_for_task("judge")
    except Exception:
        mid = (settings.verifier_judge_model or "azure/gpt-4o").strip()
        if "/" in mid:
            provider_name, model = mid.split("/", 1)
            provider_name, model = provider_name.strip().lower(), model.strip()
        else:
            provider_name, model = "azure", mid

    try:
        from app.core.llm_providers import generate_with_hosted_fallback
        resp, used_provider, used_model = await generate_with_hosted_fallback(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            provider_name, model,
            purpose=purpose,
            gen_kwargs={"max_tokens": settings.verifier_max_tokens},
        )
    except Exception as e:
        logger.warning("ragas_gold: chamada LLM falhou (%s): %s", purpose, str(e)[:150])
        return {"score": None, "source": "unavailable", "reason": "LLM indisponível",
                "tokens": 0, "cost_usd": 0.0}

    content = (resp.get("content") or "").strip()
    usage = resp.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    tokens = in_tok + out_tok
    try:
        from app.core.llm_pricing import compute_cost
        cost_usd = compute_cost(used_provider, used_model, in_tok, out_tok)
    except Exception:
        cost_usd = 0.0

    parsed = _parse_score_obj(content)
    score = _coerce_score((parsed or {}).get("score"))
    if score is None:
        return {"score": None, "source": "judge", "reason": "juiz não retornou score numérico",
                "tokens": tokens, "cost_usd": cost_usd}
    score = max(0.0, min(1.0, score))  # clamp [0..1] — o resto do Verifier assume esse range
    reason = str((parsed or {}).get("reason") or "")[:300]
    return {"score": round(score, 4), "source": "judge", "reason": reason,
            "tokens": tokens, "cost_usd": cost_usd}


async def compute_gold_ragas(answer: str, ground_truth: str, contexts: Optional[list] = None) -> dict:
    """context_recall + answer_correctness (RAGAS COM GABARITO, 33.12.0).

    - answer: resposta gerada pelo agente.
    - ground_truth: expected_output do caso gold (a referência).
    - contexts: textos dos trechos recuperados (para context_recall; sem eles a
      métrica é incomparável → None).

    answer_correctness = 1 chamada LLM; context_recall = +1 chamada SE houver
    contexto. Retorna {context_recall:{score,source,reason}, answer_correctness:
    {...}, _meta:{cost_usd, tokens, has_contexts}}. score em [0..1] OU None
    (incomparável / juiz falhou). O CALLER decide QUANDO chamar (toggle)."""
    gt = (ground_truth or "").strip()
    if not gt:
        na = {"score": None, "source": "unavailable",
              "reason": "sem gabarito (expected_output vazio)"}
        return {"context_recall": dict(na), "answer_correctness": dict(na),
                "_meta": {"cost_usd": 0.0, "tokens": 0, "has_contexts": False}}

    ctx_texts = [str(c).strip() for c in (contexts or []) if str(c).strip()]
    total_cost = 0.0
    total_tokens = 0

    ac = await _judge_ragas_score(
        _AC_SYSTEM,
        f"GABARITO (resposta de referência):\n{gt}\n\n"
        f"RESPOSTA GERADA:\n{(answer or '').strip() or '(resposta vazia)'}",
        purpose="verifier.ragas.answer_correctness",
    )
    total_cost += ac.get("cost_usd") or 0.0
    total_tokens += ac.get("tokens") or 0

    if ctx_texts:
        ctx_block = "\n\n".join(f"[C{i + 1}] {t[:1000]}" for i, t in enumerate(ctx_texts[:20]))
        cr = await _judge_ragas_score(
            _CR_SYSTEM,
            f"GABARITO (resposta de referência):\n{gt}\n\nCONTEXTOS RECUPERADOS:\n{ctx_block}",
            purpose="verifier.ragas.context_recall",
        )
        total_cost += cr.get("cost_usd") or 0.0
        total_tokens += cr.get("tokens") or 0
    else:
        cr = {"score": None, "source": "unavailable",
              "reason": "sem contextos recuperados — context_recall requer evidências"}

    def _clean(m: dict) -> dict:
        return {"score": m.get("score"), "source": m.get("source"), "reason": m.get("reason")}

    return {
        "context_recall": _clean(cr),
        "answer_correctness": _clean(ac),
        "_meta": {"cost_usd": round(total_cost, 6), "tokens": total_tokens,
                  "has_contexts": bool(ctx_texts)},
    }
