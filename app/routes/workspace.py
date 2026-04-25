import uuid
import json
import os
import re
import ast
import aiofiles
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from app.models.schemas import ChatMessage
from app.agents.engine import execute_interaction
from app.core.database import interactions_repo, turns_repo

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"

router = APIRouter(prefix="/api/v1/workspace", tags=["workspace"])


def _extract_inputs_schema(inputs_section: str) -> dict | None:
    if not inputs_section:
        return None
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", inputs_section, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _coerce_input_value(value, spec: dict):
    if not isinstance(spec, dict):
        return value
    expected = spec.get("type")
    if expected == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            txt = value.strip()
            if txt == "":
                return value
            return int(txt)
    if expected == "number":
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            txt = value.strip()
            if txt == "":
                return value
            return float(txt)
    if expected == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            txt = value.strip().lower()
            if txt in {"true", "1", "yes", "sim"}:
                return True
            if txt in {"false", "0", "no", "nao", "não"}:
                return False
    if expected == "array":
        item_spec = spec.get("items", {}) if isinstance(spec.get("items"), dict) else {}
        if isinstance(value, list):
            return [_coerce_input_value(v, item_spec) for v in value]
        if isinstance(value, str):
            txt = value.strip()
            if not txt:
                return []
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, list):
                    return [_coerce_input_value(v, item_spec) for v in parsed]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            # fallback para "1,2,3" ou valor único "4"
            if "," in txt:
                parts = [p.strip() for p in txt.split(",") if p.strip()]
                return [_coerce_input_value(p, item_spec) for p in parts]
            return [_coerce_input_value(txt, item_spec)]
        return [value]
    return value


def _coerce_inputs_by_schema(inputs: dict, schema: dict | None) -> dict:
    if not schema or not isinstance(inputs, dict):
        return inputs
    out = dict(inputs)
    props = schema.get("properties")
    required = set(schema.get("required") or [])
    if not isinstance(props, dict):
        return out
    for field, spec in props.items():
        if field not in out:
            continue
        val = out.get(field)
        if isinstance(val, str) and val.strip() == "" and field not in required:
            # Campo opcional vazio: remove para evitar erro de validação downstream.
            out.pop(field, None)
            continue
        try:
            out[field] = _coerce_input_value(val, spec if isinstance(spec, dict) else {})
        except (ValueError, TypeError):
            # Mantém valor original se coercão falhar; a validação de destino decide.
            out[field] = val
    return out


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
            # Auto-rotear para engine declarativo se a skill do agente declara
            # execution_mode=declarative — assim a chamada à API real é feita
            # em vez do LLM apenas comentar sobre.
            from app.core.database import agents_repo, skills_repo
            from app.skill_parser.parser import parse_skill_md

            parsed_skill = None
            agent_obj = await agents_repo.find_by_id(data.agent_id)
            if agent_obj and agent_obj.get("skill_id"):
                sk = await skills_repo.find_by_id(agent_obj["skill_id"])
                if sk and sk.get("raw_content"):
                    parsed_skill = parse_skill_md(sk["raw_content"])

            is_declarative = bool(parsed_skill and parsed_skill.execution_mode == "declarative")

            if is_declarative:
                from app.agents.declarative_engine import execute_declarative

                # Inputs vêm da mensagem: se for JSON válido usa direto; senão
                # joga em {"question": <texto>} (campo mais comum).
                msg = (data.message or "").strip()
                inputs: dict = {}
                if msg.startswith("{") and msg.endswith("}"):
                    try:
                        parsed_msg = json.loads(msg)
                        if isinstance(parsed_msg, dict):
                            inputs = parsed_msg
                    except json.JSONDecodeError:
                        # fallback para dict estilo Python: {'a': 1}
                        try:
                            parsed_msg = ast.literal_eval(msg)
                            if isinstance(parsed_msg, dict):
                                inputs = parsed_msg
                        except (ValueError, SyntaxError):
                            pass
                if not inputs and msg:
                    inputs = {"question": msg}

                schema = _extract_inputs_schema(parsed_skill.inputs)
                inputs = _coerce_inputs_by_schema(inputs, schema)

                decl = await execute_declarative(
                    agent=agent_obj,
                    skill_parsed=parsed_skill,
                    inputs=inputs,
                    context=None,
                    session_id=data.session_id,
                    dry_run=False,
                )

                # Adapta saída para o formato esperado pelo workspace.
                # Prioriza context.resposta (output_mapping comum) sobre o JSON
                # cru de bindings_executed.
                ctx_dict = decl.get("context") or {}
                output_text = ""
                if "resposta" in ctx_dict:
                    r = ctx_dict["resposta"]
                    output_text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, indent=2)
                elif decl.get("api_response") is not None:
                    api_resp = decl.get("api_response")
                    output_text = api_resp if isinstance(api_resp, str) else json.dumps(api_resp, ensure_ascii=False, indent=2)
                else:
                    output_text = decl.get("output", "")

                executed = decl.get("bindings_executed") or []
                errors = decl.get("errors") or []
                any_success = any(200 <= b.get("status", 0) < 300 for b in executed)
                final_state = decl.get("final_state", "completed")

                diag_level = "success" if (any_success and not errors) else ("warning" if any_success else "danger")
                diag_text = (
                    f"Modo declarativo: {len(executed)} binding(s) executado(s)" +
                    (f" · {len(errors)} erro(s)" if errors else "")
                )

                exec_log = []
                for b in executed:
                    st = b.get("status", 0)
                    lvl = "success" if 200 <= st < 300 else "danger"
                    exec_log.append({
                        "cat": "api",
                        "icon": "🌐",
                        "title": f"{b.get('method','?')} {b.get('path','?')}",
                        "detail": f"status={st} · {b.get('connector','')}",
                        "level": lvl,
                    })

                result = {
                    "interaction_id": decl.get("interaction_id"),
                    "agent_id": data.agent_id,
                    "output": output_text,
                    "final_state": final_state,
                    "evidence_score": 0.0,
                    "transitions": [],
                    "duration_ms": decl.get("duration_ms"),
                    "status": "completed",
                    "mode": "declarative",
                    "errors": errors,
                    "trace": {
                        "total_steps": len(executed),
                        "evidence_count": 0,
                        "evidence_sources": [],
                        "diagnostics": [{"level": diag_level, "text": diag_text}],
                        "agent_name": agent_obj.get("name", ""),
                        "agent_kind": agent_obj.get("kind", ""),
                        "agent_model": "(declarativo)",
                        "agent_provider": "declarative",
                        "agent_version": agent_obj.get("version", "1.0.0"),
                        "agent_domain": agent_obj.get("domain", ""),
                        "skill_detail": {
                            "name": parsed_skill.name,
                            "version": parsed_skill.frontmatter.version,
                            "execution_mode": "declarative",
                        },
                        "mcp_tools": [],
                        "api_tools_count": len(executed),
                        "api_bindings_executed": executed,
                        "tokens": {"input": 0, "output": 0, "total": 0, "calls": 0, "input_billed_sum": 0, "total_billed": 0},
                        "execution_log": exec_log,
                    },
                }
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
