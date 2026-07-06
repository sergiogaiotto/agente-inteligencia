"""Quota de custo por API Key (F6).

Uma X-API-Key entregue a um frontend externo gasta tokens de LLM sem teto. Este
módulo dá à plataforma um **orçamento de custo por key**, com débito real e
bloqueio (402) quando a janela corrente estoura.

Fluxo (só quando ``api_key_cost_budget_enabled`` está ON):

1. **Pré-checagem** (``enforce_budget``): antes de executar um invoke via key,
   se a key tem orçamento (``api_keys.cost_budget_usd``) e o gasto acumulado da
   janela corrente (dia | mês | acumulado) já atingiu o teto → HTTP 402. Soft-cap:
   o invoke que CRUZA o teto passa; os seguintes são barrados (não dá pra prever o
   custo do invoke atual antes de executar).
2. **Débito** (``record_cost``): após executar, grava o ``cost_usd``/tokens REAIS
   (soma dos steps do pipeline — ``_step_cost_and_tokens`` no engine) no ledger
   ``api_key_cost_ledger``. Best-effort: falha aqui NUNCA derruba a resposta.

Semântica:
- Toggle OFF → no-op total (sem débito, sem bloqueio) = comportamento atual.
- Key SEM orçamento (NULL) → nunca bloqueada; só tem o gasto registrado quando ON.
- Janela em UTC (o app inteiro armazena UTC naive). Um teto "mensal" reseta à
  meia-noite UTC do dia 1 — para custo advisory, a diferença de fuso é irrelevante.
- gpt-oss custa 0 (só Azure/OpenAI têm preço em ``llm_pricing``) — o débito reflete.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

from app.core.datetime_utils import naive_utc_now

logger = logging.getLogger(__name__)

_VALID_WINDOWS = ("day", "month", "total")
_WINDOW_PT = {"day": "dia", "month": "mês", "total": "acumulado"}


def normalize_window(window: Optional[str]) -> str:
    """Coage a janela para um valor válido (default 'month')."""
    w = (window or "month").lower().strip()
    return w if w in _VALID_WINDOWS else "month"


def _window_start(window: str, now: datetime) -> Optional[datetime]:
    """Início (UTC naive) da janela corrente. 'total' (ou desconhecido) → None
    (sem filtro temporal — soma tudo). Função pura p/ teste."""
    w = normalize_window(window)
    if w == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if w == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None  # total


def cost_and_tokens_from_result(result: dict) -> tuple[float, int]:
    """Soma o custo/tokens REAIS dos steps do pipeline. Cada step traz
    ``cost_usd``/``tokens_used`` já calculados no engine (_step_cost_and_tokens).
    Defensivo: step malformado é ignorado (não derruba o débito)."""
    steps = (result or {}).get("pipeline_steps") or []
    cost = 0.0
    tokens = 0
    for s in steps:
        if not isinstance(s, dict):
            continue
        try:
            cost += float(s.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            tokens += int(s.get("tokens_used") or 0)
        except (TypeError, ValueError):
            pass
    return cost, tokens


def _enabled() -> bool:
    """Lê o toggle global em runtime (sem restart — segue o padrão da plataforma)."""
    from app.core.config import get_settings
    return bool(get_settings().api_key_cost_budget_enabled)


async def get_key_budget(api_key_id: str) -> tuple[Optional[float], str]:
    """(orçamento_usd, janela) da key. orçamento None = SEM teto. Janela default
    'month'. Key inexistente → (None, 'month')."""
    from app.core.database import _get_pool
    pool = _get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT cost_budget_usd, cost_budget_window FROM api_keys WHERE id=$1",
            api_key_id,
        )
    if not row:
        return None, "month"
    budget = row["cost_budget_usd"]
    return (
        (float(budget) if budget is not None else None),
        normalize_window(row["cost_budget_window"]),
    )


async def current_spend_usd(api_key_id: str, window: str) -> float:
    """Gasto acumulado (USD) da key na janela corrente. 'total' soma tudo."""
    from app.core.database import _get_pool
    start = _window_start(window, naive_utc_now())
    pool = _get_pool()
    async with pool.acquire() as con:
        if start is None:
            v = await con.fetchval(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM api_key_cost_ledger "
                "WHERE api_key_id=$1",
                api_key_id,
            )
        else:
            v = await con.fetchval(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM api_key_cost_ledger "
                "WHERE api_key_id=$1 AND created_at >= $2",
                api_key_id, start,
            )
    return float(v or 0.0)


async def enforce_budget(api_key_id: Optional[str]) -> None:
    """Pré-checagem de orçamento. Levanta HTTP 402 se a janela corrente já
    atingiu o teto. No-op quando: sem key, toggle OFF, ou key sem orçamento."""
    if not api_key_id or not _enabled():
        return
    budget, window = await get_key_budget(api_key_id)
    if budget is None or budget <= 0:
        return  # sem teto → nunca bloqueia
    spent = await current_spend_usd(api_key_id, window)
    if spent >= budget:
        raise HTTPException(402, {
            "error": "cost_budget_exceeded",
            "reason": "api_key_cost_budget",
            "budget_usd": round(budget, 6),
            "spent_usd": round(spent, 6),
            "window": window,
            "hint": (
                f"Esta API Key atingiu o teto de custo de US$ {budget:.4f} no "
                f"período ({_WINDOW_PT.get(window, window)}) — gasto atual "
                f"US$ {spent:.4f}. Aguarde a renovação da janela ou ajuste o "
                f"orçamento em Configurações → API Keys."
            ),
        })


async def record_cost(
    api_key_id: Optional[str],
    cost_usd: float,
    tokens_used: int,
    pipeline_id: Optional[str] = None,
    interaction_id: Optional[str] = None,
) -> None:
    """Debita o custo real da invocação no ledger da key. Best-effort e gated
    pelo toggle — falha aqui NUNCA derruba a resposta (o invoke já executou)."""
    if not api_key_id or not _enabled():
        return
    try:
        from app.core.database import api_key_cost_ledger_repo
        await api_key_cost_ledger_repo.create({
            "id": str(uuid.uuid4()),
            "api_key_id": api_key_id,
            "pipeline_id": pipeline_id,
            "interaction_id": interaction_id,
            "cost_usd": float(cost_usd or 0.0),
            "tokens_used": int(tokens_used or 0),
            "created_at": naive_utc_now(),
        })
    except Exception as e:  # noqa: BLE001 — ledger nunca pode derrubar o invoke
        logger.warning(
            "event=api_key_cost_record_failed api_key_id=%s error=%s",
            api_key_id, e,
        )
