"""Dispatcher assíncrono do Verifier — production sampling §14.2.

Roda judge multi-dim em background numa amostra das interações reais.
Não bloqueia a resposta ao usuário; persiste em verifications via
Verifier.persist (já existente).

Sampling: hash determinístico do interaction_id → bucket [0, 1).
Backpressure: drop quando o set de tasks pendentes atinge o cap.
Stats: contadores in-process; cross-worker requer Prometheus/Redis (futuro).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

logger = logging.getLogger(__name__)

# Estado módulo-level. Set para evitar GC das tasks; callbacks
# fazem cleanup quando completam.
_pending_tasks: set[asyncio.Task] = set()

# Contadores in-process. Atomicidade garantida pela single-threaded
# event loop do asyncio.
_stats: dict[str, int] = {
    "sampled": 0,    # tasks dispatched (não inclui drops)
    "completed": 0,  # tasks que terminaram com sucesso
    "failed": 0,     # tasks que terminaram com exceção
    "dropped": 0,    # samples descartados por backpressure
}


def should_sample(interaction_id: str | None, rate: float) -> bool:
    """Sampling determinístico por hash. Mesma interaction_id sempre vai
    para o mesmo destino — útil pra debug e estabilidade entre deploys.

    rate=0 → sempre False; rate>=1 → sempre True; intermediate → SHA256
    dos primeiros 8 bytes do interaction_id, normalizado para [0, 1),
    comparado com rate.

    Sem interaction_id → False (defensive: não amostrar sem identificador).
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    if not interaction_id:
        return False
    digest = hashlib.sha256(interaction_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate


def stats_snapshot() -> dict[str, int]:
    """Snapshot dos counters + pending. Read-only — caller não deve mutar."""
    return {**_stats, "pending": len(_pending_tasks)}


def dispatch(
    *,
    draft: str,
    evidences: list,
    output_contract: str,
    guardrails: str,
    user_question: str,
    profile: str,
    interaction_id: str,
    max_concurrent: int,
    # Auditoria (24.10.0): dono do julgamento — vai pras colunas novas.
    agent_id: str | None = None,
    pipeline_id: str | None = None,
) -> bool:
    """Cria task em background para verificar o draft.

    Retorna True se a task foi criada; False se foi descartada por
    backpressure (set já no cap). Falha de criação não levanta —
    chamador continua.
    """
    if len(_pending_tasks) >= max_concurrent:
        _stats["dropped"] += 1
        logger.info(
            f"async verifier dropped (pending={len(_pending_tasks)} >= cap={max_concurrent}) "
            f"interaction_id={interaction_id}"
        )
        return False

    try:
        task = asyncio.create_task(
            _run_verification(
                draft=draft, evidences=evidences,
                output_contract=output_contract, guardrails=guardrails,
                user_question=user_question, profile=profile,
                interaction_id=interaction_id,
                agent_id=agent_id, pipeline_id=pipeline_id,
            ),
            name=f"verifier_async_{(interaction_id or 'noid')[:8]}",
        )
    except RuntimeError as e:
        # Sem event loop ativo (chamada fora de async context). Não amostra.
        logger.warning(f"async verifier dispatch falhou (no loop?): {e}")
        return False

    _pending_tasks.add(task)
    task.add_done_callback(_on_task_done)
    _stats["sampled"] += 1
    return True


async def _run_verification(
    *,
    draft: str,
    evidences: list,
    output_contract: str,
    guardrails: str,
    user_question: str,
    profile: str,
    interaction_id: str,
    agent_id: str | None = None,
    pipeline_id: str | None = None,
) -> None:
    """Corpo da task. Falha aqui propaga para o callback contar como failed."""
    # Import lazy: evita ciclo verifier → dispatcher → verifier no boot.
    from app.verifier import verifier as _verifier
    try:
        await _verifier.verify(
            draft=draft,
            evidences=evidences,
            output_contract=output_contract,
            guardrails=guardrails,
            user_question=user_question,
            profile=profile,
            interaction_id=interaction_id,
            persist=True,  # vai pra verifications table
            agent_id=agent_id,
            pipeline_id=pipeline_id,
        )
    except Exception as e:
        logger.warning(
            f"async verification falhou interaction_id={interaction_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise


def _on_task_done(task: asyncio.Task) -> None:
    """Callback que: (1) tira a task do set (libera GC), (2) atualiza counter."""
    _pending_tasks.discard(task)
    if task.cancelled():
        # Cancelled em shutdown — não conta como failed nem completed.
        return
    if task.exception() is not None:
        _stats["failed"] += 1
    else:
        _stats["completed"] += 1


async def drain(timeout: float = 5.0) -> int:
    """Aguarda tasks pendentes por até `timeout` segundos. Retorna o
    número de tasks que ainda estavam pendentes quando o timeout estourou
    (0 = todas drenadas no prazo).

    Chamado no lifespan do FastAPI antes do close_db.
    """
    if not _pending_tasks:
        return 0

    pending = list(_pending_tasks)
    logger.info(f"draining {len(pending)} async verifier tasks (timeout={timeout}s)")
    try:
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            logger.warning(
                f"shutdown timeout: {len(still_pending)} async verifier tasks "
                f"ainda pendentes — abandonando"
            )
        return len(still_pending)
    except Exception as e:
        logger.warning(f"drain falhou: {type(e).__name__}: {e}")
        return len(_pending_tasks)


def _reset_for_tests() -> None:
    """Reseta estado interno. Uso só em smoke/teste — não chamar em runtime."""
    _pending_tasks.clear()
    for k in _stats:
        _stats[k] = 0
