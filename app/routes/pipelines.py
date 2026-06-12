"""Rotas do Estúdio de Pipelines (PR1).

Pipeline vira entidade de 1ª classe: organização explícita de agentes +
lifecycle governado (rascunho|publicado|aposentado). As CONEXÕES continuam
SÓ em mesh_connections — aqui só gerimos membership (exclusiva) e metadados.

Runtime (execute_pipeline) NÃO muda no PR1 — status é metadado de governança;
o gate de execução por status entra no PR2. Sem auth (igual às rotas de mesh,
mesma área de UI); auditoria via audit_repo.
"""
import uuid
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.models.schemas import (
    PipelineCreate,
    PipelineUpdate,
    PipelineStatusChange,
    PipelineAddAgent,
)
from app.core.database import (
    pipelines_repo,
    pipeline_membership,
    agents_repo,
    audit_repo,
)
from app.agents.pipeline_lifecycle import (
    can_transition_pipeline,
    next_pipeline_states,
    PIPELINE_STATES,
)

router = APIRouter(prefix="/api/v1/pipelines", tags=["pipelines"])


def _iso(v):
    """datetime → ISO string; passa string/None adiante (asyncpg devolve datetime)."""
    return v.isoformat() if isinstance(v, datetime) else v


def _serialize(p: dict, agent_ids: list) -> dict:
    status = p.get("status", "rascunho")
    return {
        "id": p["id"],
        "name": p.get("name"),
        "status": status,
        "domain": p.get("domain"),
        "color": p.get("color") or "teal",
        "description": p.get("description"),
        "agent_ids": agent_ids,
        "agent_count": len(agent_ids),
        "next_states": list(next_pipeline_states(status)),
        "created_at": _iso(p.get("created_at")),
        "updated_at": _iso(p.get("updated_at")),
    }


async def _require(pid: str) -> dict:
    p = await pipelines_repo.find_by_id(pid)
    if not p:
        raise HTTPException(404, "Pipeline não encontrado")
    return p


@router.get("")
async def list_pipelines(status: Optional[str] = None, domain: Optional[str] = None):
    """Lista pipelines + agent_ids/agent_count. Filtros opcionais por igualdade.

    Inclui agent_ids (1 query de membership) para a UI montar lente e hand-offs
    sem N+1.
    """
    filters = {}
    if status:
        filters["status"] = status
    if domain:
        filters["domain"] = domain
    pipelines = await pipelines_repo.find_all(limit=500, **filters)
    membership = await pipeline_membership.all()
    by_pipeline: dict = {}
    for m in membership:
        by_pipeline.setdefault(m["pipeline_id"], []).append(m["agent_id"])
    return {"pipelines": [_serialize(p, by_pipeline.get(p["id"], [])) for p in pipelines]}


@router.post("", status_code=201)
async def create_pipeline(data: PipelineCreate):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(422, "name é obrigatório")
    pid = str(uuid.uuid4())
    await pipelines_repo.create({
        "id": pid,
        "name": name,
        "status": "rascunho",
        "domain": (data.domain or None),
        "color": (data.color or "teal"),
        "description": (data.description or None),
    })
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "created",
        "details": json.dumps({"name": name, "status": "rascunho"}),
    })
    row = await pipelines_repo.find_by_id(pid)
    return _serialize(row or {"id": pid, "name": name, "status": "rascunho"}, [])


@router.get("/{pid}")
async def get_pipeline(pid: str):
    p = await _require(pid)
    agent_ids = await pipeline_membership.agents_of(pid)
    return _serialize(p, agent_ids)


@router.put("/{pid}")
async def update_pipeline(pid: str, data: PipelineUpdate):
    """Atualiza metadados. NÃO muda status (use POST /{pid}/status — padrão do
    catálogo: transição governada nunca via PUT direto)."""
    await _require(pid)
    patch: dict = {}
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(422, "name não pode ser vazio")
        patch["name"] = name
    if data.domain is not None:
        patch["domain"] = data.domain or None
    if data.color is not None:
        patch["color"] = data.color or "teal"
    if data.description is not None:
        patch["description"] = data.description or None
    if patch:
        patch["updated_at"] = datetime.utcnow()
        await pipelines_repo.update(pid, patch)
    row = await pipelines_repo.find_by_id(pid)
    agent_ids = await pipeline_membership.agents_of(pid)
    return _serialize(row, agent_ids)


@router.delete("/{pid}")
async def delete_pipeline(pid: str):
    """Remove o pipeline + sua membership (CASCADE). As conexões e os agentes
    continuam intactos no mesh."""
    p = await _require(pid)
    await pipelines_repo.delete(pid)
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "deleted",
        "details": json.dumps({"name": p.get("name")}),
    })
    return {"message": "Pipeline removido", "id": pid}


@router.post("/{pid}/status")
async def change_status(pid: str, data: PipelineStatusChange):
    """Transição GOVERNADA de status (máquina de estados). 422 se inválida."""
    p = await _require(pid)
    to_state = data.status
    current = p.get("status", "rascunho")
    if to_state not in PIPELINE_STATES:
        raise HTTPException(
            422,
            f"status inválido: {to_state!r}. Use um de: {', '.join(PIPELINE_STATES)}.",
        )
    if to_state == current:
        # idempotente: já está no estado pedido (a UI só oferece next_states).
        agent_ids = await pipeline_membership.agents_of(pid)
        return _serialize(p, agent_ids)
    if not can_transition_pipeline(current, to_state):
        nxt = ", ".join(next_pipeline_states(current)) or "—"
        raise HTTPException(
            422,
            f"Pipeline em '{current}' não pode transitar para '{to_state}'. "
            f"Transições válidas: {nxt}.",
        )
    await pipelines_repo.update(pid, {"status": to_state, "updated_at": datetime.utcnow()})
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "status_changed",
        "details": json.dumps({"from": current, "to": to_state}),
    })
    row = await pipelines_repo.find_by_id(pid)
    agent_ids = await pipeline_membership.agents_of(pid)
    return _serialize(row, agent_ids)


@router.post("/{pid}/agents")
async def add_agent(pid: str, data: PipelineAddAgent):
    """Inclui um agente no pipeline. Membership EXCLUSIVA: se o agente já está em
    outro pipeline, é movido (upsert na PK agent_id)."""
    await _require(pid)
    if not await agents_repo.find_by_id(data.agent_id):
        raise HTTPException(404, "Agente não encontrado")
    prev = await pipeline_membership.pipeline_of(data.agent_id)
    await pipeline_membership.set(data.agent_id, pid)
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "agent_added",
        "details": json.dumps({"agent_id": data.agent_id, "moved_from": prev}),
    })
    agent_ids = await pipeline_membership.agents_of(pid)
    return {
        "pipeline_id": pid,
        "agent_id": data.agent_id,
        "moved_from": prev,
        "agent_ids": agent_ids,
    }


@router.delete("/{pid}/agents/{agent_id}")
async def remove_agent(pid: str, agent_id: str):
    """Remove o agente DESTE pipeline (404 se ele não pertence a ele)."""
    await _require(pid)
    removed = await pipeline_membership.remove_from(pid, agent_id)
    if not removed:
        raise HTTPException(404, "Agente não pertence a este pipeline")
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "agent_removed",
        "details": json.dumps({"agent_id": agent_id}),
    })
    agent_ids = await pipeline_membership.agents_of(pid)
    return {"pipeline_id": pid, "agent_id": agent_id, "agent_ids": agent_ids}
