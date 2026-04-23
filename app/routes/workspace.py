"""Workspace — execução de interações via FSM §15 + upload de arquivos."""
import uuid
import json
import os
import aiofiles
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from app.models.schemas import ChatMessage
from app.agents.engine import execute_interaction
from app.core.database import interactions_repo, turns_repo

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"

router = APIRouter(prefix="/api/v1/workspace", tags=["workspace"])


@router.get("/sessions")
async def list_sessions(agent_id: str = None, limit: int = 30, offset: int = 0):
    f = {}
    if agent_id: f["agent_id"] = agent_id
    sessions = await interactions_repo.find_all(limit=limit, offset=offset, **f)
    return {"sessions": sessions, "total": await interactions_repo.count(**f)}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    s = await interactions_repo.find_by_id(session_id)
    if not s: raise HTTPException(404, "Sessão não encontrada")
    msgs = await turns_repo.find_all(interaction_id=session_id, limit=200)

    # Restaurar trace_data persistido
    trace_data = None
    if s.get("trace_data"):
        try:
            trace_data = json.loads(s["trace_data"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Pipeline steps para enriquecer mensagens com metadata de agente
    pipeline_steps = trace_data.get("pipeline_steps", []) if trace_data else []

    messages = []
    assistant_idx = 0
    for t in reversed(msgs):
        if t.get("user_text_redacted"):
            messages.append({"role": "user", "content": t["user_text_redacted"], "created_at": t.get("created_at", "")})
        if t.get("output_text_redacted"):
            content = t["output_text_redacted"]
            # Converter JSON legado de recusa/escalação
            if content.startswith("{") and '"type"' in content:
                try:
                    p = json.loads(content)
                    if p.get("type") == "refusal":
                        content = f"⚠ Recusa controlada: {p.get('reason','')}\n\nPróximo passo: {p.get('next_step','')}"
                    elif p.get("type") == "escalation":
                        content = f"🔺 Escalação: {p.get('reason','')}"
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            msg = {"role": "assistant", "content": content, "created_at": t.get("created_at", "")}

            # Enriquecer com metadata do pipeline step correspondente
            if pipeline_steps and assistant_idx < len(pipeline_steps):
                step = pipeline_steps[assistant_idx]
                msg["_agentName"] = step.get("agent_name", "")
                msg["_agentKind"] = step.get("agent_kind", "")
                msg["_duration"] = step.get("duration_ms", 0)
                # Reconstruir trace individual do step
                msg["_trace"] = {
                    "interaction_id": step.get("interaction_id"),
                    "agent_id": step.get("agent_id"),
                    "final_state": step.get("final_state"),
                    "evidence_score": step.get("evidence_score", 0),
                    "transitions": step.get("transitions", []),
                    "duration_ms": step.get("duration_ms", 0),
                    "trace": step.get("trace", {}),
                    "mode": "agent",
                    "pipeline_steps": pipeline_steps,
                }
                assistant_idx += 1

            messages.append(msg)

    return {"session": s, "messages": messages, "trace": trace_data}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if not await interactions_repo.delete(session_id): raise HTTPException(404)
    return {"message": "Sessão removida"}


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, title: str = ""):
    s = await interactions_repo.find_by_id(session_id)
    if not s: raise HTTPException(404)
    await interactions_repo.update(session_id, {"title": title})
    return {"message": "Sessão renomeada", "title": title}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload de arquivo para uso pelo agente na sessão."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_id = str(uuid.uuid4())[:8]
    safe_name = f"{file_id}_{file.filename.replace(' ', '_')}"
    file_path = UPLOAD_DIR / safe_name

    content_bytes = await file.read()
    async with aiofiles.open(str(file_path), "wb") as f:
        await f.write(content_bytes)

    # Tentar ler como texto para passar ao agente
    text_content = None
    try:
        text_content = content_bytes.decode("utf-8")
        if len(text_content) > 50000:
            text_content = text_content[:50000] + "\n\n[...truncado em 50.000 caracteres]"
    except (UnicodeDecodeError, ValueError):
        text_content = f"[Arquivo binário: {file.filename}, {len(content_bytes)} bytes, tipo: {file.content_type}]"

    return {
        "file_id": file_id,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(content_bytes),
        "path": str(safe_name),
        "text_content": text_content,
    }


_IMAGE_MIME_PREFIXES = ("image/",)
_TEXT_MIME_PREFIXES = ("text/",)
_DOC_MIME_EXACT = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/rtf", "application/json", "application/xml",
    "application/x-yaml", "application/x-markdown",
}


def _classify_attachment(mime: str) -> str:
    """Retorna 'image' | 'document' | 'unknown'.

    Tudo que começa com image/* → image. text/* e MIMEs comuns de office
    → document. Resto → unknown (será filtrado se ambas as flags forem
    false).
    """
    mime = (mime or "").lower()
    if any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES):
        return "image"
    if mime in _DOC_MIME_EXACT or any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return "document"
    return "document"  # fallback — melhor assumir doc e deixar a flag decidir


async def _filter_attachments_by_agent(attachments: list, agent_id: str) -> tuple[list, list]:
    """Filtra attachments conforme flags accepts_images / accepts_documents
    do agente. Retorna (aceitos, rejeitados_meta) — rejeitados vão para
    o trace para o usuário ver o que foi podado."""
    if not attachments:
        return [], []
    from app.core.database import agents_repo
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        return attachments, []
    accepts_img = bool(agent.get("accepts_images") or 0)
    accepts_doc = bool(agent.get("accepts_documents") or 0)
    accepted, rejected = [], []
    for att in attachments:
        kind = _classify_attachment(att.get("type", ""))
        allowed = (kind == "image" and accepts_img) or (kind == "document" and accepts_doc)
        if allowed:
            accepted.append(att)
        else:
            rejected.append({
                "name": att.get("name", ""),
                "type": att.get("type", ""),
                "kind": kind,
                "reason": f"Agente não aceita {kind}s — habilite em 'Editar Agente'",
            })
    return accepted, rejected


@router.post("/chat")
async def chat(data: ChatMessage):
    """Executa interação (agente individual ou pipeline mesh)."""
    try:
        attachments = []
        if data.attachments:
            for att in data.attachments:
                attachments.append({
                    "name": att.get("filename", ""),
                    "type": att.get("content_type", ""),
                    "size": att.get("size", 0),
                    "content": att.get("text_content", ""),
                })
        # Filtra conforme flags do agente
        attachments, rejected_attachments = await _filter_attachments_by_agent(attachments, data.agent_id)

        if data.mode == "pipeline":
            from app.agents.engine import execute_pipeline
            result = await execute_pipeline(
                entry_agent_id=data.agent_id,
                user_input=data.message,
                channel=data.channel,
                attachments=attachments,
            )
        else:
            result = await execute_interaction(
                agent_id=data.agent_id,
                user_input=data.message,
                session_id=data.session_id,
                channel=data.channel,
                journey=data.journey or "",
                attachments=attachments,
            )

        # Persistir trace_data
        iid = result.get("interaction_id")
        if iid:
            trace_persist = {k: result.get(k) for k in ["interaction_id","agent_id","final_state","evidence_score","transitions","duration_ms","trace","pipeline_steps","mode"]}
            await interactions_repo.update(iid, {"trace_data": json.dumps(trace_persist, ensure_ascii=False, default=str)})

        # Sinaliza attachments rejeitados para que o frontend possa mostrar
        if rejected_attachments:
            result["rejected_attachments"] = rejected_attachments
        return result
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erro na execução: {str(e)}")