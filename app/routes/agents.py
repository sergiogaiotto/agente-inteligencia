"""Rotas de agentes — AOBD, Router, Subagent."""
import re
import time
import uuid, json
from fastapi import APIRouter, HTTPException
from app.models.schemas import AgentCreate, AgentUpdate, AgentInvokeRequest, AgentInvokeResponse
from app.core.database import agents_repo, audit_repo, skills_repo

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


# ═══════════════════════════════════════════════════════
# INVOKE — Entry point estruturado (Fase 1)
# ═══════════════════════════════════════════════════════
# Complementa /workspace/chat (texto livre) com contrato JSON tipado.
# Fase 1: caminho LLM via execute_interaction(); inputs viram bloco JSON
# no user_input. Validação de inputs contra JSON Schema embutido em
# SKILL.md ## Inputs (fenced ```json). Fase 2 adicionará execution_mode
# declarative, sem LLM.

_JSON_TYPE_MAP = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "object": dict, "array": list, "null": type(None),
}


def _extract_inputs_schema(inputs_section: str) -> dict | None:
    if not inputs_section:
        return None
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", inputs_section, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _validate_inputs(inputs: dict, schema: dict) -> list[str]:
    errors: list[str] = []
    required = schema.get("required")
    if isinstance(required, list):
        for field in required:
            if field not in inputs:
                errors.append(f"Campo obrigatório ausente: '{field}'")
    props = schema.get("properties")
    if isinstance(props, dict):
        for field, spec in props.items():
            if field not in inputs or not isinstance(spec, dict):
                continue
            expected = spec.get("type")
            py_type = _JSON_TYPE_MAP.get(expected) if expected else None
            if py_type and not isinstance(inputs[field], py_type):
                errors.append(
                    f"Campo '{field}' deveria ser {expected}, recebido {type(inputs[field]).__name__}"
                )
    return errors


@router.post("/{agent_id}/invoke", response_model=AgentInvokeResponse)
async def invoke_agent(agent_id: str, data: AgentInvokeRequest) -> AgentInvokeResponse:
    from app.agents.engine import execute_interaction
    from app.skill_parser.parser import parse_skill_md

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    if not data.message and not data.inputs:
        raise HTTPException(400, "Informe ao menos 'message' ou 'inputs'")

    if agent.get("skill_id"):
        skill_row = await skills_repo.find_by_id(agent["skill_id"])
        if skill_row and skill_row.get("raw_content"):
            parsed = parse_skill_md(skill_row["raw_content"])
            schema = _extract_inputs_schema(parsed.inputs)
            if schema and data.inputs:
                errs = _validate_inputs(data.inputs, schema)
                if errs:
                    raise HTTPException(422, {"message": "Falha de validação de inputs", "errors": errs})

    parts = []
    if data.message:
        parts.append(data.message)
    if data.inputs:
        parts.append(
            "## Parâmetros estruturados\n```json\n"
            + json.dumps(data.inputs, ensure_ascii=False, indent=2)
            + "\n```"
        )
    user_input = "\n\n".join(parts)

    pipeline_context = json.dumps(data.context, ensure_ascii=False) if data.context else None

    start = time.time()
    try:
        result = await execute_interaction(
            agent_id=agent_id,
            user_input=user_input,
            session_id=data.session_id,
            channel=data.channel or "api",
            journey=data.journey or "",
            pipeline_context=pipeline_context,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erro na execução: {str(e)}")

    final_state = result.get("final_state") or ""
    if final_state == "Recommend":
        status = "ok"
    elif final_state in ("Refuse", "Escalate"):
        status = "partial"
    else:
        status = "ok" if result.get("output") else "failed"

    duration = result.get("duration_ms") or round((time.time() - start) * 1000, 2)

    await audit_repo.create({
        "entity_type": "agent",
        "entity_id": agent_id,
        "action": "invoked",
        "details": json.dumps({
            "session_id": result.get("interaction_id"),
            "inputs_keys": list(data.inputs.keys()) if data.inputs else [],
            "has_message": bool(data.message),
            "has_context": bool(data.context),
            "final_state": final_state,
            "duration_ms": duration,
        }, ensure_ascii=False),
    })

    return AgentInvokeResponse(
        session_id=result.get("interaction_id"),
        agent_id=agent_id,
        status=status,
        outputs={
            "answer": result.get("output", ""),
            "final_state": final_state,
        },
        context=data.context or {},
        trace_id=result.get("interaction_id"),
        duration_ms=duration,
        evidence_score=result.get("evidence_score"),
    )