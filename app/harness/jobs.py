"""Fila DURÁVEL do harness assíncrono (43.0.0) — PR2 do arco Otimização.

O job É a própria linha de `eval_runs` (status 'queued' → 'running' →
terminal): o aceite (POST /eval-runs/execute com `harness_async_enabled` ON)
cria a linha 'queued' e devolve 202 + eval_id; este módulo claima
atomicamente e roda `run_evaluation` FORA do request (o harness síncrono
bloqueava o request por minutos — e um run de N casos × juiz × RAGAS não
cabe num timeout de proxy). Polling em GET /api/v1/eval-runs/{id}.

Decisões (espelham `invoke_jobs`, com desvios deliberados):
- 'running' órfão de boot anterior vira 'interrupted' e NUNCA re-executa —
  harness paga LLM por caso (mesma postura do invoke). Só 'queued' retoma.
- SEM retenção própria: eval_runs é o HISTÓRICO de métricas (baseline,
  regressão, drift) — apagar seria destruir a série; o reaper do invoke_jobs
  só dá carona ao sweep de despacho ('queued' → running quando abre vaga).
- Cap PRÓPRIO (harness_jobs_max_concurrent, default 1): um run já serializa
  N casos de LLM; 2 runs simultâneos dobram a pressão no provider/breaker.
- Deadline por run (harness_job_timeout_minutes): wait_for CANCELA a
  execução no estouro → 'timeout'. Os custos por caso já registrados
  sobrevivem (são agendados off-path a cada caso, não no fim).
- Kill-switch: harness_async_enabled OFF congela o despacho (queued fica
  queued; nada paga LLM); a higiene de boot (órfão → interrupted) roda sempre.
- Single-process por design (uvicorn 1 worker): o claim atômico
  (UPDATE ... WHERE status='queued' RETURNING) já deixa o caminho pronto
  para multi-worker sem double-run.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_INTERRUPTED_NOTE = (
    "Execução interrompida (restart da aplicação). Os casos já avaliados "
    "tiveram custo registrado; re-submeta o run se ainda precisar."
)

# Retenção in-process (anti-GC + inventário p/ o shutdown).
_active_tasks: set = set()
_active_eval_ids: set = set()


def _pool():
    from app.core.database import _get_pool
    return _get_pool()


def _enabled() -> bool:
    from app.core.config import get_settings
    try:
        return bool(get_settings().harness_async_enabled)
    except Exception:
        return False


def _max_concurrent() -> int:
    from app.core.config import get_settings
    try:
        return max(1, int(get_settings().harness_jobs_max_concurrent or 1))
    except Exception:
        return 1


def _timeout_minutes() -> float:
    from app.core.config import get_settings
    try:
        return float(get_settings().harness_job_timeout_minutes or 60)
    except Exception:
        return 60.0


def dispatch(eval_id: str) -> bool:
    """Agenda a execução se houver vaga (harness_jobs_max_concurrent). Sem
    vaga, o run fica 'queued' e a carona do reaper despacha quando abrir.
    Retorna se agendou."""
    if eval_id in _active_eval_ids:
        return False  # já em voo neste processo
    if len(_active_eval_ids) >= _max_concurrent():
        return False
    try:
        # id entra no set ANTES do claim (mesma janela-zero do invoke_jobs).
        _active_eval_ids.add(eval_id)
        task = asyncio.create_task(_run_job(eval_id), name=f"eval_job_{eval_id[-8:]}")
    except RuntimeError:
        _active_eval_ids.discard(eval_id)
        logger.warning("event=eval_job_no_loop eval_id=%s", eval_id)
        return False
    _active_tasks.add(task)

    def _done(t, eid=eval_id):
        _active_tasks.discard(t)
        _active_eval_ids.discard(eid)

    task.add_done_callback(_done)
    return True


async def _mark(eval_id: str, status: str, error: Optional[str] = None,
                gate_reason: Optional[str] = None) -> None:
    """Marca estado terminal do job com retry — falha silenciosa deixaria o
    cliente pollando um 'running' fantasma (lição do invoke_jobs)."""
    for attempt in range(3):
        try:
            async with _pool().acquire() as con:
                await con.execute(
                    "UPDATE eval_runs SET status=$2, gate_result='skipped', "
                    "error=COALESCE($3, error), gate_reason=COALESCE($4, gate_reason) "
                    "WHERE id=$1 AND status IN ('queued','running')",
                    eval_id, status, error, gate_reason,
                )
            return
        except Exception as e:
            if attempt == 2:
                logger.error("event=eval_job_mark_failed eval_id=%s status=%s error=%s",
                             eval_id, status, str(e)[:200])
            else:
                await asyncio.sleep(0.5 * (attempt + 1))


async def _run_job(eval_id: str) -> None:
    """Worker de UM run: claim atômico → run_evaluation com deadline.
    run_evaluation persiste o próprio terminal (completed/budget_exceeded/
    no_cases/invalid_*) — o worker só cobre timeout e exceção."""
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "UPDATE eval_runs SET status='running' "
            "WHERE id=$1 AND status='queued' "
            "RETURNING id, release_id, gold_version, run_type, agent_id, "
            "pipeline_id, owner_user_id",
            eval_id,
        )
    if not row:
        return  # outro caminho (boot × reaper) já claimou/terminou — no-op
    job = dict(row)
    from app.harness.evaluator import run_evaluation
    try:
        await asyncio.wait_for(
            run_evaluation(
                job["release_id"],
                agent_id=job.get("agent_id"),
                gold_version=job.get("gold_version") or "latest",
                run_type=job.get("run_type") or "baseline",
                pipeline_id=job.get("pipeline_id"),
                owner_user_id=job.get("owner_user_id"),
                eval_id=eval_id,
            ),
            timeout=_timeout_minutes() * 60.0,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.error("event=eval_job_timeout eval_id=%s timeout_min=%s",
                     eval_id, _timeout_minutes())
        await _mark(
            eval_id, "timeout",
            error="job_timeout",
            gate_reason=(
                f"execução excedeu o deadline (harness_job_timeout_minutes="
                f"{_timeout_minutes():g}) e foi cancelada — custos dos casos "
                "já avaliados permanecem registrados no ledger"
            ),
        )
    except Exception:
        # Log com stack; a linha guarda um código estável (o operador vê o
        # gate_reason na UI; detalhes ficam nos logs — LOG_DIR primeiro).
        logger.exception("event=eval_job_failed eval_id=%s", eval_id)
        await _mark(
            eval_id, "failed",
            error="eval_execution_failed",
            gate_reason="falha ao executar o harness — consulte os logs "
                        "(event=eval_job_failed)",
        )


async def resume_on_boot() -> dict:
    """Boot (lifespan, após init_db): 'running' órfão → 'interrupted' (nunca
    re-executa — paga LLM); 'queued' → despacha até o cap SE o toggle estiver
    ON (kill-switch congela o backlog, não só os 202 novos)."""
    out = {"interrupted": 0, "dispatched": 0}
    async with _pool().acquire() as con:
        res = await con.execute(
            "UPDATE eval_runs SET status='interrupted', gate_result='skipped', "
            "error='job_interrupted', gate_reason=$1 WHERE status='running'",
            _INTERRUPTED_NOTE,
        )
        try:
            out["interrupted"] = int(str(res).split()[-1])
        except Exception:
            pass
        rows = []
        if _enabled():
            rows = await con.fetch(
                "SELECT id FROM eval_runs WHERE status='queued' "
                "ORDER BY created_at LIMIT $1", _max_concurrent(),
            )
    for r in rows:
        if dispatch(r["id"]):
            out["dispatched"] += 1
    if out["interrupted"] or out["dispatched"]:
        logger.info("event=eval_jobs_resumed interrupted=%s dispatched=%s",
                    out["interrupted"], out["dispatched"])
    return out


async def sweep_queued() -> dict:
    """Carona no reaper do invoke_jobs (60s). Duas responsabilidades:

    (a) HIGIENE (roda SEMPRE, review [10]/[26]): 'running' de JOB (is_job)
        sem task viva neste processo = zumbi (ex.: o _mark esgotou os retries
        com o DB fora do ar) → 'interrupted'. Runs SÍNCRONOS (is_job=FALSE)
        NUNCA são tocados — podem estar legitimamente em voo dentro de um
        request deste mesmo processo. Single-process → o set é autoridade.
    (b) DESPACHO (só com o toggle ON — kill-switch congela a fila): 'queued'
        → running quando há vaga. Só consulta o banco se há vaga LIVRE
        (review [22]: com cap 1 e um run em voo, o SELECT era 100% inútil).
    """
    out = {"dispatched": 0, "interrupted": 0}
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT id FROM eval_runs WHERE status='running' AND is_job = TRUE")
        zombies = [r["id"] for r in rows if r["id"] not in _active_eval_ids]
        if zombies:
            await con.execute(
                "UPDATE eval_runs SET status='interrupted', gate_result='skipped', "
                "error='job_interrupted', gate_reason=$2 "
                "WHERE id = ANY($1) AND status='running'",
                zombies, _INTERRUPTED_NOTE,
            )
            out["interrupted"] = len(zombies)
            logger.warning("event=eval_jobs_zombie_interrupted count=%s", len(zombies))
        queued = []
        free = _max_concurrent() - len(_active_eval_ids)
        if _enabled() and free > 0:
            queued = await con.fetch(
                "SELECT id FROM eval_runs WHERE status='queued' "
                "ORDER BY created_at LIMIT $1", free,
            )
    for r in queued:
        if dispatch(r["id"]):
            out["dispatched"] += 1
    return out


async def shutdown_eval_jobs(timeout: float = 5.0) -> None:
    """Shutdown gracioso ANTES do close_db: espera os runs ativos até o
    timeout, cancela os restantes e os marca 'interrupted' (o cliente não
    fica pollando um 'running' que nunca vai terminar)."""
    pending = {t for t in _active_tasks if not t.done()}
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    leftovers = sorted(_active_eval_ids)
    if leftovers:
        stragglers = [t for t in _active_tasks if not t.done()]
        for t in stragglers:
            t.cancel()
        if stragglers:
            # Aguarda o cancel aterrissar ANTES do mark — a task morrendo não
            # pode correr contra o close_db (mesma razão do invoke_jobs).
            await asyncio.gather(*stragglers, return_exceptions=True)
        for eid in leftovers:
            await _mark(eid, "interrupted", error="job_interrupted",
                        gate_reason=_INTERRUPTED_NOTE)
        logger.info("event=eval_jobs_shutdown_interrupted count=%s", len(leftovers))


def _reset_for_tests() -> None:
    """Higiene de estado módulo-level entre testes (lição do breaker #566)."""
    for t in list(_active_tasks):
        try:
            if not t.done():
                t.cancel()
        except RuntimeError:
            pass
    _active_tasks.clear()
    _active_eval_ids.clear()
