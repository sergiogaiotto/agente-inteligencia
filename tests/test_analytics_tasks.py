"""Fila off-path compartilhada de analytics (33.7.0) — app/core/analytics_tasks.

Extraída de pipelines.py p/ ser reusada por agents/workspace no SSOT de custo.
Garante o invariante: schedule é fire-and-forget, drain aguarda, e uma falha na
tarefa é ENGOLIDA (nunca propaga ao caminho de resposta).
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_schedule_roda_off_path_e_drain_aguarda():
    from app.core.analytics_tasks import schedule_analytics, drain_analytics

    ran = []

    async def _work():
        ran.append(1)

    schedule_analytics(_work())
    # ainda pode não ter rodado (fire-and-forget); drain garante a conclusão.
    n = await drain_analytics(timeout=2.0)
    assert ran == [1]
    assert n >= 1


@pytest.mark.asyncio
async def test_falha_na_tarefa_e_engolida():
    from app.core.analytics_tasks import schedule_analytics, drain_analytics

    async def _boom():
        raise RuntimeError("erro proposital")

    schedule_analytics(_boom())        # não propaga aqui
    await drain_analytics(timeout=2.0)  # nem aqui (o _safe_analytics engole)


@pytest.mark.asyncio
async def test_drain_vazio_retorna_zero():
    from app.core.analytics_tasks import drain_analytics
    assert await drain_analytics(timeout=0.1) == 0


def test_pipelines_reexporta_nomes_p_retrocompat():
    # main.py faz `from app.routes.pipelines import drain_invoke_analytics`.
    from app.routes.pipelines import drain_invoke_analytics, _schedule_analytics
    from app.core.analytics_tasks import drain_analytics, schedule_analytics
    assert drain_invoke_analytics is drain_analytics
    assert _schedule_analytics is schedule_analytics
