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

Sem ground truth, NÃO calculamos:
- `context_recall` (precisa de gabarito do que DEVERIA ter sido recuperado)
- `answer_correctness` (precisa de resposta-padrão)
"""
from __future__ import annotations

from typing import Optional


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
