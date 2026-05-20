"""Detecção de anomalias em catalog_costs (Onda 4 PR #71).

Reusa os dados já gravados pelo executor (PR #67/#69). Dois tipos de
anomalia hoje:

1. **Pico relativo** — custo de hoje > N× a média dos últimos K dias.
   Bom para detectar saltos atípicos (ex: agent rodando em loop).

2. **Limite global** — custo de hoje em USD > teto absoluto.
   Bom para guardrail de budget mensal/diário (early warning).

**Decisão de escopo**: thresholds são hardcoded (vs env vars ou DB).
Reasoning: tuning de threshold de anomaly é raro e deve passar por
revisão. Se o ritmo aumentar, migra para platform_settings num PR
futuro sem mudar a API de detect_anomalies.

**Sandbox**: runs sandbox (is_sandbox=true) NÃO geram row em catalog_costs
(PR #70), portanto não inflacionam nem hoje nem baseline. Conta-se só
o que de fato bateu chargeback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.database import _get_pool

logger = logging.getLogger(__name__)


# ─── Thresholds (hardcoded; ver docstring do módulo) ─────────────

PICO_MULTIPLIER: float = 3.0
"""Quantas vezes a média do baseline o custo de hoje precisa ultrapassar
para virar 'pico'. 3x é conservador — captura saltos reais sem alarmar
em flutuação normal de uso (que costuma ficar em ±50%)."""

PICO_MIN_BASELINE_USD: float = 1.0
"""Se baseline avg < $1, NÃO detecta pico (evita 'qualquer coisa × 0.1
parece pico'). Operações fora do ar voltando produzem ratios absurdos
sem significado."""

LIMITE_GLOBAL_USD: float = 100.0
"""Teto absoluto de custo diário. Ultrapassar dispara anomalia com
severity='warning'. Calibrado para o piloto; aumente em prod a critério
de FinOps."""

BASELINE_WINDOW_DAYS: int = 7
"""Janela de baseline (dias anteriores a hoje, exclusivo). 7d filtra
sazonalidade semanal sem ficar lento demais para responder a mudanças
de padrão."""


async def _query_today_total(
    consumer_user_id: Optional[str] = None,
    consumer_department: Optional[str] = None,
) -> float:
    """Soma cost_usd de hoje (00:00 UTC ao now), com filtros opcionais."""
    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = ["DATE(invoked_at) = CURRENT_DATE"]
    if consumer_user_id:
        params.append(consumer_user_id)
        where_parts.append(f"consumer_user_id = ${len(params)}")
    if consumer_department:
        params.append(consumer_department)
        where_parts.append(f"consumer_department = ${len(params)}")
    sql = f"""
        SELECT COALESCE(SUM(cost_usd), 0) AS total
        FROM catalog_costs
        WHERE {" AND ".join(where_parts)}
    """
    async with pool.acquire() as con:
        r = await con.fetchrow(sql, *params)
    return float(r["total"] or 0.0)


async def _query_baseline_avg(
    consumer_user_id: Optional[str] = None,
    consumer_department: Optional[str] = None,
    window_days: int = BASELINE_WINDOW_DAYS,
) -> float:
    """Média diária dos N dias anteriores a hoje (exclusivo). Dias sem
    cost contam como 0 — pega average sobre todos os dias da janela,
    não só os com dados (queremos detectar pico vs zero também)."""
    pool = _get_pool()
    params: list[Any] = [window_days]
    where_parts: list[str] = [
        "DATE(invoked_at) >= CURRENT_DATE - $1::int",
        "DATE(invoked_at) < CURRENT_DATE",
    ]
    if consumer_user_id:
        params.append(consumer_user_id)
        where_parts.append(f"consumer_user_id = ${len(params)}")
    if consumer_department:
        params.append(consumer_department)
        where_parts.append(f"consumer_department = ${len(params)}")
    # SUM / window_days — divide pela janela inteira para incluir dias zero
    sql = f"""
        SELECT COALESCE(SUM(cost_usd), 0) / $1::float AS avg_per_day
        FROM catalog_costs
        WHERE {" AND ".join(where_parts)}
    """
    async with pool.acquire() as con:
        r = await con.fetchrow(sql, *params)
    return float(r["avg_per_day"] or 0.0)


async def detect_anomalies(
    *,
    consumer_user_id: Optional[str] = None,
    consumer_department: Optional[str] = None,
) -> dict:
    """Calcula anomalias para o scope dado. Sem scope = global (todos).

    Returns shape:
        {
            "checked_at": ISO-8601 string,
            "today_usd": float,
            "baseline_avg_usd": float,
            "scope": {user_id, department},
            "anomalies": [
                {type, severity, message, value, threshold}, ...
            ]
        }
    """
    today_usd = await _query_today_total(consumer_user_id, consumer_department)
    baseline_avg = await _query_baseline_avg(consumer_user_id, consumer_department)

    anomalies: list[dict] = []

    # 1. Pico relativo — só se baseline >= floor (evita ratios absurdos)
    if baseline_avg >= PICO_MIN_BASELINE_USD:
        ratio = today_usd / baseline_avg if baseline_avg > 0 else 0.0
        if ratio >= PICO_MULTIPLIER:
            anomalies.append({
                "type": "pico_relativo",
                "severity": "warning",
                "message": (
                    f"Custo de hoje (US$ {today_usd:.2f}) está {ratio:.1f}× "
                    f"acima da média dos últimos {BASELINE_WINDOW_DAYS}d "
                    f"(US$ {baseline_avg:.2f})"
                ),
                "value": round(today_usd, 4),
                "threshold": round(baseline_avg * PICO_MULTIPLIER, 4),
                "ratio": round(ratio, 2),
            })

    # 2. Limite global absoluto
    if today_usd > LIMITE_GLOBAL_USD:
        anomalies.append({
            "type": "limite_global",
            "severity": "warning",
            "message": (
                f"Custo total de hoje (US$ {today_usd:.2f}) ultrapassou "
                f"o limite diário de US$ {LIMITE_GLOBAL_USD:.2f}"
            ),
            "value": round(today_usd, 4),
            "threshold": LIMITE_GLOBAL_USD,
        })

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "today_usd": round(today_usd, 4),
        "baseline_avg_usd": round(baseline_avg, 4),
        "scope": {
            "consumer_user_id": consumer_user_id,
            "consumer_department": consumer_department,
        },
        "anomalies": anomalies,
        "thresholds": {
            "pico_multiplier": PICO_MULTIPLIER,
            "pico_min_baseline_usd": PICO_MIN_BASELINE_USD,
            "limite_global_usd": LIMITE_GLOBAL_USD,
            "baseline_window_days": BASELINE_WINDOW_DAYS,
        },
    }
