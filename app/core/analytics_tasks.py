"""Fila OFF-PATH de escritas de analytics/custo (fire-and-forget).

Compartilhada por pipelines/agents/workspace (33.7.0 extraiu de pipelines.py):
o invoke NUNCA espera por estas escritas (invariante de desempenho — auditoria,
atribuição, débito, ledger de custo SSOT saem do caminho de resposta). Drenada no
shutdown (best-effort). Perda rara em crash é aceitável para alto volume.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_ANALYTICS_TASKS: set = set()


def schedule_analytics(coro) -> None:
    """Agenda uma escrita de analytics fora do caminho de resposta (fire-and-forget).
    No-op se não houver event loop (não ocorre em rota async)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_safe_analytics(coro))
    _ANALYTICS_TASKS.add(task)
    task.add_done_callback(_ANALYTICS_TASKS.discard)


async def _safe_analytics(coro) -> None:
    try:
        await coro
    except Exception as e:
        logger.warning("event=analytics_task_failed error=%s", str(e)[:200], exc_info=True)


async def drain_analytics(timeout: float = 5.0) -> int:
    """Drena as escritas de analytics pendentes no shutdown (best-effort)."""
    pend = list(_ANALYTICS_TASKS)
    if not pend:
        return 0
    done, _ = await asyncio.wait(pend, timeout=timeout)
    return len(done)
