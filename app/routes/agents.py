"""Rotas de agentes — AOBD, Router, Subagent."""
import uuid, json
from fastapi import APIRouter, HTTPException
from app.models.schemas import AgentCreate, AgentUpdate
from app.core.database import agents_repo, audit_repo

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

@router.get("")
async def list_agents(limit: int = 50, offset: int = 0, kind: str = None, status: str = None, domain: str = None):
    f = {}
    if kind: f["kind"] = kind
    if status: f["status"] = status
    if domain: f["domain"] = domain
    agents = await agents_repo.find_all(limit=limit, offset=offset, **f)
    return {"agents": agents, "total": await agents_repo.count(**f)}

@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    a = await agents_repo.find_by_id(agent_id)
    if not a: raise HTTPException(404, "Agente não encontrado")
    return a

@router.post("", status_code=201)
async def create_agent(data: AgentCreate):
    aid = str(uuid.uuid4())
    d = {"id": aid, **data.model_dump()}
    d["require_evidence"] = 1 if d.get("require_evidence", True) else 0
    await agents_repo.create(d)
    await audit_repo.create({"entity_type":"agent","entity_id":aid,"action":"created","details":json.dumps({"name":data.name,"kind":data.kind,"version":data.version})})
    return {"id": aid, "message": "Agente criado"}

@router.put("/{agent_id}")
async def update_agent(agent_id: str, data: AgentUpdate):
    existing = await agents_repo.find_by_id(agent_id)
    if not existing: raise HTTPException(404)
    upd = {k:v for k,v in data.model_dump().items() if v is not None}
    # require_evidence=False é válido, não filtrar
    if data.require_evidence is not None and "require_evidence" not in upd:
        upd["require_evidence"] = data.require_evidence
    # Convert bool to int for SQLite
    if "require_evidence" in upd:
        upd["require_evidence"] = 1 if upd["require_evidence"] else 0
    if not upd: raise HTTPException(400, "Nenhum campo")
    # Auto-bump version se campos significativos mudaram
    significant = {"system_prompt","model","llm_provider","skill_id","kind"}
    if any(k in upd for k in significant) and "version" not in upd:
        upd["version"] = _bump_version(existing.get("version","1.0.0"))
    return await agents_repo.update(agent_id, upd)

@router.patch("/{agent_id}/status")
async def toggle_agent_status(agent_id: str, status: str = "active"):
    existing = await agents_repo.find_by_id(agent_id)
    if not existing: raise HTTPException(404)
    new_status = status if status in ("active","inactive") else ("inactive" if existing.get("status")=="active" else "active")
    await agents_repo.update(agent_id, {"status": new_status})
    await audit_repo.create({"entity_type":"agent","entity_id":agent_id,"action":"status_changed","details":json.dumps({"from":existing.get("status"),"to":new_status})})
    return {"status": new_status, "message": f"Agente {'ativado' if new_status=='active' else 'desativado'}"}

@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    if not await agents_repo.delete(agent_id): raise HTTPException(404)
    # Cascade: remover conexões do AI Mesh que referenciam este agente
    from app.core.database import mesh_repo
    conns = await mesh_repo.find_all(limit=500)
    for c in conns:
        if c.get("source_agent_id") == agent_id or c.get("target_agent_id") == agent_id:
            try:
                await mesh_repo.delete(c["id"])
            except Exception:
                pass
    return {"message": "Agente removido"}

def _bump_version(v: str) -> str:
    parts = v.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)