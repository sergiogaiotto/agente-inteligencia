"""Rotas do Estúdio de Pipelines (PR1).

Pipeline vira entidade de 1ª classe: organização explícita de agentes +
lifecycle governado (rascunho|publicado|aposentado). As CONEXÕES continuam
SÓ em mesh_connections — aqui só gerimos membership (exclusiva) e metadados.

Runtime (execute_pipeline) NÃO muda no PR1 — status é metadado de governança;
o gate de execução por status entra no PR2. As rotas de CRUD/lifecycle ficam SEM
auth (igual às rotas de mesh, mesma área de UI interna); auditoria via audit_repo.

EXCEÇÃO — `POST /{pid}/invoke` é o CONTRATO EXTERNO (o que o modal de cURL expõe)
e EXIGE autenticação (`Depends(require_user)`): cookie de sessão (UI) OU header
`X-API-Key: ag_live_...` (integração). Sem isso, qualquer um na rede dispararia
execuções que gastam tokens de LLM. Mesmo padrão do `POST /api/v1/workspace/chat`.
"""
import uuid
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.auth import require_user

from app.models.schemas import (
    PipelineCreate,
    PipelineUpdate,
    PipelineStatusChange,
    PipelineAddAgent,
    PipelineEntrySet,
    PipelineInvokeRequest,
)
from app.core.database import (
    pipelines_repo,
    pipeline_membership,
    agents_repo,
    audit_repo,
    settings_store,
)
from app.agents.pipeline_lifecycle import (
    can_transition_pipeline,
    next_pipeline_states,
    PIPELINE_STATES,
)
from app.agents.result_view import resolve_verbosity, project_pipeline_result

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
        "entry_agent_id": p.get("entry_agent_id"),
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


@router.post("/{pid}/entry")
async def set_pipeline_entry(pid: str, data: PipelineEntrySet):
    """Define (ou limpa) o ponto de entrada EXPLÍCITO do pipeline.

    agent_id deve ser MEMBRO do pipeline (ou null → volta ao automático:
    _detect_roots/fallback). O invoke e o _build_subgraph passam a usar esse agente
    como raiz — desempata 2+ raízes ou 0 conexões, dando controle de por onde o
    pipeline começa. Validar membership evita apontar para um agente fora do selo.
    """
    await _require(pid)
    agent_id = (data.agent_id or "").strip() or None
    if agent_id is not None:
        owner = await pipeline_membership.pipeline_of(agent_id)
        if owner != pid:
            raise HTTPException(422, "agent_id deve ser um membro deste pipeline (ou null para automático).")
    await pipelines_repo.update(pid, {"entry_agent_id": agent_id, "updated_at": datetime.utcnow()})
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "entry_set",
        "details": json.dumps({"entry_agent_id": agent_id}, ensure_ascii=False),
    })
    fresh = await pipelines_repo.find_by_id(pid)
    agent_ids = await pipeline_membership.agents_of(pid)
    return _serialize(fresh, agent_ids)


@router.post("/{pid}/invoke")
async def invoke_pipeline(
    pid: str,
    data: PipelineInvokeRequest,
    request: Request,
    user: dict = Depends(require_user),
    verbosity: Optional[str] = Query(
        None, description="Detalhe da resposta: full | summary | minimal. "
        "Sobrescreve o default por auth (sessão→full; X-API-Key→summary)."
    ),
):
    """Invoca um pipeline pela ENTIDADE (contrato API-first SELADO — Trilha A PR-A2).

    Resolve a raiz + os membros do pipeline e executa via execute_pipeline
    DELIMITADO ao subgrafo (allowed_agent_ids=membros) — a execução não vaza para
    o mesh global. Mais estável que invocar o UUID do agente-raiz (que pode mudar
    ao recabear o mesh). `aposentado` → 409 (não roteável); rascunho/publicado rodam.
    Descoberta: GET /api/v1/pipelines (filtra ?status=publicado).

    AUTH (contrato externo): exige cookie de sessão (UI) OU `X-API-Key: ag_live_...`
    (integração). 401 sem credencial. Quando vem por chave, `request.state.api_key_id`
    é registrado na auditoria pra distinguir a integração que disparou.
    """
    p = await _require(pid)
    if p.get("status") == "aposentado":
        raise HTTPException(409, f"Pipeline '{p.get('name')}' está aposentado — não é roteável.")
    user_input = (data.message or data.input or "").strip()
    if not user_input:
        raise HTTPException(400, "Informe 'message' (ou 'input').")

    # Resolve o subgrafo VIVO do pipeline (raiz + membros) — reusa o builder do
    # snapshot do catálogo (mesma lógica: membership + arestas intra-pipeline + raiz).
    from app.catalog.pipeline_defs import _build_subgraph
    sub = await _build_subgraph(pid)
    root = sub.get("root_agent_id")
    members = {n.get("id") for n in sub.get("nodes", []) if n.get("id")}
    if not root:
        raise HTTPException(422, "Pipeline sem agentes/raiz resolvível — nada a executar.")

    # Anexos: mapeia a saída do /workspace/upload pra forma que o engine consome.
    # O dispatcher do execute_pipeline roteia cada anexo só aos agentes da cadeia
    # que aceitam doc/imagem; os demais ignoram (sem poda cega aqui).
    from pathlib import Path
    from app.routes.workspace import UPLOAD_DIR
    pipeline_attachments = [
        {
            "name": att.get("filename", ""),
            "type": att.get("content_type", ""),
            "size": att.get("size", 0),
            "content": att.get("text_content", ""),
            "abs_path": str(UPLOAD_DIR / Path(att.get("path", "") or "").name) if att.get("path") else "",
        }
        for att in (data.attachments or [])
    ]

    from app.agents.engine import execute_pipeline
    try:
        result = await execute_pipeline(
            entry_agent_id=root,
            user_input=user_input,
            channel=data.channel or "api",
            session_id=data.session_id,
            attachments=pipeline_attachments or None,
            allowed_agent_ids=members,  # SELA a execução ao subgrafo do pipeline
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erro na execução do pipeline: {e}")

    api_key_id = getattr(request.state, "api_key_id", None)
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "invoked",
        "details": json.dumps({
            "root_agent_id": root,
            "member_count": len(members),
            "completed_agents": result.get("completed_agents", 0),
            "interaction_id": result.get("interaction_id"),
            "actor_user_id": user.get("id"),
            "via": "api_key" if api_key_id else "session",
            "api_key_id": api_key_id,
        }, ensure_ascii=False),
    })
    payload = {
        "pipeline_id": pid,
        "status": result.get("status", "completed"),
        "output": result.get("output", ""),
        "final_state": result.get("final_state"),
        "interaction_id": result.get("interaction_id"),
        "total_agents": result.get("total_agents", 0),
        "completed_agents": result.get("completed_agents", 0),
        "pipeline_steps": result.get("pipeline_steps", []),
        "duration_ms": result.get("duration_ms"),
    }
    # Verbosidade da resposta (projeção server-side; NÃO muda execução/custo).
    # Precedência: query > body > default por auth. Sessão→full; X-API-Key→
    # platform_settings.api_invoke_default_verbosity (semente 'summary').
    explicit = verbosity or data.verbosity
    # default por auth p/ integrações (X-API-Key). Lido SEMPRE que houver chave —
    # assim até um explícito inválido (typo) cai no nível CONFIGURADO, nunca em
    # 'full' (que vazaria o debug). Chamada de sessão não lê settings (fica 'full').
    api_default = "summary"
    if api_key_id:
        api_default = await settings_store.get("api_invoke_default_verbosity", "summary")
    effective = resolve_verbosity(explicit, is_api_key=bool(api_key_id), api_default=api_default)
    return project_pipeline_result(payload, effective)


@router.post("/{pid}/invoke/stream")
async def invoke_pipeline_stream(
    pid: str,
    data: PipelineInvokeRequest,
    request: Request,
    user: dict = Depends(require_user),
):
    """Streaming (SSE) do invoke SELADO — emite 1 evento por transição em tempo real
    (pipeline_start, agent_start/done/skipped/error, pipeline_done com o result, end).

    Mesmo selo (raiz+membros via allowed_agent_ids) e auth do /invoke; o frontend
    consome via fetch+ReadableStream e mostra o passo-a-passo ao vivo. Espelha o
    padrão do POST /workspace/chat/stream (queue + progress_callback + StreamingResponse).
    """
    import asyncio
    from pathlib import Path
    from fastapi.responses import StreamingResponse
    from app.routes.workspace import UPLOAD_DIR

    p = await _require(pid)
    if p.get("status") == "aposentado":
        raise HTTPException(409, f"Pipeline '{p.get('name')}' está aposentado — não é roteável.")
    user_input = (data.message or data.input or "").strip()
    if not user_input:
        raise HTTPException(400, "Informe 'message' (ou 'input').")

    from app.catalog.pipeline_defs import _build_subgraph
    sub = await _build_subgraph(pid)
    root = sub.get("root_agent_id")
    members = {n.get("id") for n in sub.get("nodes", []) if n.get("id")}
    if not root:
        raise HTTPException(422, "Pipeline sem agentes/raiz resolvível — nada a executar.")

    pipeline_attachments = [
        {
            "name": att.get("filename", ""),
            "type": att.get("content_type", ""),
            "size": att.get("size", 0),
            "content": att.get("text_content", ""),
            "abs_path": str(UPLOAD_DIR / Path(att.get("path", "") or "").name) if att.get("path") else "",
        }
        for att in (data.attachments or [])
    ]

    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def _cb(event: dict) -> None:
        await queue.put(event)

    async def _run():
        from app.agents.engine import execute_pipeline
        try:
            await execute_pipeline(
                entry_agent_id=root,
                user_input=user_input,
                channel=data.channel or "api",
                session_id=data.session_id,
                attachments=pipeline_attachments or None,
                allowed_agent_ids=members,  # SELA ao subgrafo do pipeline
                progress_callback=_cb,
            )
        except Exception as e:
            await queue.put({"type": "stream_error", "error": str(e)[:300]})
        finally:
            await queue.put(_DONE)

    asyncio.create_task(_run())

    async def _event_gen():
        yield ":ok\n\n"  # heartbeat: força proxies a flushar os headers antes do 1º evento
        while True:
            item = await queue.get()
            if item is _DONE:
                yield "event: end\ndata: {}\n\n"
                break
            payload = json.dumps(item, ensure_ascii=False, default=str)
            name = item.get("type", "message") if isinstance(item, dict) else "message"
            yield f"event: {name}\ndata: {payload}\n\n"

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
