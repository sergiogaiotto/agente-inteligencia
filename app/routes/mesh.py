"""Mesh + CAR — topologia e catálogo de roteadores §6."""
import uuid, json
from fastapi import APIRouter, HTTPException
from app.models.schemas import MeshConnectionCreate, CAREntryCreate
from app.core.database import mesh_repo, agents_repo, car_repo

router = APIRouter(prefix="/api/v1/mesh", tags=["mesh"])

@router.get("/topology")
async def get_topology():
    agents = await agents_repo.find_all(limit=200)
    conns = await mesh_repo.find_all(limit=500)
    active_agents = [a for a in agents if a.get("status") == "active"]
    active_ids = {a["id"] for a in active_agents}
    nodes = [{"id":a["id"],"name":a["name"],"kind":a.get("kind","subagent"),"status":a["status"],"provider":a["llm_provider"],"model":a["model"],"domain":a.get("domain",""),"version":a.get("version","1.0.0")} for a in active_agents]
    edges = []
    for c in conns:
        src, tgt = c["source_agent_id"], c["target_agent_id"]
        if src in active_ids and tgt in active_ids:
            edges.append({"id":c["id"],"source":src,"target":tgt,"type":c["connection_type"]})
        elif src not in {a["id"] for a in agents} or tgt not in {a["id"] for a in agents}:
            # Auto-cleanup: conexão órfã → agente deletado (não apenas inativo)
            try:
                await mesh_repo.delete(c["id"])
            except Exception:
                pass
    return {"nodes": nodes, "edges": edges}

@router.post("/connections", status_code=201)
async def create_connection(data: MeshConnectionCreate):
    if not await agents_repo.find_by_id(data.source_agent_id) or not await agents_repo.find_by_id(data.target_agent_id):
        raise HTTPException(404, "Agente não encontrado")
    cid = str(uuid.uuid4())
    await mesh_repo.create({"id":cid,"source_agent_id":data.source_agent_id,"target_agent_id":data.target_agent_id,"connection_type":data.connection_type,"config":data.config or "{}"})
    return {"id": cid, "message": "Conexão criada"}

@router.put("/connections/{conn_id}")
async def update_connection(conn_id: str, data: MeshConnectionCreate):
    existing = await mesh_repo.find_by_id(conn_id)
    if not existing: raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    return await mesh_repo.update(conn_id, upd)

@router.delete("/connections/{conn_id}")
async def delete_connection(conn_id: str):
    if not await mesh_repo.delete(conn_id): raise HTTPException(404)
    return {"message": "Conexão removida"}

# ── CAR §6 ──
car_router = APIRouter(prefix="/api/v1/car", tags=["car"])

@car_router.get("")
async def list_car(domain: str = None, limit: int = 50):
    f = {}
    if domain: f["domain"] = domain
    return {"entries": await car_repo.find_all(limit=limit, **f)}

@car_router.post("", status_code=201)
async def create_car_entry(data: CAREntryCreate):
    eid = str(uuid.uuid4())
    await car_repo.create({"id":eid,"skill_urn":data.skill_urn,"domain":data.domain,"activation_keywords":data.activation_keywords,"required_entities":data.required_entities})
    return {"id": eid, "message": "Entrada CAR criada"}

@car_router.delete("/{entry_id}")
async def delete_car_entry(entry_id: str):
    if not await car_repo.delete(entry_id): raise HTTPException(404)
    return {"message": "Entrada removida"}