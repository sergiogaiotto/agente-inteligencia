"""Playground — histórico de execuções persistido POR USUÁRIO (Feature 1 + thread).

O histórico do console (`mesh_playground.html`) é persistido no servidor, escopado
ao usuário autenticado — sobrevive a troca de máquina e fica auditável. Duas camadas:

- ``playground_runs``        : o CARTÃO escalar (pipeline, mensagem, verbosidade,
                               status, tamanho, duração) — listado em ``GET /runs``.
- ``playground_run_threads`` : a THREAD completa de UMA execução ({result, timings,
                               http}), guardada à parte e carregada SÓ sob demanda em
                               ``GET /runs/{id}`` — restaura todos os painéis (Resposta/
                               Tempo/Trace/HTTP) ao clicar no histórico, sem re-rodar.

A thread pode ser grande (Debug traz trace/SQL/custo) → guarda de tamanho na gravação
e tabela separada (fora do ``SELECT *`` da listagem). ``thread_json`` é TEXT com
``json.dumps`` (evita o footgun asyncpg+JSONB no Repository genérico).

Auth: ``require_user`` (cookie OU ``X-API-Key``). Tudo escopado a ``user_id``.

Endpoints:
- ``POST   /api/v1/playground/runs``        grava uma execução (+thread opcional)
- ``GET    /api/v1/playground/runs?limit``  últimos cartões do user (sem thread)
- ``GET    /api/v1/playground/runs/{id}``   cartão + thread completa (restaurar)
- ``DELETE /api/v1/playground/runs``        limpa tudo do user
- ``DELETE /api/v1/playground/runs/{id}``   remove uma (404 se não for do dono)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from app.core.datetime_utils import naive_utc_now

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_user
from app.core.database import playground_runs_repo, playground_threads_repo
from app.models.schemas import PlaygroundRunCreate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/playground", tags=["playground"])

#: histórico mantido por usuário (folga sobre o cap de 20 do localStorage).
_MAX_KEEP = 50
#: teto da thread persistida (Debug pode trazer trace/SQL grandes). Acima disso, só
#: o cartão é gravado — o clique restaura a requisição e avisa que não há detalhe.
_MAX_THREAD_BYTES = 512 * 1024


def _serialize(r: dict) -> dict:
    created = r.get("created_at")
    return {
        "id": r.get("id"),
        "pipeline_id": r.get("pipeline_id"),
        "pipeline_name": r.get("pipeline_name"),
        "message": r.get("message"),
        "verbosity": r.get("verbosity"),
        "status": r.get("status"),
        "size_bytes": r.get("size_bytes"),
        "duration_ms": r.get("duration_ms"),
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
    }


async def _prune(user_id: str, keep: int = _MAX_KEEP) -> None:
    """Mantém só as ``keep`` execuções mais recentes do usuário (bound de crescimento).

    ``find_all`` ordena por ``created_at`` DESC; tudo após o índice ``keep`` é o
    excedente mais antigo. O delete da ``playground_runs`` leva a thread junto (FK
    ON DELETE CASCADE). Em regime normal apaga 0–1 linha por inserção.
    """
    rows = await playground_runs_repo.find_all(user_id=user_id, limit=keep + 200)
    for r in rows[keep:]:
        rid = r.get("id")
        if rid:
            await playground_runs_repo.delete(rid)


@router.post("/runs", status_code=201)
async def create_run(data: PlaygroundRunCreate, user: dict = Depends(require_user)):
    """Persiste UMA execução do Playground no histórico do usuário (+thread opcional).

    Chamado de forma OTIMISTA pela UI. ``created_at`` é NAIVE (coluna TIMESTAMP) —
    armadilha asyncpg conhecida. A thread (se vier e couber no teto) é gravada na
    tabela irmã. Após gravar, PODA o histórico para ``<= _MAX_KEEP`` (50).
    """
    run_id = str(uuid.uuid4())
    row = {
        "id": run_id,
        "user_id": user["id"],
        "pipeline_id": (data.pipeline_id or None),
        "pipeline_name": (data.pipeline_name or None),
        "message": (data.message or "")[:2000],
        "verbosity": (data.verbosity or None),
        "status": (data.status or None),
        "size_bytes": data.size_bytes,
        "duration_ms": data.duration_ms,
        "created_at": naive_utc_now(),  # NAIVE — coluna TIMESTAMP (não aware!)
    }
    await playground_runs_repo.create(row)

    # Thread completa (TEXT/json.dumps) — só se couber no teto; senão fica só o cartão.
    stored_thread = False
    if data.thread is not None:
        try:
            blob = json.dumps(data.thread, ensure_ascii=False, default=str)
        except Exception:
            blob = None
        if blob and len(blob.encode("utf-8")) <= _MAX_THREAD_BYTES:
            # Best-effort: falha ao gravar a thread NÃO derruba o POST (o cartão já
            # foi salvo) — degrada p/ has_thread=False, igual ao estouro de tamanho.
            try:
                await playground_threads_repo.create({
                    "id": run_id,
                    "thread_json": blob,
                    "created_at": naive_utc_now(),
                })
                stored_thread = True
            except Exception as e:
                logger.warning(
                    "playground.thread.persist_failed",
                    extra={"event": "playground.thread.persist_failed", "run_id": run_id,
                           "error_type": type(e).__name__, "error": str(e)[:200]},
                )

    await _prune(user["id"])
    out = _serialize(row)
    out["has_thread"] = stored_thread
    return out


@router.get("/runs")
async def list_runs(
    user: dict = Depends(require_user),
    limit: int = Query(20, ge=1, le=100),
):
    """Últimos CARTÕES do usuário (sem a thread — leve)."""
    rows = await playground_runs_repo.find_all(user_id=user["id"], limit=limit)
    return {"runs": [_serialize(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(require_user)):
    """Cartão + THREAD completa de uma execução (restaurar os painéis ao clicar).

    404 se não existe OU não é do usuário. ``thread`` vem ``null`` se não foi gravada
    (execução antiga, sem detalhe, ou que estourou o teto) — a UI cai na requisição.
    """
    row = await playground_runs_repo.find_by_id(run_id)
    if not row or row.get("user_id") != user["id"]:
        raise HTTPException(404, "Execução não encontrada")
    out = _serialize(row)
    thread = None
    t = await playground_threads_repo.find_by_id(run_id)
    if t and t.get("thread_json"):
        try:
            thread = json.loads(t["thread_json"])
        except Exception:
            thread = None
    out["thread"] = thread
    return out


@router.delete("/runs")
async def clear_runs(user: dict = Depends(require_user)):
    """Limpa TODO o histórico do usuário (threads vão junto via CASCADE)."""
    rows = await playground_runs_repo.find_all(user_id=user["id"], limit=1000)
    deleted = 0
    for r in rows:
        rid = r.get("id")
        if rid and await playground_runs_repo.delete(rid):
            deleted += 1
    return {"deleted": deleted}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str, user: dict = Depends(require_user)):
    """Remove UMA execução. 404 se não existe OU não é do usuário (sem vazar)."""
    row = await playground_runs_repo.find_by_id(run_id)
    if not row or row.get("user_id") != user["id"]:
        raise HTTPException(404, "Execução não encontrada")
    await playground_runs_repo.delete(run_id)  # CASCADE remove a thread
    return {"deleted": run_id}
