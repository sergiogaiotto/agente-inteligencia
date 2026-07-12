"""SSOT de custo por invocação — `invocation_costs` (insert-only) + agregação.

Cobre TODOS os caminhos de invoke (pipeline sync/stream, inclusive cookie/UI e
X-API-Key) — o ponto cego que `catalog_costs` (só catálogo) e `api_key_cost_ledger`
(só com toggle + key) deixavam. A escrita roda OFF-PATH (via `_schedule_analytics`
do invoke), NUNCA no caminho de resposta. Aqui vive o "quanto gastamos" org-wide.

Insert-only: cada invocação = 1 linha. Agregação por pipeline/agent/user/source/day.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# group_by → expressão SQL. `day` trunca para a data; os demais são colunas diretas.
_GROUP_COLS = {
    "pipeline": "pipeline_id",
    "agent": "agent_id",
    "user": "user_id",
    "source": "source",
    "day": "created_at::date",
}


async def record_invocation_cost(
    *,
    interaction_id: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    channel: Optional[str] = None,
    source: Optional[str] = None,
    cost_usd: float = 0.0,
    tokens_used: int = 0,
    latency_ms: float = 0.0,
    final_state: Optional[str] = None,
) -> None:
    """Insere UMA linha em `invocation_costs`. Best-effort, roda off-path."""
    from app.core.database import _get_pool
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO invocation_costs
              (id, interaction_id, pipeline_id, agent_id, user_id, api_key_id,
               channel, source, cost_usd, tokens_used, latency_ms, final_state)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            str(uuid.uuid4()), interaction_id, pipeline_id, agent_id, user_id, api_key_id,
            channel, source, float(cost_usd or 0.0), int(tokens_used or 0),
            float(latency_ms or 0.0), final_state,
        )


async def aggregate_invocation_costs(
    *,
    group_by: str = "pipeline",
    since: Optional[str] = None,   # ISO date YYYY-MM-DD
    until: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 200,
) -> tuple[list[dict], dict]:
    """Agrega `invocation_costs` por grupo. Returns (rows, totals).

    rows: {group_key, invocations, total_cost_usd, total_tokens, avg_latency_ms}.
    totals: os mesmos agregados globais para os filtros aplicados.
    """
    if group_by not in _GROUP_COLS:
        raise ValueError(f"group_by inválido. Opções: {list(_GROUP_COLS)}")
    group_expr = _GROUP_COLS[group_by]

    from app.core.database import _get_pool
    params: list[Any] = []
    where: list[str] = []
    if since:
        params.append(since)
        where.append(f"created_at >= ${len(params)}::date")
    if until:
        params.append(until)
        where.append(f"created_at < (${len(params)}::date + interval '1 day')")
    if pipeline_id:
        params.append(pipeline_id)
        where.append(f"pipeline_id = ${len(params)}")
    if user_id:
        params.append(user_id)
        where.append(f"user_id = ${len(params)}")
    if source:
        params.append(source)
        where.append(f"source = ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    where_params = list(params)            # só os filtros (para o totals)
    params.append(limit)
    limit_ph = f"${len(params)}"

    rows_sql = f"""
        SELECT {group_expr}::text AS group_key,
               count(*)::int                         AS invocations,
               COALESCE(sum(cost_usd), 0)::float     AS total_cost_usd,
               COALESCE(sum(tokens_used), 0)::bigint AS total_tokens,
               COALESCE(avg(latency_ms), 0)::float   AS avg_latency_ms
        FROM invocation_costs
        {where_sql}
        GROUP BY {group_expr}
        ORDER BY total_cost_usd DESC
        LIMIT {limit_ph}
    """
    totals_sql = f"""
        SELECT count(*)::int                         AS invocations,
               COALESCE(sum(cost_usd), 0)::float     AS total_cost_usd,
               COALESCE(sum(tokens_used), 0)::bigint AS total_tokens,
               COALESCE(avg(latency_ms), 0)::float   AS avg_latency_ms
        FROM invocation_costs
        {where_sql}
    """

    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(rows_sql, *params)
        totals = await con.fetchrow(totals_sql, *where_params)

    def _round(d: dict) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}

    return [_round(dict(r)) for r in rows], (_round(dict(totals)) if totals else {})
