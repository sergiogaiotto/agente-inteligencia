"""Dispatcher assíncrono do Verifier — production sampling §14.2 + fila DURÁVEL (Onda 6).

Roda judge multi-dim em background numa amostra das interações reais. Não bloqueia
a resposta ao usuário; persiste em `verifications` via Verifier.verify.

Sampling: hash determinístico do interaction_id → bucket [0, 1).
Backpressure: quando o set de tasks pendentes atinge o cap, o sample NÃO é mais
descartado (era perdido) — vira uma linha 'pending' em `verifier_jobs` que o
boot-resume roda depois.

DURABILIDADE (33.16.0): cada dispatch persiste um `verifier_jobs` (running→done)
ANTES/durante a execução. A fila em MEMÓRIA (`_pending_tasks`) não sobrevivia a
restart; agora, no boot, `resume_jobs()` re-despacha os 'pending' e os 'running'
órfãos (processo anterior morreu no meio). Falha re-tenta até
`verifier_job_max_attempts` e então vira 'dead' (dead-letter auditável).

Stats: contadores in-process; cross-worker requer Prometheus/Redis (futuro).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# Estado módulo-level. Set para evitar GC das tasks; callbacks fazem cleanup.
_pending_tasks: set[asyncio.Task] = set()

_stats: dict[str, int] = {
    "sampled": 0,    # tasks dispatched (não inclui drops)
    "completed": 0,  # tasks que terminaram com sucesso
    "failed": 0,     # tasks que terminaram com exceção
    "dropped": 0,    # samples que estouraram o backpressure (persistidos como pending)
    "resumed": 0,    # jobs re-despachados no boot (durabilidade)
}


def should_sample(interaction_id: str | None, rate: float) -> bool:
    """Sampling determinístico por hash. Mesma interaction_id sempre vai para o
    mesmo destino — útil pra debug e estabilidade entre deploys."""
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


# ───────────────────────────────────────────────────────────────
# Durabilidade — verifier_jobs (persiste o ciclo do job)
# ───────────────────────────────────────────────────────────────

def _serialize_evidences(evidences: list | None) -> list[dict]:
    """Reduz cada evidência aos 3 campos que o juiz usa (getattr no MultiDimJudge)
    p/ caber no payload JSON. Tolera objetos (atributos) OU dicts."""
    out: list[dict] = []
    for e in (evidences or []):
        if isinstance(e, dict):
            g = e.get
        else:
            g = lambda k, _e=e: getattr(_e, k, None)  # noqa: E731
        out.append({
            "relevance_score": g("relevance_score"),
            "source_name": g("source_name"),
            "snippet_text": g("snippet_text"),
        })
    return out


def _deserialize_evidences(dicts: list | None) -> list:
    """Reconstrói as evidências como objetos leves (o juiz faz getattr)."""
    return [SimpleNamespace(**d) for d in (dicts or []) if isinstance(d, dict)]


def _build_payload(**kw) -> str:
    """Serializa os args de verify() (com evidências reduzidas) p/ o payload."""
    return json.dumps({
        "draft": kw.get("draft", ""),
        "evidences": _serialize_evidences(kw.get("evidences")),
        "output_contract": kw.get("output_contract", ""),
        "guardrails": kw.get("guardrails", ""),
        "user_question": kw.get("user_question", ""),
        "profile": kw.get("profile", "standard"),
        "interaction_id": kw.get("interaction_id"),
        "agent_id": kw.get("agent_id"),
        "pipeline_id": kw.get("pipeline_id"),
    })


async def _job_start(job_id: str, interaction_id, agent_id, pipeline_id, payload_json: str) -> None:
    """Upsert: cria o job 'running' (attempts=1) ou, se já existe (resume/retry),
    vira 'running' e incrementa attempts. Best-effort — durabilidade não bloqueia."""
    try:
        from app.core.database import _get_pool
        async with _get_pool().acquire() as con:
            await con.execute(
                "INSERT INTO verifier_jobs (id, interaction_id, agent_id, pipeline_id, payload, status, attempts) "
                "VALUES ($1,$2,$3,$4,$5,'running',1) "
                "ON CONFLICT (id) DO UPDATE SET status='running', "
                "attempts=verifier_jobs.attempts+1, updated_at=now()",
                job_id, interaction_id, agent_id, pipeline_id, payload_json,
            )
    except Exception as e:
        logger.warning("verifier_job start persist falhou id=%s: %s", job_id, str(e)[:150])


async def _job_done(job_id: str) -> None:
    try:
        from app.core.database import _get_pool
        async with _get_pool().acquire() as con:
            await con.execute(
                "UPDATE verifier_jobs SET status='done', last_error=NULL, updated_at=now() WHERE id=$1",
                job_id,
            )
    except Exception as e:
        logger.warning("verifier_job done persist falhou id=%s: %s", job_id, str(e)[:150])


async def _job_failed(job_id: str, err: str) -> None:
    """dead se esgotou as tentativas (attempts >= max), senão pending (o boot-
    resume re-despacha). A decisão dead/pending é do Postgres (CASE), atômica."""
    try:
        from app.core.config import get_settings
        from app.core.database import _get_pool
        mx = int(get_settings().verifier_job_max_attempts or 3)
        async with _get_pool().acquire() as con:
            await con.execute(
                "UPDATE verifier_jobs SET status = CASE WHEN attempts >= $2 THEN 'dead' ELSE 'pending' END, "
                "last_error=$3, updated_at=now() WHERE id=$1",
                job_id, mx, str(err)[:500],
            )
    except Exception as e:
        logger.warning("verifier_job fail persist falhou id=%s: %s", job_id, str(e)[:150])


async def _job_persist_pending(job_id: str, interaction_id, agent_id, pipeline_id, payload_json: str) -> None:
    """Persiste um sample DROPADO (backpressure) como 'pending' SEM rodar — o
    boot-resume pega depois. Não sobrescreve um job já existente."""
    try:
        from app.core.database import _get_pool
        async with _get_pool().acquire() as con:
            await con.execute(
                "INSERT INTO verifier_jobs (id, interaction_id, agent_id, pipeline_id, payload, status) "
                "VALUES ($1,$2,$3,$4,$5,'pending') ON CONFLICT (id) DO NOTHING",
                job_id, interaction_id, agent_id, pipeline_id, payload_json,
            )
    except Exception as e:
        logger.warning("verifier_job pending persist falhou id=%s: %s", job_id, str(e)[:150])


# ───────────────────────────────────────────────────────────────
# Dispatch + execução
# ───────────────────────────────────────────────────────────────

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
    agent_id: str | None = None,
    pipeline_id: str | None = None,
) -> bool:
    """Cria task em background para verificar o draft, persistindo o job em
    verifier_jobs (durável). Retorna True se a task foi criada; False se estourou
    o backpressure (nesse caso persiste 'pending' p/ o boot-resume, não perde)."""
    job_id = f"vj_{uuid.uuid4().hex[:16]}"
    fields = dict(
        draft=draft, evidences=evidences, output_contract=output_contract,
        guardrails=guardrails, user_question=user_question, profile=profile,
        interaction_id=interaction_id, agent_id=agent_id, pipeline_id=pipeline_id,
    )

    if len(_pending_tasks) >= max_concurrent:
        _stats["dropped"] += 1
        logger.info(
            f"async verifier no cap (pending={len(_pending_tasks)} >= {max_concurrent}) "
            f"— persistindo como pending id={job_id} interaction_id={interaction_id}"
        )
        try:  # durabilidade: não perde o sample — vira pending p/ o resume
            asyncio.create_task(
                _job_persist_pending(job_id, interaction_id, agent_id, pipeline_id, _build_payload(**fields))
            )
        except RuntimeError:
            pass
        return False

    try:
        task = asyncio.create_task(
            _run_verification(job_id=job_id, **fields),
            name=f"verifier_async_{(interaction_id or 'noid')[:8]}",
        )
    except RuntimeError as e:
        logger.warning(f"async verifier dispatch falhou (no loop?): {e}")
        return False

    _pending_tasks.add(task)
    task.add_done_callback(_on_task_done)
    _stats["sampled"] += 1
    return True


async def _run_verification(
    *,
    job_id: str,
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
    """Corpo da task: marca o job 'running', roda verify, marca 'done'/'pending'/
    'dead'. Propaga a exceção p/ o callback contar failed (o job já foi persistido)."""
    payload_json = _build_payload(
        draft=draft, evidences=evidences, output_contract=output_contract,
        guardrails=guardrails, user_question=user_question, profile=profile,
        interaction_id=interaction_id, agent_id=agent_id, pipeline_id=pipeline_id,
    )
    await _job_start(job_id, interaction_id, agent_id, pipeline_id, payload_json)

    from app.verifier import verifier as _verifier  # lazy: evita ciclo no boot
    try:
        await _verifier.verify(
            draft=draft, evidences=evidences, output_contract=output_contract,
            guardrails=guardrails, user_question=user_question, profile=profile,
            interaction_id=interaction_id, persist=True,
            agent_id=agent_id, pipeline_id=pipeline_id,
        )
    except Exception as e:
        await _job_failed(job_id, f"{type(e).__name__}: {e}")
        logger.warning(
            f"async verification falhou id={job_id} interaction_id={interaction_id}: "
            f"{type(e).__name__}: {e}"
        )
        raise
    await _job_done(job_id)


def _on_task_done(task: asyncio.Task) -> None:
    """(1) tira a task do set (libera GC), (2) atualiza counter in-process."""
    _pending_tasks.discard(task)
    if task.cancelled():
        return  # cancelled em shutdown — não conta
    if task.exception() is not None:
        _stats["failed"] += 1
    else:
        _stats["completed"] += 1


async def resume_jobs(batch: int = 20) -> int:
    """Boot-resume da fila durável: reseta os 'running' órfãos (o processo
    anterior morreu no meio) → 'pending', e re-despacha os 'pending' com
    attempts < max como tasks duráveis (até `batch`). Retorna o nº re-despachado.

    Chamado no lifespan do FastAPI DEPOIS do init_db (pool aberto) e ANTES de
    servir requests → não há tasks vivas ainda, então nenhum 'running' é legítimo
    (single-flight, sem risco de double-processing). Best-effort: nunca derruba
    o boot. O excedente (> batch) fica pending pro próximo boot."""
    try:
        from app.core.config import get_settings
        from app.core.database import _get_pool
        mx = int(get_settings().verifier_job_max_attempts or 3)
        async with _get_pool().acquire() as con:
            # órfãos 'running' do processo anterior → pending (re-despacháveis)
            await con.execute(
                "UPDATE verifier_jobs SET status='pending', updated_at=now() WHERE status='running'"
            )
            rows = await con.fetch(
                "SELECT id, payload FROM verifier_jobs "
                "WHERE status='pending' AND attempts < $1 ORDER BY created_at LIMIT $2",
                mx, batch,
            )
    except Exception as e:
        logger.warning("resume_jobs: consulta falhou: %s", str(e)[:150])
        return 0

    n = 0
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
            task = asyncio.create_task(
                _run_verification(
                    job_id=r["id"],
                    draft=p.get("draft", ""),
                    evidences=_deserialize_evidences(p.get("evidences")),
                    output_contract=p.get("output_contract", ""),
                    guardrails=p.get("guardrails", ""),
                    user_question=p.get("user_question", ""),
                    profile=p.get("profile", "standard"),
                    interaction_id=p.get("interaction_id"),
                    agent_id=p.get("agent_id"),
                    pipeline_id=p.get("pipeline_id"),
                ),
                name=f"verifier_resume_{str(r['id'])[:8]}",
            )
            _pending_tasks.add(task)
            task.add_done_callback(_on_task_done)
            n += 1
        except Exception as e:
            logger.warning("resume_jobs: re-dispatch do job %s falhou: %s", r["id"], str(e)[:150])
    if n:
        _stats["resumed"] += n
        logger.info("verifier_jobs: %d job(s) do juiz re-despachado(s) no boot", n)
    return n


async def sweep_pending(batch: int = 20) -> int:
    """Sweep PERIÓDICO da fila (35.3.0, fast-follow do #584): re-despacha
    'pending' acumulado ENTRE boots — antes, samples dropados por backpressure
    e retries de jobs falhos só rodavam no próximo restart. Pega carona no
    reaper do invoke_jobs (o 1º loop periódico do app, #590).

    ≠ resume_jobs: NÃO reseta 'running' (fora do boot há tasks legítimas em
    voo — o reset cego causaria double-processing). Guardas:
    - só linhas paradas há 2+ min (updated_at) — cobre a janela dispatch→
      _job_start de um pending recém-criado que JÁ tem task;
    - respeita o slot in-process (max_concurrent - tasks vivas).
    Best-effort: nunca propaga exceção ao reaper."""
    try:
        from app.core.config import get_settings
        from app.core.database import _get_pool
        settings = get_settings()
        mx = int(settings.verifier_job_max_attempts or 3)
        # `or` engoliria cap=0 (falsy) — 0 é válido e significa "sweep desligado"
        raw_cap = settings.verifier_max_concurrent_jobs
        cap = int(raw_cap) if raw_cap is not None else 20
        slots = max(0, cap - len(_pending_tasks))
        if slots == 0:
            return 0
        async with _get_pool().acquire() as con:
            rows = await con.fetch(
                "SELECT id, payload FROM verifier_jobs "
                "WHERE status='pending' AND attempts < $1 "
                "AND updated_at < now() - interval '2 minutes' "
                "ORDER BY created_at LIMIT $2",
                mx, min(batch, slots),
            )
    except Exception as e:
        logger.warning("sweep_pending: consulta falhou: %s", str(e)[:150])
        return 0

    n = 0
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
            task = asyncio.create_task(
                _run_verification(
                    job_id=r["id"],
                    draft=p.get("draft", ""),
                    evidences=_deserialize_evidences(p.get("evidences")),
                    output_contract=p.get("output_contract", ""),
                    guardrails=p.get("guardrails", ""),
                    user_question=p.get("user_question", ""),
                    profile=p.get("profile", "standard"),
                    interaction_id=p.get("interaction_id"),
                    agent_id=p.get("agent_id"),
                    pipeline_id=p.get("pipeline_id"),
                ),
                name=f"verifier_sweep_{str(r['id'])[:8]}",
            )
            _pending_tasks.add(task)
            task.add_done_callback(_on_task_done)
            n += 1
        except Exception as e:
            logger.warning("sweep_pending: re-dispatch do job %s falhou: %s", r["id"], str(e)[:150])
    if n:
        _stats["resumed"] += n
        logger.info("verifier_jobs: sweep periódico re-despachou %d job(s)", n)
    return n


async def drain(timeout: float = 5.0) -> int:
    """Aguarda as tasks pendentes por até `timeout` s. Retorna quantas seguem
    pendentes no fim (0 = todas drenadas). Chamado no lifespan antes do close_db.
    As que não terminarem ficam 'running' no banco → o próximo boot as resume."""
    if not _pending_tasks:
        return 0
    pending = list(_pending_tasks)
    logger.info(f"draining {len(pending)} async verifier tasks (timeout={timeout}s)")
    try:
        _done, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            logger.warning(
                f"shutdown timeout: {len(still_pending)} async verifier tasks ainda "
                f"pendentes — abandonando (ficam 'running' → resume no próximo boot)"
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
