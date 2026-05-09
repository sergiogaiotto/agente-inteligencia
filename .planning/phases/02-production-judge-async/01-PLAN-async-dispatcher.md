---
wave: 1
depends_on: []
files_modified:
  - app/verifier/async_dispatcher.py (novo)
autonomous: true
estimated_diff_lines: ~150
---

# Plan 01 — Async dispatcher do Verifier

## Objective

Criar módulo `app/verifier/async_dispatcher.py` autocontido com:
- Função pura de sampling determinístico por hash do `interaction_id`.
- Função `dispatch(...)` que cria task asyncio, registra no set global, garante cleanup.
- Backpressure: drop quando atingir `max_concurrent_jobs`.
- Counters in-process: `sampled`, `completed`, `failed`, `dropped`, mais `pending` derivado do set.
- Função `drain(timeout)` para shutdown limpo.

## Why

Encapsular a lógica de dispatch num módulo separado:
1. Mantém `engine.py` focado em FSM e LLM call.
2. Permite testar o dispatcher isoladamente.
3. Reusável caso outros componentes queiram dispatch async no futuro.
4. Estado global fica num lugar só, fácil de raciocinar sobre lifecycle.

## Tasks

<task id="1" type="new">
<file>app/verifier/async_dispatcher.py</file>
<change>
Criar módulo do zero com:

```python
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
from typing import Optional

logger = logging.getLogger(__name__)

# Estado módulo-level. Um set para evitar GC das tasks; callbacks
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
    
    task = asyncio.create_task(
        _run_verification(
            draft=draft, evidences=evidences,
            output_contract=output_contract, guardrails=guardrails,
            user_question=user_question, profile=profile,
            interaction_id=interaction_id,
        ),
        name=f"verifier_async_{interaction_id[:8] if interaction_id else 'noid'}",
    )
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
) -> None:
    """Corpo da task. Falha aqui não derruba nada — só telemetria."""
    try:
        # Import lazy: evita ciclo verifier → dispatcher → verifier no boot.
        from app.verifier import verifier as _verifier
        await _verifier.verify(
            draft=draft,
            evidences=evidences,
            output_contract=output_contract,
            guardrails=guardrails,
            user_question=user_question,
            profile=profile,
            interaction_id=interaction_id,
            persist=True,  # vai pra verifications table
        )
    except Exception as e:
        # Log apenas — task async não pode propagar pro request.
        logger.warning(
            f"async verification falhou interaction_id={interaction_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise  # propaga para o callback contar como failed


def _on_task_done(task: asyncio.Task) -> None:
    """Callback que: (1) tira a task do set (libera GC), (2) atualiza counter."""
    _pending_tasks.discard(task)
    if task.cancelled():
        # Cancelled in shutdown — não conta como failed.
        return
    exc = task.exception()
    if exc is not None:
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


# ─── Test hook: reseta estado (uso só em smoke/teste) ─────────────
def _reset_for_tests() -> None:
    _pending_tasks.clear()
    for k in _stats:
        _stats[k] = 0
```
</change>
<acceptance>
- Módulo importa sem erro (sem dependência circular).
- `should_sample("abc-123", 0.0)` → False; `should_sample("abc-123", 1.0)` → True.
- `should_sample(None, 0.5)` → False (defensive).
- `should_sample("same-id", 0.5)` é determinístico (chamada N vezes dá mesmo resultado).
- Sobre 10k IDs aleatórios com rate=0.1, taxa real cai entre 9% e 11% (lei dos grandes números).
- `stats_snapshot()` retorna dict com 5 chaves: sampled, completed, failed, dropped, pending.
- `_reset_for_tests()` zera tudo.
</acceptance>
</task>

## Verification

- [ ] Smoke `python -c "from app.verifier.async_dispatcher import should_sample; ..."` valida sampling determinístico e distribuição estatística.
- [ ] Smoke validando que `dispatch` cria task e o callback faz cleanup (task.discard do set após completar).
- [ ] Smoke validando que dispatch acima do cap retorna False e incrementa `dropped`.

## must_haves

- Módulo é autocontido — não importa nada do `engine.py`.
- Lazy import do `verifier` para evitar ciclo no boot.
- Cleanup do set de tasks é garantido (callback registrado).
- Drain idempotente (chamar 2x não quebra).

## Notes

- Por que não `asyncio.Semaphore`? Semaphore + drop com `wait_for(timeout=0)` adiciona `TimeoutError` no caminho feliz; cap por `len(_pending_tasks)` é mais legível e tem o mesmo efeito prático.
- Por que `task.add_done_callback` em vez de `try/finally` no `_run_verification`? Callback é chamado mesmo em cancelamento (`task.cancelled()`); finally bloco roda só quando a coroutine de fato corre — em cancelamento durante `await` antes do try, não roda.
- Stats são `int` simples. Soma atômica garantida por single-thread event loop. Não usar `+=` em código multi-thread; este código é asyncio puro.
