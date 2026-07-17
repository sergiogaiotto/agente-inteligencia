"""Fila DURÁVEL do loop reflexivo do otimizador (49.0.0, PR4b).

A linha de `optimization_runs` É o job (queued → running → terminal), como
`eval_runs` é do harness. O aceite (POST /optimizer/optimize, gated) cria a
linha 'queued' e devolve 202 + optimization_id; este módulo claima
atomicamente e roda `run_optimization` FORA do request (o loop dispara dezenas
de runs de LLM por minutos-a-horas — impossível sincronamente).

Espelha app/harness/jobs.py (mesmo padrão auditado):
- 'running' órfão de boot anterior → 'interrupted' (NUNCA re-executa — paga
  LLM); só 'queued' retoma;
- cap próprio (optimizer_jobs_max_concurrent, default 1);
- deadline por loop (optimizer_job_timeout_minutes) via asyncio.wait_for;
- kill-switch (optimizer_loop_enabled OFF congela a fila);
- zombie-sweep na carona do reaper (running sem task viva → interrupted);
- shutdown gracioso.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_INTERRUPTED_NOTE = ("Loop de otimização interrompido (restart da aplicação). "
                     "Os runs já avaliados têm custo registrado; re-submeta se "
                     "ainda precisar.")

_active_tasks: set = set()
_active_opt_ids: set = set()


def _pool():
    from app.core.database import _get_pool
    return _get_pool()


def _enabled() -> bool:
    from app.core.config import get_settings
    try:
        return bool(get_settings().optimizer_loop_enabled)
    except Exception:
        return False


def _max_concurrent() -> int:
    from app.core.config import get_settings
    try:
        return max(1, int(get_settings().optimizer_jobs_max_concurrent or 1))
    except Exception:
        return 1


def _timeout_seconds() -> float:
    from app.core.config import get_settings
    try:
        return float(get_settings().optimizer_job_timeout_minutes or 120) * 60.0
    except Exception:
        return 7200.0


def dispatch(opt_id: str) -> bool:
    """Agenda o loop se houver vaga. Sem vaga → 'queued'; a carona do reaper
    despacha quando abrir. Retorna se agendou."""
    if opt_id in _active_opt_ids:
        return False
    if len(_active_opt_ids) >= _max_concurrent():
        return False
    try:
        _active_opt_ids.add(opt_id)
        task = asyncio.create_task(_run_job(opt_id), name=f"opt_job_{opt_id[-8:]}")
    except RuntimeError:
        _active_opt_ids.discard(opt_id)
        logger.warning("event=optimizer_job_no_loop opt_id=%s", opt_id)
        return False
    _active_tasks.add(task)

    def _done(t, oid=opt_id):
        _active_tasks.discard(t)
        _active_opt_ids.discard(oid)

    task.add_done_callback(_done)
    return True


async def _mark(opt_id: str, status: str, error: Optional[str] = None) -> None:
    for attempt in range(3):
        try:
            async with _pool().acquire() as con:
                await con.execute(
                    "UPDATE optimization_runs SET status=$2, "
                    "error=COALESCE($3, error), updated_at=now() "
                    "WHERE id=$1 AND status IN ('queued','running')",
                    opt_id, status, error)
            return
        except Exception as e:
            if attempt == 2:
                logger.error("event=optimizer_job_mark_failed opt_id=%s status=%s "
                             "error=%s", opt_id, status, str(e)[:200])
            else:
                await asyncio.sleep(0.5 * (attempt + 1))


async def _run_job(opt_id: str) -> None:
    """Worker de UM loop: claim atômico → run_optimization com deadline.
    run_optimization persiste o próprio terminal (completed/failed); o worker
    cobre timeout e exceção."""
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "UPDATE optimization_runs SET status='running', updated_at=now() "
            "WHERE id=$1 AND status='queued' RETURNING id", opt_id)
    if not row:
        return  # outro caminho já claimou/terminou — no-op
    from app.optimizer.loop import run_optimization
    deadline = _timeout_seconds()
    # wait_for com margem (review [11]): as RODADAS param em 0.8·deadline e a
    # fase pós-loop (holdout) roda nos 20% restantes DENTRO do deadline; o
    # cancel duro só dispara 120s DEPOIS do deadline (rede de segurança).
    try:
        await asyncio.wait_for(
            run_optimization(opt_id, deadline_s=deadline), timeout=deadline + 120)
    except (TimeoutError, asyncio.TimeoutError):
        logger.error("event=optimizer_job_timeout opt_id=%s", opt_id)
        await _mark(opt_id, "timeout", error="job_timeout")
    except Exception:
        logger.exception("event=optimizer_job_failed opt_id=%s", opt_id)
        await _mark(opt_id, "failed", error="loop_execution_failed")


async def resume_on_boot() -> dict:
    """Boot (lifespan): 'running' órfão → 'interrupted' (nunca re-executa);
    'queued' → despacha até o cap SE o toggle estiver ON."""
    out = {"interrupted": 0, "dispatched": 0}
    async with _pool().acquire() as con:
        res = await con.execute(
            "UPDATE optimization_runs SET status='interrupted', "
            "error='job_interrupted', updated_at=now() WHERE status='running'")
        try:
            out["interrupted"] = int(str(res).split()[-1])
        except Exception:
            pass
        rows = []
        if _enabled():
            rows = await con.fetch(
                "SELECT id FROM optimization_runs WHERE status='queued' "
                "ORDER BY created_at LIMIT $1", _max_concurrent())
    for r in rows:
        if dispatch(r["id"]):
            out["dispatched"] += 1
    if out["interrupted"] or out["dispatched"]:
        logger.info("event=optimizer_jobs_resumed interrupted=%s dispatched=%s",
                    out["interrupted"], out["dispatched"])
    return out


async def sweep_queued() -> dict:
    """Carona no reaper do invoke_jobs (60s): HIGIENE (zumbi 'running' sem
    task viva → interrupted, roda SEMPRE) + DESPACHO ('queued' quando há vaga,
    só com o toggle ON)."""
    out = {"dispatched": 0, "interrupted": 0}
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT id FROM optimization_runs WHERE status='running'")
        zombies = [r["id"] for r in rows if r["id"] not in _active_opt_ids]
        if zombies:
            await con.execute(
                "UPDATE optimization_runs SET status='interrupted', "
                "error='job_interrupted', updated_at=now() "
                "WHERE id = ANY($1) AND status='running'", zombies)
            out["interrupted"] = len(zombies)
            logger.warning("event=optimizer_jobs_zombie count=%s", len(zombies))
        queued = []
        free = _max_concurrent() - len(_active_opt_ids)
        if _enabled() and free > 0:
            queued = await con.fetch(
                "SELECT id FROM optimization_runs WHERE status='queued' "
                "ORDER BY created_at LIMIT $1", free)
    for r in queued:
        if dispatch(r["id"]):
            out["dispatched"] += 1
    return out


async def shutdown_optimizer_jobs(timeout: float = 5.0) -> None:
    """Shutdown gracioso ANTES do close_db: espera/cancela loops ativos e
    marca 'interrupted'."""
    pending = {t for t in _active_tasks if not t.done()}
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    leftovers = sorted(_active_opt_ids)
    if leftovers:
        stragglers = [t for t in _active_tasks if not t.done()]
        for t in stragglers:
            t.cancel()
        if stragglers:
            await asyncio.gather(*stragglers, return_exceptions=True)
        for oid in leftovers:
            await _mark(oid, "interrupted", error="job_interrupted")
        logger.info("event=optimizer_jobs_shutdown_interrupted count=%s",
                    len(leftovers))


def _reset_for_tests() -> None:
    for t in list(_active_tasks):
        try:
            if not t.done():
                t.cancel()
        except RuntimeError:
            pass
    _active_tasks.clear()
    _active_opt_ids.clear()
