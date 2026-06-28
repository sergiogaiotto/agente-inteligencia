"""Playground — histórico de execuções persistido POR USUÁRIO (Feature 1).

Antes o histórico do console (`mesh_playground.html`) vivia só em `localStorage`
(por-navegador). Aqui ele passa a ser persistido no servidor, escopado ao usuário
autenticado — sobrevive a troca de máquina e fica auditável. Tudo ESCALAR (sem
payload/JSONB): guarda o MESMO cartão que a UI mostra (pipeline, mensagem,
verbosidade, status, tamanho, duração), NUNCA a resposta inteira (que pode ser
sensível). O `localStorage` segue como cache offline opcional na UI.

Auth: ``require_user`` (cookie de sessão OU ``X-API-Key``). Cada linha é escopada
a ``user_id``; um usuário nunca lê/apaga o histórico de outro.

Endpoints:
- ``POST   /api/v1/playground/runs``        grava uma execução (chamada otimista)
- ``GET    /api/v1/playground/runs?limit``  últimas do user (mais recentes 1º)
- ``DELETE /api/v1/playground/runs``        limpa tudo do user
- ``DELETE /api/v1/playground/runs/{id}``   remove uma (404 se não for do dono)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_user
from app.core.database import playground_runs_repo
from app.models.schemas import PlaygroundRunCreate

router = APIRouter(prefix="/api/v1/playground", tags=["playground"])

#: histórico mantido por usuário (folga sobre o cap de 20 do localStorage). Poda
#: na inserção pra não crescer sem limite — só o cartão, mas ainda assim bounded.
_MAX_KEEP = 50


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

    ``find_all`` ordena por ``created_at`` DESC, então tudo após o índice ``keep``
    é o excedente mais antigo. Em regime normal apaga 0–1 linha por inserção.
    """
    rows = await playground_runs_repo.find_all(user_id=user_id, limit=keep + 200)
    for r in rows[keep:]:
        rid = r.get("id")
        if rid:
            await playground_runs_repo.delete(rid)


@router.post("/runs", status_code=201)
async def create_run(data: PlaygroundRunCreate, user: dict = Depends(require_user)):
    """Persiste UMA execução do Playground no histórico do usuário.

    Chamado de forma OTIMISTA pela UI (empurra local + persiste). A mensagem é
    truncada defensivamente; ``created_at`` é NAIVE (coluna TIMESTAMP) — armadilha
    asyncpg conhecida (datetime aware → 500). Após gravar, PODA o histórico do
    usuário para ``<= _MAX_KEEP`` (50) execuções (retém as mais recentes).
    """
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "pipeline_id": (data.pipeline_id or None),
        "pipeline_name": (data.pipeline_name or None),
        "message": (data.message or "")[:2000],
        "verbosity": (data.verbosity or None),
        "status": (data.status or None),
        "size_bytes": data.size_bytes,
        "duration_ms": data.duration_ms,
        "created_at": datetime.utcnow(),  # NAIVE — coluna TIMESTAMP (não aware!)
    }
    await playground_runs_repo.create(row)
    await _prune(user["id"])
    return _serialize(row)


@router.get("/runs")
async def list_runs(
    user: dict = Depends(require_user),
    limit: int = Query(20, ge=1, le=100),
):
    """Últimas execuções do usuário (mais recentes primeiro)."""
    rows = await playground_runs_repo.find_all(user_id=user["id"], limit=limit)
    return {"runs": [_serialize(r) for r in rows]}


@router.delete("/runs")
async def clear_runs(user: dict = Depends(require_user)):
    """Limpa TODO o histórico do usuário (mantido ≤ _MAX_KEEP pela poda)."""
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
    await playground_runs_repo.delete(run_id)
    return {"deleted": run_id}
