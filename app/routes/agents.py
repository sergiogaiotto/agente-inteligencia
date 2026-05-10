"""Rotas de agentes — AOBD, Router, Subagent."""
import re
import time
import uuid, json
from fastapi import APIRouter, HTTPException
from app.models.schemas import (
    AgentCreate, AgentUpdate, AgentInvokeRequest, AgentInvokeResponse,
    PreflightReport,
)
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

_BOOL_FIELDS = ("require_evidence", "accepts_images", "accepts_documents")


@router.post("/preflight", response_model=PreflightReport)
async def preflight_agent(data: AgentCreate) -> PreflightReport:
    """Roda 9 checks semânticos contra o payload (sem persistir).
    UI consome para mostrar lista de checks na step Revisão.
    """
    from app.agents.preflight import run_preflight
    return await run_preflight(data.model_dump())


async def _resolve_task_type_to_provider_model(payload: dict) -> dict:
    """Onda 7: quando agente declara task_type, resolve provider/model
    via routing settings e snapshota no payload. Mutação in-place.

    NULL/ausente = legacy mode (mantém llm_provider/model do payload).
    Setado = sobrescreve llm_provider/model com a resolução atual.
    """
    task_type = payload.get("task_type")
    if not task_type:
        return payload
    from app.llm_routing import resolve_llm_for_task
    try:
        provider, model = await resolve_llm_for_task(task_type, has_image=False)
        payload["llm_provider"] = provider
        payload["model"] = model
    except Exception as e:
        # Falha de routing: mantém o que veio do payload (back-compat) e loga.
        import logging
        logging.getLogger(__name__).warning(
            f"resolve_task_type_to_provider_model falhou: {e}; "
            f"mantendo provider={payload.get('llm_provider')}, model={payload.get('model')}"
        )
    return payload


@router.post("", status_code=201)
async def create_agent(data: AgentCreate):
    # Pre-flight bloqueia errors antes de persistir.
    from app.agents.preflight import run_preflight
    report = await run_preflight(data.model_dump())
    if report.blocked:
        raise HTTPException(422, detail={
            "message": "Configuração com erros — corrija antes de salvar",
            "preflight": report.model_dump(),
        })

    aid = str(uuid.uuid4())
    d = {"id": aid, **data.model_dump()}
    # Onda 7: se task_type setado, resolve provider/model via routing.
    d = await _resolve_task_type_to_provider_model(d)
    # Schema legacy persiste flags booleanas como INTEGER 0/1 — converter aqui.
    # (Refator para BOOLEAN é projeto separado; muitos checks dependem de `= 1`.)
    for f in _BOOL_FIELDS:
        if f in d and d[f] is not None:
            d[f] = 1 if d[f] else 0
    await agents_repo.create(d)
    await audit_repo.create({"entity_type":"agent","entity_id":aid,"action":"created","details":json.dumps({"name":data.name,"kind":data.kind,"version":data.version})})
    return {"id": aid, "message": "Agente criado"}

@router.put("/{agent_id}")
async def update_agent(agent_id: str, data: AgentUpdate):
    existing = await agents_repo.find_by_id(agent_id)
    if not existing: raise HTTPException(404)
    upd = {k:v for k,v in data.model_dump().items() if v is not None}
    # require_evidence=False / accepts_*=False são valores válidos —
    # model_dump() com exclude_none já preserva eles, mas a comparação
    # inicial `if v is not None` faz o filtro correto. Explícito apenas
    # para require_evidence por retrocompat.
    if data.require_evidence is not None and "require_evidence" not in upd:
        upd["require_evidence"] = data.require_evidence

    # Pre-flight no payload mesclado (existing + upd) — cobre o estado
    # final que o agente terá após o update. Bloqueia erros antes de
    # persistir; warnings/info passam.
    from app.agents.preflight import run_preflight
    merged_payload = {**existing, **upd}
    # Coerção bool→int legacy só acontece DEPOIS — preflight roda em bool.
    for f in _BOOL_FIELDS:
        v = merged_payload.get(f)
        if isinstance(v, int) and not isinstance(v, bool):
            merged_payload[f] = bool(v)
    report = await run_preflight(merged_payload)
    if report.blocked:
        raise HTTPException(422, detail={
            "message": "Configuração com erros — corrija antes de atualizar",
            "preflight": report.model_dump(),
        })

    # Onda 7: task_type setado (ou modificado) → re-resolve provider/model
    # via routing. Snapshot novo sobrescreve eventual llm_provider/model
    # vindos no payload.
    if "task_type" in upd or merged_payload.get("task_type"):
        # Usa task_type final (do upd se mudou, senão do existing)
        merged_for_resolve = {
            "task_type": upd.get("task_type") or merged_payload.get("task_type"),
        }
        await _resolve_task_type_to_provider_model(merged_for_resolve)
        if merged_for_resolve.get("llm_provider"):
            upd["llm_provider"] = merged_for_resolve["llm_provider"]
        if merged_for_resolve.get("model"):
            upd["model"] = merged_for_resolve["model"]

    # Schema legacy: flags booleanas como INTEGER 0/1.
    for f in _BOOL_FIELDS:
        if f in upd:
            upd[f] = 1 if upd[f] else 0
    if not upd: raise HTTPException(400, "Nenhum campo")
    # Auto-bump version se campos significativos mudaram
    significant = {"system_prompt","model","llm_provider","skill_id","kind","temperature","task_type"}
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


_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w\.]*)\s*\}\}")


def _walk_strings(node, sink: list[str]):
    """Coleta todas as strings dentro de uma estrutura YAML aninhada."""
    if isinstance(node, str):
        sink.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_strings(v, sink)
    elif isinstance(node, list):
        for v in node:
            _walk_strings(v, sink)


def _extract_referenced_inputs(api_bindings_parsed: list) -> list[str]:
    """Extrai variáveis `inputs.X` referenciadas em qualquer string Jinja
    dentro dos API bindings parseados. Retorna nomes únicos, ordenados."""
    if not api_bindings_parsed:
        return []
    strings: list[str] = []
    for binding in api_bindings_parsed:
        _walk_strings(binding, strings)
    found: set[str] = set()
    for s in strings:
        for m in _TEMPLATE_VAR_RE.findall(s):
            if m.startswith("inputs."):
                name = m[len("inputs."):]
                if name:
                    found.add(name)
    return sorted(found)


def _summarize_bindings(api_bindings_parsed: list) -> list[dict]:
    out = []
    for b in api_bindings_parsed or []:
        if not isinstance(b, dict):
            continue
        out.append({
            "id": b.get("id"),
            "method": b.get("method", "GET"),
            "path": b.get("path", ""),
            "connector": b.get("connector", ""),
        })
    return out


@router.get("/{agent_id}/inputs-schema")
async def get_agent_inputs_schema(agent_id: str):
    """Retorna metadados de inputs do agente para auxiliar o chat do workspace.

    Inclui: identificação do agente, sumário da skill, JSON Schema da seção
    ## Inputs, lista de variáveis `inputs.*` referenciadas nos API bindings,
    e sumário dos bindings (id/method/path/connector).
    """
    from app.skill_parser.parser import parse_skill_md

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    payload = {
        "agent": {
            "id": agent_id,
            "name": agent.get("name"),
            "kind": agent.get("kind"),
            "model": agent.get("model"),
            "version": agent.get("version"),
            "domain": agent.get("domain"),
            "llm_provider": agent.get("llm_provider"),
        },
        "skill": None,
        "inputs_schema": None,
        "inputs_referenced": [],
        "api_bindings": [],
        "execution_mode": None,
    }

    if not agent.get("skill_id"):
        return payload

    skill_row = await skills_repo.find_by_id(agent["skill_id"])
    if not skill_row or not skill_row.get("raw_content"):
        return payload

    parsed = parse_skill_md(skill_row["raw_content"])
    payload["skill"] = {
        "id": skill_row.get("id"),
        "name": parsed.name,
        "urn": parsed.frontmatter.id,
        "version": parsed.frontmatter.version,
        "purpose": (parsed.purpose or "").strip()[:500],
    }
    payload["execution_mode"] = parsed.execution_mode
    payload["inputs_schema"] = _extract_inputs_schema(parsed.inputs)
    payload["api_bindings"] = _summarize_bindings(parsed.api_bindings_parsed)
    payload["inputs_referenced"] = _extract_referenced_inputs(parsed.api_bindings_parsed)
    return payload


@router.post("/{agent_id}/invoke", response_model=AgentInvokeResponse)
async def invoke_agent(agent_id: str, data: AgentInvokeRequest) -> AgentInvokeResponse:
    from app.agents.engine import execute_interaction
    from app.agents.declarative_engine import execute_declarative
    from app.skill_parser.parser import parse_skill_md

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    parsed_skill = None
    if agent.get("skill_id"):
        skill_row = await skills_repo.find_by_id(agent["skill_id"])
        if skill_row and skill_row.get("raw_content"):
            parsed_skill = parse_skill_md(skill_row["raw_content"])
            schema = _extract_inputs_schema(parsed_skill.inputs)
            if schema and data.inputs:
                errs = _validate_inputs(data.inputs, schema)
                if errs:
                    raise HTTPException(422, {"message": "Falha de validação de inputs", "errors": errs})

    is_declarative = bool(parsed_skill and parsed_skill.execution_mode == "declarative")

    if not is_declarative and not data.message and not data.inputs:
        raise HTTPException(400, "Informe ao menos 'message' ou 'inputs'")
    # Modo declarativo não exige inputs — bindings podem ser auto-contidos.

    start = time.time()

    if is_declarative:
        dry_run = bool(data.options and data.options.dry_run)
        try:
            result = await execute_declarative(
                agent=agent,
                skill_parsed=parsed_skill,
                inputs=data.inputs,
                context=data.context,
                session_id=data.session_id,
                dry_run=dry_run,
            )
        except Exception as e:
            raise HTTPException(500, f"Erro no engine declarativo: {e}")

        errs = result.get("errors", []) or []
        executed = result.get("bindings_executed", []) or []
        any_success = any(200 <= b.get("status", 0) < 300 for b in executed)
        if errs and not any_success:
            status = "failed"
        elif errs:
            status = "partial"
        else:
            status = "ok"

        duration = result.get("duration_ms") or round((time.time() - start) * 1000, 2)

        await audit_repo.create({
            "entity_type": "agent",
            "entity_id": agent_id,
            "action": "invoked",
            "details": json.dumps({
                "mode": "declarative",
                "session_id": result.get("interaction_id"),
                "inputs_keys": list(data.inputs.keys()) if data.inputs else [],
                "bindings_executed": len(executed),
                "errors": len(errs),
                "duration_ms": duration,
            }, ensure_ascii=False),
        })

        outputs_dict = {
            "bindings_executed": executed,
            "final_state": result.get("final_state", ""),
            "compensations_fired": result.get("compensations_fired", []),
        }
        if dry_run:
            outputs_dict["plans"] = result.get("dry_run_plans") or []
            outputs_dict["dry_run"] = True
        return AgentInvokeResponse(
            session_id=result.get("interaction_id"),
            agent_id=agent_id,
            status=status,
            outputs=outputs_dict,
            context=result.get("context", {}),
            trace_id=result.get("interaction_id"),
            duration_ms=duration,
            evidence_score=None,
            errors=errs,
        )

    # Caminho LLM (Fase 1) — permanece como fallback
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
            "mode": "llm",
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
