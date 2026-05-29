import asyncio
import uuid
import json
import os
import re
import ast
from datetime import datetime
import aiofiles
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.auth import require_user
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


@router.post("/chat/stream")
async def chat_stream(data: ChatMessage, request: Request, user: dict = Depends(require_user)):
    """Versão streaming (SSE) do /chat — emite eventos por step do pipeline.

    Mesmo payload do /chat, mas a response é text/event-stream com 1 evento por
    transição relevante: pipeline_start, agent_start, agent_done (ou
    agent_passthrough, agent_skipped, agent_error) e por fim pipeline_done com
    o result completo. Cliente conecta via fetch + ReadableStream e renderiza
    cada evento em tempo real (mostrando o processing_message de cada agente
    enquanto ele roda).

    Só faz sentido pra modo=pipeline (vários steps). Pra modo=agent o /chat
    sync continua sendo o caminho — overhead de SSE não compensa em 1 só step.
    """
    if data.mode != "pipeline":
        raise HTTPException(400, "Stream só suporta mode=pipeline. Use POST /chat pra modo agent.")

    attachments = []
    if data.attachments:
        for att in data.attachments:
            attachments.append({
                "name": att.get("filename", ""),
                "type": att.get("content_type", ""),
                "size": att.get("size", 0),
                "content": att.get("text_content", ""),
            })
    attachments, _rejected = await _filter_attachments_by_agent(attachments, data.agent_id)

    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()  # sentinela pra encerrar o consumidor

    async def _cb(event: dict) -> None:
        await queue.put(event)

    async def _run_pipeline():
        from app.agents.engine import execute_pipeline
        try:
            await execute_pipeline(
                entry_agent_id=data.agent_id,
                user_input=data.message,
                channel=data.channel,
                attachments=attachments,
                progress_callback=_cb,
            )
        except Exception as e:
            await queue.put({"type": "stream_error", "error": str(e)[:300]})
        finally:
            await queue.put(_DONE)

    asyncio.create_task(_run_pipeline())

    async def _event_gen():
        # Heartbeat inicial pra que proxies (Caddy) flushem headers e o browser
        # confirme a conexão antes do primeiro evento real (que pode demorar
        # alguns segundos por causa do LLM).
        yield ":ok\n\n"
        while True:
            item = await queue.get()
            if item is _DONE:
                yield "event: end\ndata: {}\n\n"
                break
            payload = json.dumps(item, ensure_ascii=False, default=str)
            event_name = item.get("type", "message") if isinstance(item, dict) else "message"
            yield f"event: {event_name}\ndata: {payload}\n\n"

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Caddy/nginx: não bufferiza, libera flush
            "Connection": "keep-alive",
        },
    )


@router.post("/chat")
async def chat(data: ChatMessage, request: Request, user: dict = Depends(require_user)):
    """Executa interação (agente individual ou pipeline mesh).

    Auth: cookie de sessão (UI) OU header `X-API-Key: ag_live_...` (integração
    externa). Quando X-API-Key é usado, request.state.api_key_id fica disponível
    pra audit log distinguir UI de chamadas externas.
    """
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
                has_mapping_overflow = any("excede max_bytes" in str(err or "") for err in (decl.get("errors") or []))
                if has_mapping_overflow and decl.get("api_response") is not None:
                    api_resp = decl.get("api_response")
                    output_text = api_resp if isinstance(api_resp, str) else json.dumps(api_resp, ensure_ascii=False, indent=2)
                elif "resposta" in ctx_dict:
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

                # Persistência de sessão/turnos para modo declarativo:
                # - reutiliza session_id informado (consulta futura)
                # - cria nova sessão quando necessário
                requested_session_id = (data.session_id or "").strip() or None
                interaction_id = requested_session_id or decl.get("interaction_id")
                existing_session = await interactions_repo.find_by_id(interaction_id) if interaction_id else None
                if not interaction_id:
                    interaction_id = str(uuid.uuid4())
                if not existing_session:
                    await interactions_repo.create({
                        "id": interaction_id,
                        "title": (data.message or "")[:80].strip(),
                        "agent_id": data.agent_id,
                        "channel": data.channel,
                        "journey_id": data.journey or "",
                        "state": "LogAndClose",
                        "ended_at": datetime.now(),
                    })
                    next_turn = 1
                else:
                    old_turns = await turns_repo.find_all(interaction_id=interaction_id, limit=500)
                    next_turn = max((int(t.get("turn_number") or 0) for t in old_turns), default=0) + 1
                    await interactions_repo.update(interaction_id, {
                        "state": "LogAndClose",
                        "ended_at": datetime.now(),
                    })

                await turns_repo.create({
                    "id": str(uuid.uuid4()),
                    "turn_number": next_turn,
                    "user_text_redacted": data.message,
                    "interaction_id": interaction_id,
                })
                await turns_repo.create({
                    "id": str(uuid.uuid4()),
                    "turn_number": next_turn + 1,
                    "output_text_redacted": output_text,
                    "interaction_id": interaction_id,
                })
                result["interaction_id"] = interaction_id
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


# ═══════════════════════════════════════════════════════════════
# Onda A.1 — Slash command universal: invocação direta de bindings
# ═══════════════════════════════════════════════════════════════
# User pediu (2026-05-29): "para funcionar o context7 mcp que tem
# multiplos parametros o usuario deveria informar o valor de cada
# parametro na sua chamada... pelo workspace, usar a / e aparecer
# os parametros de contexto para preenchimento... deveria ser um
# processo padrão, onde qualquer MCP, API que tenha multiplos
# parametros os mesmos estejam disponiveis dado o contexto".
#
# Esta onda (A.1) atende MCP tools — resolve a causa raiz dos bugs
# Context7 #1-#5 (compressão {action,subject,content} → {operation,query}
# pelo LLM). Ondas A.2-A.4 (PRs futuras) generalizam pra API/RAG/Tabular.


import logging  # noqa: E402

logger = logging.getLogger(__name__)


@router.get("/agents/{agent_id}/skills-context")
async def get_agent_skills_context(agent_id: str, user: dict = Depends(require_user)):
    """Devolve o contexto que dirige o slash command no workspace.

    Pra cada SKILL ativa do agente, lista bindings disponíveis com
    `CanonicalFormSchema` pronto. UI usa pra:
    1. Autocomplete do `/` (lista bindings por SKILL)
    2. Renderizar form inline com os fields canônicos
    3. Enviar payload validado pro endpoint /invoke-binding-direct

    Onda A.1: só MCP. Outros tipos retornam binding_kind="unsupported".

    Returns:
        {
          "agent_id": str,
          "agent_name": str,
          "skills": [
            {
              "skill_id": str,
              "skill_name": str,
              "kind": str,           # subagent | router | aobd
              "bindings": [CanonicalFormSchema, ...]
            }
          ]
        }
    """
    from app.core.database import agents_repo, skills_repo, tools_repo
    from app.mcp.runtime import parse_tool_bindings, match_with_registry
    from app.skill_parser.parser import parse_skill_md
    from app.workspace.binding_schema import (
        normalize_mcp_binding,
        normalize_api_binding_from_skill,
    )

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_id}' não encontrado.")

    # Hoje agent → 1 skill (column skill_id). Generalização futura:
    # skill_ids list pra multi-skill por agente.
    skill_ids: list[str] = []
    if agent.get("skill_id"):
        skill_ids.append(agent["skill_id"])

    skills_out: list[dict] = []
    for sid in skill_ids:
        sk = await skills_repo.find_by_id(sid)
        if not sk:
            continue
        raw_md = sk.get("raw_content") or ""
        try:
            parsed = parse_skill_md(raw_md)
        except Exception as e:
            logger.warning(f"skills_context: parse_skill_md falhou pra {sid}: {e}")
            parsed = None

        bindings_text = (parsed.tool_bindings if parsed else "") or ""
        parsed_tools = parse_tool_bindings(bindings_text)
        enriched = await match_with_registry(parsed_tools, tools_repo)

        bindings_out: list[dict] = []

        # ── MCP bindings: 1 item por tool ──
        for tool in enriched:
            # Só geramos schema canônico pra tools que casaram com o
            # Registry — sem isso não temos id/auth/server pra invocar.
            if not tool.get("db_id") and not tool.get("id"):
                continue
            bindings_out.append(normalize_mcp_binding(tool, skill_md=raw_md))

        # ── Onda A.2: API bindings — 1 item por SKILL declarativa ──
        # Pq não 1 por binding? Porque api_bindings_parsed compartilham
        # ## Inputs via Jinja2 — usuário preenche inputs uma vez e a SKILL
        # roda como unidade (execute_declarative orquestra todos). Slash
        # invoke pra API == invocação da SKILL inteira.
        api_canonical = normalize_api_binding_from_skill(
            skill=sk, skill_md=raw_md, parsed_skill=parsed,
        )
        if api_canonical:
            bindings_out.append(api_canonical)

        skills_out.append({
            "skill_id": sid,
            "skill_name": sk.get("name") or "",
            "kind": sk.get("kind") or "",
            "bindings": bindings_out,
        })

    return {
        "agent_id": agent_id,
        "agent_name": agent.get("name") or "",
        "skills": skills_out,
    }


class _InvokeBindingRequest:
    """Pydantic substituto leve — usamos pydantic.BaseModel real."""
    pass


from pydantic import BaseModel as _BaseModel, Field as _Field  # noqa: E402


class InvokeBindingDirectRequest(_BaseModel):
    """Payload do POST /workspace/invoke-binding-direct.

    Identifica univocamente QUAL binding de QUAL skill de QUAL agente
    deve ser invocado, e os params do user (já preenchidos via form).

    Onda A.1: só `binding_kind="mcp"`. A.2+ adiciona "api"|"rag"|"tabular".
    """
    agent_id: str
    skill_id: str
    binding_kind: str = _Field(..., pattern=r"^(mcp|api|rag|tabular)$")
    binding_id: str
    operation: str = ""
    params: dict = _Field(default_factory=dict)
    timeout: int = 60


@router.post("/invoke-binding-direct")
async def invoke_binding_direct(
    data: InvokeBindingDirectRequest,
    user: dict = Depends(require_user),
):
    """Invoca um binding (Onda A.1: MCP) com payload do user, sem LLM.

    Caminho:
    1. Resolve agent → skill → tool_bindings → enriched tools
    2. Localiza o binding pelo binding_id
    3. Gera schema canônico e valida `data.params` contra ele
    4. Roteia execução:
       - MCP → app.mcp.runtime.execute_tool_call (passa params extras
         direto — _build_call_arguments mapeia pro inputSchema do server)
    5. Loga estruturado workspace.invoke_direct
    6. Retorna {ok, result, schema, payload_sent, latency_ms}

    Onda A.1: bindings A.2+ retornam 501 (Not Implemented).
    """
    import time
    import json as _json
    from app.core.database import agents_repo, skills_repo, tools_repo
    from app.mcp.runtime import parse_tool_bindings, match_with_registry, execute_tool_call
    from app.skill_parser.parser import parse_skill_md
    from app.workspace.binding_schema import (
        normalize_mcp_binding,
        validate_params_against_schema,
    )

    if data.binding_kind not in ("mcp", "api"):
        raise HTTPException(
            501,
            f"binding_kind '{data.binding_kind}' não suportado nesta onda. "
            "Ondas A.1+A.2 cobrem MCP+API; RAG/Tabular vêm em A.3.",
        )

    # 1. Resolve agent + skill (comum aos 2 paths)
    agent = await agents_repo.find_by_id(data.agent_id)
    if not agent:
        raise HTTPException(404, f"Agent '{data.agent_id}' não encontrado.")
    sk = await skills_repo.find_by_id(data.skill_id)
    if not sk:
        raise HTTPException(404, f"Skill '{data.skill_id}' não encontrada.")
    raw_md = sk.get("raw_content") or ""
    try:
        parsed = parse_skill_md(raw_md)
    except Exception as e:
        raise HTTPException(400, f"SKILL.md inválido: {e}")

    # ──────────────────────────────────────────────────────
    # Branch: binding_kind == "api" — Onda A.2
    # ──────────────────────────────────────────────────────
    if data.binding_kind == "api":
        return await _invoke_api_binding_direct(
            data=data, agent=agent, skill=sk, parsed=parsed, raw_md=raw_md,
        )

    # ──────────────────────────────────────────────────────
    # Branch: binding_kind == "mcp" — Onda A.1 (path original abaixo)
    # ──────────────────────────────────────────────────────
    # 2. Resolve binding (MCP tool) pelo binding_id (= db_id no Registry)
    bindings_text = (parsed.tool_bindings if parsed else "") or ""
    parsed_tools = parse_tool_bindings(bindings_text)
    enriched = await match_with_registry(parsed_tools, tools_repo)
    tool = next(
        (t for t in enriched if str(t.get("db_id") or t.get("id") or "") == data.binding_id),
        None,
    )
    if not tool:
        raise HTTPException(
            404,
            f"Binding '{data.binding_id}' não está em ## Tool Bindings da skill "
            f"'{data.skill_id}'. Conferi ID no /tools e em ## Tool Bindings.",
        )

    # 3. Gera schema canônico e valida params
    schema = normalize_mcp_binding(tool, skill_md=raw_md)
    ok, errors = validate_params_against_schema(schema, data.params or {})
    if not ok:
        raise HTTPException(422, {"errors": errors, "schema": schema})

    # 4. Monta arguments pro execute_tool_call. Critical: passa os params
    # do user direto — _build_call_arguments do runtime mapeia pro
    # inputSchema REAL do servidor MCP. Sem LLM compressing nada.
    arguments: dict = {}
    if data.operation:
        arguments["operation"] = data.operation
    elif schema.get("operations"):
        arguments["operation"] = schema["operations"][0]
    # Heurística pra preencher 'query' quando o user mandou só um campo
    # textual — runtime espera 'query' como fallback. Se houver field
    # explícito 'query', já vai.
    arguments.update(data.params or {})
    if "query" not in arguments:
        # Pega o primeiro field string preenchido como query default —
        # melhora compat com servidores MCP que esperam 'query' obrigatório
        for f in schema.get("fields", []):
            if f["name"] != "operation" and f["type"] in ("string", "enum"):
                val = (data.params or {}).get(f["name"])
                if isinstance(val, str) and val.strip():
                    arguments["query"] = val
                    break

    # 5. Executa + mede latência
    t0 = time.monotonic()
    tool_name = tool.get("name") or "tool"
    try:
        result_raw = await execute_tool_call(
            tool_name=tool_name,
            arguments=arguments,
            mcp_tools=enriched,
            timeout=int(data.timeout or 60),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "workspace.invoke_direct.error",
            extra={
                "event": "workspace.invoke_direct",
                "agent_id": data.agent_id,
                "skill_id": data.skill_id,
                "binding_kind": data.binding_kind,
                "binding_id": data.binding_id,
                "tool_name": tool_name,
                "operation": arguments.get("operation"),
                "latency_ms": latency_ms,
                "error": str(e)[:300],
            },
        )
        raise HTTPException(500, f"Erro ao invocar tool: {str(e)[:300]}")

    # 6. Parsing oportunista do JSON de retorno — se for JSON serializado,
    # devolvemos o objeto pra UI poder renderizar bonito; senão devolve raw.
    result_obj: object = result_raw
    if isinstance(result_raw, str):
        try:
            result_obj = _json.loads(result_raw)
        except (ValueError, TypeError):
            result_obj = result_raw  # mantém string

    # 7. Log estruturado (auditoria)
    is_error = (
        isinstance(result_obj, dict)
        and ("error" in result_obj)
    )
    logger.info(
        "workspace.invoke_direct.completed",
        extra={
            "event": "workspace.invoke_direct",
            "agent_id": data.agent_id,
            "skill_id": data.skill_id,
            "binding_kind": data.binding_kind,
            "binding_id": data.binding_id,
            "tool_name": tool_name,
            "operation": arguments.get("operation"),
            "schema_source": schema.get("schema_source"),
            "latency_ms": latency_ms,
            "ok": not is_error,
            "param_fields_sent": sorted(list((data.params or {}).keys())),
        },
    )

    return {
        "ok": not is_error,
        "result": result_obj,
        "result_raw": result_raw if isinstance(result_raw, str) else None,
        "schema": schema,
        "payload_sent": arguments,
        "latency_ms": latency_ms,
        "tool_name": tool_name,
    }


# ═══════════════════════════════════════════════════════════════
# Onda A.2 — Helper de invocação de API (declarativa)
# ═══════════════════════════════════════════════════════════════


async def _invoke_api_binding_direct(
    *,
    data: "InvokeBindingDirectRequest",
    agent: dict,
    skill: dict,
    parsed,
    raw_md: str,
):
    """Invoca SKILL declarativa via execute_declarative (sem LLM).

    Diferente de MCP (1 tool por chamada), API roda a SKILL inteira —
    todos os api_bindings_parsed são orquestrados pelo declarative_engine
    com retry, output_mapping, compensation, etc. O user só fornece os
    inputs (params), o engine cuida do resto.

    Returns mesmo shape do MCP path: {ok, result, schema, payload_sent,
    latency_ms, tool_name (= skill.name aqui), declarative (extras)}.
    """
    import time
    from app.workspace.binding_schema import (
        normalize_api_binding_from_skill,
        validate_params_against_schema,
    )

    # 1. Confirma que skill_id casa com binding_id (single-skill aware)
    if str(skill.get("id") or "") != data.binding_id:
        raise HTTPException(
            404,
            f"Binding API '{data.binding_id}' não corresponde à skill "
            f"'{data.skill_id}'. Em API, binding_id deve ser o skill_id.",
        )

    # 2. Gera schema canônico (revalida que skill é declarativa + tem bindings)
    schema = normalize_api_binding_from_skill(skill, skill_md=raw_md, parsed_skill=parsed)
    if not schema:
        raise HTTPException(
            422,
            f"Skill '{data.skill_id}' não é declarativa OU não tem "
            "## API Bindings parseável. Apenas declarativas suportam "
            "invoke-binding-direct kind='api'.",
        )

    # 3. Valida params contra schema (required + enum)
    ok, errors = validate_params_against_schema(schema, data.params or {})
    if not ok:
        raise HTTPException(422, {"errors": errors, "schema": schema})

    # 4. Coerge inputs por tipo (engine declarativo é estrito)
    inputs = dict(data.params or {})
    inputs_schema_dict = _extract_inputs_schema(parsed.inputs or "")
    if inputs_schema_dict:
        inputs = _coerce_inputs_by_schema(inputs, inputs_schema_dict)

    # 5. Executa declarativo
    t0 = time.monotonic()
    try:
        from app.agents.declarative_engine import execute_declarative
        decl = await execute_declarative(
            agent=agent,
            skill_parsed=parsed,
            inputs=inputs,
            context=None,
            session_id="",  # slash invoke é stateless
            dry_run=False,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "workspace.invoke_direct.api_error",
            extra={
                "event": "workspace.invoke_direct",
                "agent_id": data.agent_id,
                "skill_id": data.skill_id,
                "binding_kind": "api",
                "latency_ms": latency_ms,
                "error": str(e)[:300],
            },
        )
        raise HTTPException(500, f"Erro ao executar SKILL declarativa: {str(e)[:300]}")

    # 6. Adapta saída — mesma lógica do /chat declarativo (já testada)
    import json as _json
    ctx_dict = decl.get("context") or {}
    has_mapping_overflow = any(
        "excede max_bytes" in str(err or "") for err in (decl.get("errors") or [])
    )
    if has_mapping_overflow and decl.get("api_response") is not None:
        api_resp = decl.get("api_response")
        output_text = api_resp if isinstance(api_resp, str) else _json.dumps(api_resp, ensure_ascii=False, indent=2)
        result_obj = api_resp
    elif "resposta" in ctx_dict:
        r = ctx_dict["resposta"]
        output_text = r if isinstance(r, str) else _json.dumps(r, ensure_ascii=False, indent=2)
        result_obj = r
    elif decl.get("api_response") is not None:
        api_resp = decl.get("api_response")
        output_text = api_resp if isinstance(api_resp, str) else _json.dumps(api_resp, ensure_ascii=False, indent=2)
        result_obj = api_resp
    else:
        output_text = decl.get("output", "")
        result_obj = output_text

    executed = decl.get("bindings_executed") or []
    errors_out = decl.get("errors") or []
    any_success = any(200 <= b.get("status", 0) < 300 for b in executed)
    is_ok = bool(any_success and not errors_out)

    # 7. Log estruturado (auditoria)
    logger.info(
        "workspace.invoke_direct.api_completed",
        extra={
            "event": "workspace.invoke_direct",
            "agent_id": data.agent_id,
            "skill_id": data.skill_id,
            "binding_kind": "api",
            "binding_id": data.binding_id,
            "skill_name": skill.get("name") or "",
            "schema_source": schema.get("schema_source"),
            "latency_ms": latency_ms,
            "ok": is_ok,
            "bindings_executed_count": len(executed),
            "errors_count": len(errors_out),
            "param_fields_sent": sorted(list((data.params or {}).keys())),
        },
    )

    return {
        "ok": is_ok,
        "result": result_obj,
        "result_raw": output_text if isinstance(output_text, str) else None,
        "schema": schema,
        "payload_sent": inputs,
        "latency_ms": latency_ms,
        "tool_name": skill.get("name") or "",
        # Extras do declarativo pra UI mostrar (opcionalmente)
        "declarative": {
            "bindings_executed": executed,
            "errors": errors_out,
            "final_state": decl.get("final_state", "completed"),
        },
    }
