import asyncio
import uuid
import json
import os
import re
import ast
from datetime import datetime

from app.core.datetime_utils import naive_utc_now
import aiofiles
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.auth import require_user
from app.models.schemas import ChatMessage
from app.agents.engine import execute_interaction
from app.core.database import interactions_repo, turns_repo

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"


def _safe_upload_name(filename: str, file_id: str) -> str:
    """Nome de arquivo seguro p/ gravação — anti path-traversal (CWE-22).

    Usa só o BASENAME do nome enviado (descarta componentes de diretório e os
    separadores `/` e `\\`), neutraliza `..` e `:` residuais, colapsa espaços e
    limita o tamanho. O nome do cliente NUNCA controla o diretório de destino.
    """
    raw = (filename or "upload").replace("\\", "/")
    base = raw.split("/")[-1].strip() or "upload"
    base = base.replace("..", "_").replace(":", "_").replace(" ", "_")[:150] or "upload"
    return f"{file_id}_{base}"

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


def _single_required_input(schema: dict | None) -> str | None:
    """Nome do ÚNICO campo de input quando o mapeamento texto-livre→input é
    inequívoco: 1 campo em `required`, ou (sem required) 1 `property`. Caso
    contrário None — uma única mensagem de texto não tem como preencher múltiplos
    campos (multi-input exige inputs estruturados via JSON na mensagem)."""
    if not isinstance(schema, dict):
        return None
    required = schema.get("required")
    if isinstance(required, list) and len(required) == 1 and isinstance(required[0], str):
        return required[0]
    props = schema.get("properties")
    if isinstance(props, dict) and len(props) == 1:
        return next(iter(props))
    return None


# Token `campo=valor`: valor entre aspas (com espaços) OU run sem espaço.
_KV_RE = re.compile(r"""([A-Za-z_]\w*)\s*=\s*("[^"]*"|'[^']*'|\S+)""")


def _parse_kv_message(msg: str, schema: dict | None) -> dict | None:
    """Parseia `campo=valor campo2=valor2` → dict — só quando a mensagem é
    PURAMENTE pares chave=valor E todas as chaves são propriedades do
    inputs_schema. Senão None (cai no mapeamento de texto livre / compilador NL).

    Permite WHERE multi-campo no chat sem digitar JSON:
      `nr_idade=35 vr_renda=5000`  ou  `uf="Rio Grande" nr_idade=35`.
    Valores ficam string — `_coerce_inputs_by_schema` converte o tipo depois.
    Gate anti-falso-positivo: prosa com '=' no meio (ex.: "clientes com
    renda=5000 acima") deixa resto não-vazio → None (vai pro caminho NL).
    """
    if not msg or "=" not in msg:
        return None
    props = (schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return None
    matches = list(_KV_RE.finditer(msg))
    if not matches:
        return None
    if _KV_RE.sub("", msg).strip():       # sobrou texto fora dos pares → não é k=v puro
        return None
    out: dict = {}
    for m in matches:
        key, val = m.group(1), m.group(2)
        if key not in props:              # chave fora do schema → não estruturado
            return None
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]               # tira aspas
        out[key] = val
    return out or None


def _t2_log(event: str, **fields):
    import logging as _logging
    _logging.getLogger("app.routes.workspace").warning(event, extra={"event": event, **fields})


async def _nl_table_answer(parsed_skill, msg: str, user: dict,
                           session_id, agent_id) -> dict | None:
    """Tier 2 text-to-SQL no CHAT: TEXTO LIVRE em PT-BR → struct governado → execução.

    Reusa as peças Tier 2 já na main: compile_question (governado pelo Catálogo —
    allow-list, anti-PII, caps; o LLM emite struct, nunca SQL), execute_query
    (read_only, audited) e declarative_engine._default_table_answer (markdown +
    mask PII-catalogada). Gated por TEXT_TO_SQL_ENABLED. TODOS os imports são LAZY
    (não acopla a rota de chat à feature opcional/DuckDB).

    Retorna {output_text, errors, duration_ms} quando RESPONDE (sucesso OU
    degradação amigável), ou **None** p/ DEGRADAR ao fallback do chat (flag off,
    skill sem tabela, catálogo não-curado, sem permissão, LLM indisponível) —
    ZERO regressão no comportamento atual.
    """
    # (1) GATING — flag default OFF (lida a cada chamada; toggle runtime).
    try:
        from app.data_tables.runtime import text_to_sql_enabled
    except ImportError:
        return None
    if not text_to_sql_enabled():
        return None

    # (2) Tabela: 1ª Data Table declarada na skill (resolve URN → row + catálogo).
    dts = getattr(parsed_skill, "data_tables_parsed", []) or []
    table_ref = str((dts[0] or {}).get("table_ref") or "") if dts else ""
    if not table_ref:
        return None
    try:
        from app.data_tables.queries import (
            find_by_urn_with_ks, find_by_id_with_ks, can_user_see,
        )
        row = (await find_by_urn_with_ks(table_ref)) if table_ref.startswith("urn:table:") \
            else (await find_by_id_with_ks(table_ref))
    except Exception:
        return None
    if not row:
        return None

    # (3) Visibilidade — degrada p/ None (NÃO 403; não derruba a bolha).
    if not can_user_see(user, row):
        return None

    # (4) Catálogo curado — sem coluna liberada, não tenta (fail-safe deny).
    catalog = row.get("catalog") or {}
    from app.data_tables.governance import allowed_cols_from_catalog
    allowed = allowed_cols_from_catalog(catalog)
    if not allowed:
        return None

    # (5) Anti-injeção (OWASP LLM01) ANTES do LLM — bolha neutra, não ecoa a pergunta.
    from app.core.prompt_guard import detect as _pg_detect
    guard = _pg_detect(msg)
    if guard.blocked:
        _t2_log("text_to_sql.prompt_blocked", score=round(guard.score, 3))
        return {"output_text": "Não consegui interpretar sua pergunta com segurança. "
                "Reformule de forma direta — cite a coluna e o valor, ou use "
                "campo=valor (ex.: cd_cliente=123).", "errors": [], "duration_ms": None}

    # (6) Amostra read-only p/ o prompt: só public/internal e SÓ colunas liberadas
    # (PII de base sensível nunca vai ao provedor LLM). Best-effort.
    from app.evidence.tabular import execute_query
    sample_rows: list = []
    label = str(row.get("ks_confidentiality_label") or "internal").lower()
    if label in ("public", "internal"):
        try:
            sres = await execute_query(row["id"], select=allowed, limit=10,
                                       executed_by=user.get("id"))
            sample_rows = sres.get("rows", []) or []
        except Exception:
            sample_rows = []

    # (7) Compila NL → struct (LLM determinístico, temp 0 + JSON-mode). 503/erro → degrada.
    try:
        from app.llm_routing import resolve_llm_for_task
        from app.routes.wizard import _wizard_llm_complete
        from app.data_tables.text_to_sql import compile_question
        provider, model = await resolve_llm_for_task("instruct")

        async def _complete(messages):
            content, _, _ = await _wizard_llm_complete(
                messages, provider, model, route="text_to_sql_compile",
                temperature=0.0, response_format={"type": "json_object"})
            return content

        comp = await compile_question(row, catalog, sample_rows, msg, _complete)
    except Exception as e:
        _t2_log("text_to_sql.chat_compile_failed", error=str(e)[:200])
        return None  # degrada ao fallback (não derruba o /chat)

    compiled = comp.get("compiled") or {}
    if comp.get("note"):                         # tabela sem coluna liberada (acionável)
        return {"output_text": comp["note"], "errors": [], "duration_ms": None}
    select = compiled.get("select") or []
    if not select:
        # struct vazio: NÃO executar — select=[] vira '*' e VAZARIA colunas
        # não-liberadas (anti-vazamento). Devolve dica + o que a governança barrou.
        blocked = comp.get("blocked") or []
        hint = (" Barrado pela governança: " + "; ".join(blocked[:3])) if blocked else ""
        return {"output_text": "Não consegui mapear sua pergunta para as colunas disponíveis "
                "desta tabela. Cite uma coluna ou use campo=valor." + hint,
                "errors": [], "duration_ms": None}

    # (8) Executa (read_only) + formata via _default_table_answer (markdown + mask PII).
    try:
        res = await execute_query(
            table_id=row["id"], select=select, filters=compiled.get("filters") or [],
            order_by=compiled.get("order_by") or [], limit=compiled.get("limit") or 100,
            executed_by=user.get("id"), interaction_id=session_id or None, agent_id=agent_id)
    except Exception as e:
        _t2_log("text_to_sql.chat_execute_failed", error=str(e)[:200])
        return None
    from app.agents.declarative_engine import _default_table_answer
    rec = {"kind": "table", "status": 200,
           "response_data": {"rows": res.get("rows") or [], "columns": res.get("columns") or []},
           "_table_meta": {"name": row.get("name") or "", "catalog": catalog}}
    output_text = _default_table_answer([rec]) or "Nenhum registro encontrado para os critérios informados."
    return {"output_text": output_text, "errors": [], "duration_ms": res.get("duration_ms")}


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

    # Restaurar trace_data persistido. Estabilizar campos críticos para
    # evitar "undefinedms / 0 transições" no frontend quando sessões
    # antigas/parciais tinham JSON minimalista.
    trace_data = None
    raw_trace = s.get("trace_data")
    if raw_trace:
        try:
            parsed = json.loads(raw_trace)
            if isinstance(parsed, dict):
                trace_data = parsed
        except (json.JSONDecodeError, TypeError) as e:
            # Logger é declarado mais abaixo no módulo (linha ~689) — usar
            # getLogger inline pra evitar acoplamento com a ordem de
            # carregamento (mesmo pattern de logger inline já usado em
            # _enforce_skill_input_schema).
            import logging as _logging
            _logging.getLogger("app.routes.workspace").warning(
                "workspace.session.trace_data_parse_failed",
                extra={
                    "event": "workspace.session.trace_data",
                    "session_id": session_id,
                    "raw_preview": str(raw_trace)[:200],
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )

    # 2026-06-01 (3ª revisão): trace_data SEMPRE não-null quando há sessão.
    # User pediu: painéis Rastreabilidade + Execution Log SEMPRE visíveis,
    # mesmo para sessões antigas. Frontend confia que `trace` sempre
    # existe — placeholders ("—", 0, []) em vez de UI escondida.
    if trace_data is None:
        trace_data = {}  # placeholder vazio, defaults preenchidos abaixo
    _defaults = {
        "interaction_id": session_id,
        "agent_id": s.get("agent_id"),
        "final_state": s.get("state") or "Unknown",
        "duration_ms": 0,
        "transitions": [],
        "evidence_score": 0,
        "pipeline_steps": [],
        "trace": {},
    }
    for k, v in _defaults.items():
        if trace_data.get(k) is None:
            trace_data[k] = v
    # `trace` sub-objeto também precisa estrutura mínima — frontend lê
    # trace.execution_log e trace.evidence_count.
    if isinstance(trace_data.get("trace"), dict):
        trace_data["trace"].setdefault("execution_log", [])
        trace_data["trace"].setdefault("evidence_count", 0)
    # mode: 'pipeline' se há steps; 'agent' caso contrário.
    if not trace_data.get("mode"):
        trace_data["mode"] = "pipeline" if trace_data.get("pipeline_steps") else "agent"
    # Marcador opcional de "trace é placeholder vs real" — não usado pra
    # esconder UI, mantido pra observability/debug.
    trace_data.setdefault(
        "_has_real_trace",
        bool(
            trace_data.get("transitions")
            or trace_data.get("pipeline_steps")
            or (isinstance(trace_data.get("trace"), dict) and trace_data["trace"].get("execution_log"))
        ),
    )

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
    from app.core.config import get_settings
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_id = str(uuid.uuid4())[:8]
    safe_name = _safe_upload_name(file.filename, file_id)
    file_path = UPLOAD_DIR / safe_name

    # Defesa em profundidade: confirma que o caminho resolvido fica DENTRO de
    # UPLOAD_DIR (anti path-traversal, mesmo que _safe_upload_name mude).
    base_dir = UPLOAD_DIR.resolve()
    dest = file_path.resolve()
    if dest != base_dir and not str(dest).startswith(str(base_dir) + os.sep):
        raise HTTPException(400, "Nome de arquivo inválido")

    # Leitura em chunks com teto de tamanho — anti-DoS de memória/disco (CWE-400).
    max_bytes = max(1, get_settings().max_upload_mb) * 1024 * 1024
    total = 0
    parts: list[bytes] = []
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                413, f"Arquivo excede o limite de {get_settings().max_upload_mb} MB"
            )
        parts.append(chunk)
    content_bytes = b"".join(parts)

    async with aiofiles.open(str(file_path), "wb") as f:
        await f.write(content_bytes)

    # Tentar ler como texto para passar ao agente.
    # Ordem: UTF-8 direto → markitdown (PDF/PPTX/DOCX/XLSX/imagens/audio) →
    # placeholder descritivo. Antes (até 2026-06-01) o handler caía direto no
    # placeholder pra qualquer binário, sem invocar markitdown — o LLM recebia
    # "[Arquivo binário: relatorio.pdf]" como input e respondia parafraseando
    # ("não foi possível ler arquivo binário"). markitdown já vivia em
    # requirements.txt + app/evidence/converters.py mas só era usado pelo
    # pipeline RAG.
    text_content = None
    try:
        text_content = content_bytes.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        try:
            # Import lazy: markitdown puxa PIL/soundfile/etc. Falha de import
            # vira ConverterError 503 mas aqui tratamos como warning sem
            # quebrar o upload — agente recebe placeholder e responde.
            from app.evidence.converters import convert_bytes
            text_content = convert_bytes(
                content_bytes, file.filename, file.content_type
            )
        except Exception as conv_e:  # pragma: no cover - conversor falhou
            import logging as _logging
            _logging.getLogger("app.routes.workspace").warning(
                "workspace.upload.text_extract_failed",
                extra={
                    "event": "workspace.upload",
                    # 'filename' é atributo built-in do LogRecord — usar nome
                    # diferente pra não disparar KeyError no logging.
                    "attachment_name": file.filename,
                    "content_type": file.content_type,
                    "size": len(content_bytes),
                    "error_type": type(conv_e).__name__,
                    "error_msg": str(conv_e)[:200],
                },
            )
            text_content = (
                f"[Arquivo binário não convertido: {file.filename}, "
                f"{len(content_bytes)} bytes, tipo: {file.content_type}]"
            )

    if text_content and len(text_content) > 50000:
        text_content = text_content[:50000] + "\n\n[...truncado em 50.000 caracteres]"

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


async def _chain_capabilities(entry_agent_id: str, entry: dict | None = None) -> tuple[bool, bool]:
    """União de accepts_images / accepts_documents da CADEIA do mesh a partir de
    `entry_agent_id` (entrada + todos os agentes downstream alcançáveis via BFS).
    Retorna (accepts_images, accepts_documents).

    Motivo (bug real "Doc Analise", 2026-06-06): um ROTEADOR *dispatcher* não
    ingere anexos — ele roteia. Filtrar a porta do pipeline só pelas flags do
    dispatcher PODA o documento ANTES de ele chegar ao especialista que o aceita
    (sintoma observado: "0 Anexos", SAs skipped_conditional, Refuse por evidência
    insuficiente). Unindo a cadeia, o anexo passa pela porta sse QUALQUER agente
    do pipeline aceita aquele tipo; o motor (forwarding por step, PR B) já entrega
    cada anexo só ao SA com capacidade — então não há vazamento p/ quem não trata.

    Degrada com elegância: agente-folha sem downstream → união = as próprias flags
    (idêntico ao comportamento legado). Fail-open: erro no traversal cai pras flags
    do agente de entrada (nunca poda demais por falha de infra)."""
    from app.core.database import agents_repo
    if entry is None:
        entry = await agents_repo.find_by_id(entry_agent_id) or {}
    accepts_img = bool(entry.get("accepts_images") or 0)
    accepts_doc = bool(entry.get("accepts_documents") or 0)
    if accepts_img and accepts_doc:
        return True, True  # entrada já cobre ambos — nada a unir
    try:
        from app.agents.engine import _resolve_ordered_chain
        chain = await _resolve_ordered_chain(entry_agent_id)
    except Exception as e:
        import logging as _logging
        _logging.getLogger("app.routes.workspace").warning(
            "chain_capabilities: traversal do mesh falhou — fail-open p/ flags da entrada",
            extra={
                "event": "workspace.chain_capabilities.error",
                "entry_agent_id": entry_agent_id,
                "error": str(e)[:200],
            },
        )
        return accepts_img, accepts_doc
    for aid in chain:
        if aid == entry_agent_id:
            continue
        a = await agents_repo.find_by_id(aid)
        if not a:
            continue
        accepts_img = accepts_img or bool(a.get("accepts_images") or 0)
        accepts_doc = accepts_doc or bool(a.get("accepts_documents") or 0)
        if accepts_img and accepts_doc:
            break  # short-circuit: a cadeia já cobre os dois tipos
    return accepts_img, accepts_doc


_REJECTED_KIND_PT = {"image": "imagens", "document": "documentos", "other": "este tipo de arquivo"}


def _rejected_attachments_message(rejected: list) -> str:
    """Mensagem clara/acionável quando TODOS os anexos foram podados na porta
    (nenhum agente da cadeia aceita o tipo). Substitui a resposta CEGA do agente
    ("sem evidências…") — o opt-in (accepts_images/documents) é mantido, só o
    feedback ao usuário muda. Pedido do usuário (2026-06-07)."""
    names = ", ".join(f'“{r.get("name", "arquivo")}”' for r in rejected) or "o anexo"
    kinds = sorted({(r.get("kind") or "other") for r in rejected})
    kinds_txt = " e ".join(_REJECTED_KIND_PT.get(k, k) for k in kinds)
    return (
        f"⚠️ Anexo não processado: {names}.\n\n"
        f"Nenhum agente deste fluxo aceita {kinds_txt}. Para usar este anexo, "
        f"habilite a aceitação de {kinds_txt} em **Editar Agente** (no agente de "
        f"entrada ou em um subagente da cadeia), ou escolha um agente/pipeline com "
        f"essa capacidade — e reenvie."
    )


async def _filter_attachments_by_agent(
    attachments: list, agent_id: str, *, include_chain: bool = False
) -> tuple[list, list]:
    """Filtra attachments conforme flags accepts_images / accepts_documents.
    Retorna (aceitos, rejeitados_meta) — rejeitados vão para o trace para o
    usuário ver o que foi podado.

    `include_chain` (default False = comportamento legado, decide só pelo agente
    de entrada): quando True, decide pela UNIÃO de capacidades da cadeia do mesh
    (entrada + downstream). Use True em modo pipeline — assim um roteador
    dispatcher (accepts_*=0) não poda o anexo destinado ao especialista
    downstream que o aceita. Ver `_chain_capabilities`."""
    if not attachments:
        return [], []
    from app.core.database import agents_repo
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        return attachments, []  # fail-open: agente desconhecido não poda nada
    if include_chain:
        accepts_img, accepts_doc = await _chain_capabilities(agent_id, entry=agent)
    else:
        accepts_img = bool(agent.get("accepts_images") or 0)
        accepts_doc = bool(agent.get("accepts_documents") or 0)
    accepted, rejected = [], []
    for att in attachments:
        kind = _classify_attachment(att.get("type", ""))
        allowed = (kind == "image" and accepts_img) or (kind == "document" and accepts_doc)
        if allowed:
            accepted.append(att)
        else:
            reason = (
                f"Nenhum agente do pipeline aceita {kind}s — habilite em 'Editar Agente'"
                if include_chain else
                f"Agente não aceita {kind}s — habilite em 'Editar Agente'"
            )
            rejected.append({
                "name": att.get("name", ""),
                "type": att.get("type", ""),
                "kind": kind,
                "reason": reason,
            })
    if rejected:
        # Observabilidade: anexo podado nunca mais some em silêncio (foi o que
        # escondeu o bug "Doc Analise"). Log estruturado + (no SSE) evento de
        # trace pro usuário VER o que foi descartado e por quê.
        import logging as _logging
        _logging.getLogger("app.routes.workspace").info(
            "anexo(s) podado(s) na porta do pipeline",
            extra={
                "event": "workspace.attachment.rejected",
                "entry_agent_id": agent_id,
                "include_chain": include_chain,
                "chain_accepts_images": accepts_img,
                "chain_accepts_documents": accepts_doc,
                "rejected_count": len(rejected),
                "rejected_names": [r["name"] for r in rejected],
                "rejected_kinds": [r["kind"] for r in rejected],
            },
        )
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
                # Caminho absoluto saneado (só basename dentro de UPLOAD_DIR — sem
                # path traversal) p/ o engine ler os bytes e enviar a imagem como
                # conteúdo multimodal (image_url) a modelos de visão.
                "abs_path": str(UPLOAD_DIR / Path(att.get("path", "") or "").name)
                if att.get("path") else "",
            })
    # include_chain=True: a porta decide pela UNIÃO das capacidades da cadeia
    # do mesh (entrada + downstream). Um roteador dispatcher (accepts_*=0) deixa
    # de podar o anexo destinado ao especialista — fix do dead-end "Doc Analise".
    attachments, _rejected = await _filter_attachments_by_agent(
        attachments, data.agent_id, include_chain=True
    )
    # Curto-circuito: usuário anexou arquivo(s) e TODOS foram podados na porta →
    # não roda o pipeline CEGO (que devolve "sem evidências"); manda mensagem clara.
    _all_rejected = bool(data.attachments) and not attachments and bool(_rejected)

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
                # Reusa interaction existente quando frontend manda session_id
                # — só o primeiro agente do pipeline reaproveita (subagentes
                # geram child_interaction_ids próprios em execute_pipeline).
                session_id=data.session_id,
                # Memória de conversa (2026-06-06): 'auto' reinjeta o histórico
                # da sessão no roteador/orquestrador. 'none' = stateless.
                context_mode=data.context_mode or "auto",
            )
        except Exception as e:
            await queue.put({"type": "stream_error", "error": str(e)[:300]})
        finally:
            await queue.put(_DONE)

    if not _all_rejected:
        asyncio.create_task(_run_pipeline())

    async def _event_gen():
        # Heartbeat inicial pra que proxies (Caddy) flushem headers e o browser
        # confirme a conexão antes do primeiro evento real (que pode demorar
        # alguns segundos por causa do LLM).
        yield ":ok\n\n"
        # Anexo(s) podado(s) na porta: avisa o usuário ANTES dos steps pra que
        # ele veja por que o arquivo não foi usado (em vez do dead-end silencioso
        # "0 Anexos"). Só dispara quando há rejeição.
        if _rejected:
            _rej_payload = json.dumps(
                {"type": "attachments_rejected", "rejected": _rejected},
                ensure_ascii=False, default=str,
            )
            yield f"event: attachments_rejected\ndata: {_rej_payload}\n\n"
        if _all_rejected:
            # Não há pipeline rodando: devolve a resposta clara como pipeline_done
            # (o frontend renderiza o balão a partir de result.output) e encerra.
            _done = json.dumps({"type": "pipeline_done", "result": {
                "output": _rejected_attachments_message(_rejected),
                "final_state": "Refuse", "mode": "pipeline",
                "rejected_attachments": _rejected, "transitions": [],
                "duration_ms": 0, "trace": {}, "pipeline_steps": [],
            }}, ensure_ascii=False, default=str)
            yield f"event: pipeline_done\ndata: {_done}\n\n"
            yield "event: end\ndata: {}\n\n"
            return
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
                    # Caminho absoluto saneado (basename dentro de UPLOAD_DIR) p/ o
                    # engine ler os bytes e mandar a imagem como image_url multimodal.
                    "abs_path": str(UPLOAD_DIR / Path(att.get("path", "") or "").name)
                    if att.get("path") else "",
                })
        # Filtra conforme flags do agente. Em pipeline, decide pela UNIÃO da
        # cadeia do mesh (dispatcher não poda anexo do especialista downstream);
        # em modo agente único, mantém a decisão só pelo agente de entrada.
        attachments, rejected_attachments = await _filter_attachments_by_agent(
            attachments, data.agent_id, include_chain=(data.mode == "pipeline")
        )
        # Anexo(s) rejeitado(s) na porta (nenhum agente aceita o tipo): em vez de
        # rodar o agente/pipeline CEGO, devolve mensagem clara e acionável. Opt-in
        # mantido; só o feedback muda. Vale p/ modo agente e pipeline.
        if data.attachments and not attachments and rejected_attachments:
            return {
                "agent_id": data.agent_id,
                "output": _rejected_attachments_message(rejected_attachments),
                "final_state": "Refuse",
                "status": "rejected_attachments",
                "duration_ms": 0,
                "evidence_score": 0,
                "transitions": [],
                "trace": {},
                "verification": {},
                "interaction_id": None,
                "rejected_attachments": rejected_attachments,
                "mode": data.mode,
            }

        if data.mode == "pipeline":
            from app.agents.engine import execute_pipeline
            result = await execute_pipeline(
                entry_agent_id=data.agent_id,
                user_input=data.message,
                channel=data.channel,
                attachments=attachments,
                session_id=data.session_id,
                context_mode=data.context_mode or "auto",
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
                schema = _extract_inputs_schema(parsed_skill.inputs)
                nl_answer = None
                if not inputs and msg:
                    # Precedência: (1) JSON já tratado acima; (2) `campo=valor`
                    # estruturado (WHERE multi-campo); (3) TEXTO LIVRE → compilador
                    # Tier 2 (gated, governado pelo Catálogo) quando NÃO há input
                    # único nomeado; (4) texto-único → input NOMEADO (lookup por
                    # PK — protege o fix das 0 linhas silenciosas, bug 2026-06-10);
                    # (5) genérico {"question": msg}.
                    kv = _parse_kv_message(msg, schema)
                    if kv:
                        inputs = kv
                    else:
                        # Ramo NL só p/ texto livre genuíno (sem input único). Degrada
                        # p/ None em qualquer não-aplicação → fallback abaixo (zero regressão).
                        if _single_required_input(schema) is None:
                            nl_answer = await _nl_table_answer(
                                parsed_skill=parsed_skill, msg=msg, user=user,
                                session_id=data.session_id, agent_id=data.agent_id,
                            )
                        if nl_answer is None:
                            target = _single_required_input(schema)
                            inputs = {target: msg} if target else {"question": msg}

                if nl_answer is not None:
                    # Tier 2 respondeu (sucesso ou degradação amigável): a query
                    # compilada substitui o ## Data Tables da skill → NÃO roda o
                    # engine declarativo. Monta as mesmas variáveis do caminho comum.
                    output_text = nl_answer["output_text"]
                    executed = []
                    errors = nl_answer.get("errors") or []
                    final_state = "completed"
                    any_success = True
                    diag_level = "success" if not errors else "warning"
                    diag_text = "Consulta em linguagem natural (Tier 2 text-to-SQL)"
                    exec_log = []
                    decl = {"interaction_id": data.session_id,
                            "duration_ms": nl_answer.get("duration_ms")}
                else:
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
                    # Frase humana do ## Response Template tem PRECEDÊNCIA: quando a skill
                    # tem o template, o engine devolve `answer` (texto puro) → vira a
                    # bolha do assistente em vez do JSON estruturado de bindings.
                    if decl.get("answer"):
                        output_text = decl["answer"]
                    elif has_mapping_overflow and decl.get("api_response") is not None:
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
                        "ended_at": naive_utc_now(),
                    })
                    next_turn = 1
                else:
                    old_turns = await turns_repo.find_all(interaction_id=interaction_id, limit=500)
                    next_turn = max((int(t.get("turn_number") or 0) for t in old_turns), default=0) + 1
                    await interactions_repo.update(interaction_id, {
                        "state": "LogAndClose",
                        "ended_at": naive_utc_now(),
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
                    context_mode=data.context_mode or "auto",
                )

        # Persistir trace_data. Estabilizar defaults pra evitar campos
        # missing que viram "undefinedms" no frontend de sessão antiga.
        iid = result.get("interaction_id")
        if iid:
            # "verification" incluída (24.10.0): o painel Verifier do Workspace
            # agora RESTAURA a auditoria ao recarregar a sessão (antes ela só
            # existia no response vivo do /chat e sumia no reload).
            trace_persist = {k: result.get(k) for k in ["interaction_id","agent_id","final_state","evidence_score","transitions","duration_ms","trace","pipeline_steps","mode","verification"]}
            # Defaults pra campos que o frontend espera sempre presentes
            trace_persist.setdefault("interaction_id", iid)
            trace_persist.setdefault("agent_id", data.agent_id)
            if trace_persist.get("duration_ms") is None:
                trace_persist["duration_ms"] = 0
            if trace_persist.get("transitions") is None:
                trace_persist["transitions"] = []
            if trace_persist.get("evidence_score") is None:
                trace_persist["evidence_score"] = 0
            if not trace_persist.get("mode"):
                trace_persist["mode"] = "pipeline" if trace_persist.get("pipeline_steps") else "agent"
            try:
                await interactions_repo.update(iid, {"trace_data": json.dumps(trace_persist, ensure_ascii=False, default=str)})
            except Exception as e:
                # Persist falhou — não bloqueia a resposta ao user (já temos
                # `result`), mas vai pro errors.log pra troubleshooting.
                import logging as _logging
                _logging.getLogger("app.routes.workspace").error(
                    "workspace.chat.trace_persist_failed",
                    extra={
                        "event": "workspace.chat.trace_persist",
                        "interaction_id": iid,
                        "mode": trace_persist.get("mode"),
                        "error_type": type(e).__name__,
                    },
                    exc_info=True,
                )

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
    from app.core.database import agents_repo, skills_repo, tools_repo, knowledge_repo
    from app.mcp.runtime import parse_tool_bindings, match_with_registry
    from app.skill_parser.parser import parse_skill_md
    from app.workspace.binding_schema import (
        normalize_mcp_binding,
        normalize_declarative_skill_binding,
        normalize_rag_binding,
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

        # ── Onda A.2+A.3: SKILL declarativa (api_bindings + data_tables) ──
        # Pq não 1 por binding? Porque ambos os tipos compartilham ## Inputs
        # via Jinja2 — usuário preenche inputs uma vez e execute_declarative
        # orquestra todos. binding_kind reflete o conteúdo (api/tabular).
        decl_canonical = normalize_declarative_skill_binding(
            skill=sk, skill_md=raw_md, parsed_skill=parsed,
        )
        if decl_canonical:
            bindings_out.append(decl_canonical)

        # ── Onda A.3: RAG sources permitidas pela skill ──
        # SKILL declara via ## Evidence Policy → evidence_policy_parsed.sources
        # (lista de knowledge_source.id). Sem essa lista, nada é exposto
        # (defensivo — slash invoke direto bypassa governance do engine).
        # kb_mode=tabular sources não fazem sentido pra busca RAG textual.
        rag_source_ids = []
        if parsed:
            policy = getattr(parsed, "evidence_policy_parsed", None) or {}
            rag_source_ids = policy.get("sources") or []
        if rag_source_ids:
            try:
                # Busca em batch — repo não tem find_by_ids, fazemos N lookups
                for src_id in rag_source_ids:
                    src = await knowledge_repo.find_by_id(src_id)
                    if not src:
                        continue
                    # Filtra fontes desautorizadas e modo "tabular" (não suportam
                    # busca textual livre — slash invoke pra tabular RAG seria via
                    # Data Tables, não RAG).
                    if not src.get("authorized", 0):
                        continue
                    if src.get("kb_mode") == "tabular":
                        continue
                    bindings_out.append(normalize_rag_binding(src))
            except Exception as e:
                logger.warning(f"skills_context: lookup de RAG sources falhou: {e}")

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

    Persistência (2026-06-01): `session_id` e `message` opcionais. Quando
    `message` vem, a invocação é gravada como 1 turn na sessão (cria se
    não existir) — sem isso, mensagens viviam só no DOM do Alpine e
    sumiam ao recarregar a sessão pela sidebar.
    """
    agent_id: str
    skill_id: str
    binding_kind: str = _Field(..., pattern=r"^(mcp|api|rag|tabular)$")
    binding_id: str
    operation: str = ""
    params: dict = _Field(default_factory=dict)
    timeout: int = 60
    session_id: str = ""
    message: str = ""
    # Localização opt-in (2026-06-03): invoke direto é stateless/sem-LLM, então
    # o resultado cru de uma tool MCP (ex: busca Tavily) volta no idioma da
    # fonte (web em inglês). Quando True, roda UM passe de LLM pós-execução pra
    # traduzir os campos textuais pro idioma de resposta configurado
    # (agent.response_language > settings.default_response_language > pt-BR).
    # Default False preserva a natureza rápida/sem-LLM do caminho. Hoje só o
    # branch MCP honra esta flag (api/tabular/rag a ignoram).
    translate_result: bool = False


async def _localize_invoke_result(
    *,
    result_obj: object,
    target_lang: str,
    agent: dict,
) -> tuple[object, dict]:
    """Traduz campos textuais de um resultado de invoke direto pro idioma alvo.

    Caminho opt-in (``translate_result=True``). A invocação direta de uma tool
    MCP é sem-LLM por design — devolve o payload cru, que para buscas web
    (Tavily) costuma vir em inglês. Aqui rodamos UM passe de LLM reusando as
    MESMAS regras do chat (:func:`app.agents.engine._build_response_language_directive`):
    traduz títulos/conteúdo, preserva URLs, IDs/slugs, código e nomes próprios.

    Best-effort e à prova de falha: QUALQUER erro (provider sem config, timeout,
    JSON inválido na volta, payload grande demais) devolve o resultado ORIGINAL
    + ``meta.error``. NUNCA levanta — não pode derrubar uma invocação que já
    obteve o dado da tool.

    Returns:
        ``(localized_obj, meta)`` com ``meta = {ran, lang, latency_ms, error}``.
        ``ran=True`` só quando o LLM rodou e devolveu algo utilizável.
    """
    import time as _time
    import json as _json
    import re as _re
    from app.agents.engine import (
        _build_response_language_directive,
        _LANGUAGE_LABELS,
    )
    from app.core.llm_providers import get_provider

    meta: dict = {"ran": False, "lang": target_lang, "latency_ms": 0, "error": None}

    is_str = isinstance(result_obj, str)
    if is_str:
        payload_text = result_obj
        if not payload_text.strip():
            meta["error"] = "resultado vazio"
            return result_obj, meta
    else:
        try:
            payload_text = _json.dumps(result_obj, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            meta["error"] = f"serializacao falhou: {e}"
            return result_obj, meta

    # Guarda de tamanho: payload gigante explode custo/latência do passe extra.
    MAX_CHARS = 24000
    if len(payload_text) > MAX_CHARS:
        meta["error"] = f"payload {len(payload_text)} chars > teto {MAX_CHARS}"
        return result_obj, meta

    label = _LANGUAGE_LABELS.get(target_lang, target_lang)
    directive = _build_response_language_directive(target_lang)
    if is_str:
        sys_prompt = (
            directive
            + "\n\nVocê é um tradutor. Receberá um TEXTO e deve devolver o MESMO "
            f"texto traduzido/adaptado para {label}. Devolva APENAS o texto "
            "traduzido — sem comentários, sem aspas extras, sem cerca markdown."
        )
    else:
        sys_prompt = (
            directive
            + "\n\nVocê é um tradutor de JSON. Receberá um JSON e deve devolver um "
            f"JSON com EXATAMENTE a mesma estrutura (mesmas chaves, mesmos tipos), "
            f"com os VALORES textuais traduzidos para {label}. NÃO traduza nomes "
            "de chaves. NÃO altere números, booleanos nem null. Preserve URLs, "
            "IDs/slugs, código e nomes próprios. Devolva APENAS o JSON puro — sem "
            "cerca markdown, sem comentários."
        )

    t0 = _time.monotonic()
    try:
        provider = get_provider(
            (agent.get("llm_provider") or "openai"),
            model=agent.get("model"),
            temperature=0,
        )
        resp = await provider.generate([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": payload_text},
        ])
        meta["latency_ms"] = int((_time.monotonic() - t0) * 1000)
        content = ((resp or {}).get("content") or "").strip()
        if not content:
            meta["error"] = "LLM devolveu vazio"
            return result_obj, meta
    except Exception as e:  # best-effort — nunca derruba o invoke
        meta["latency_ms"] = int((_time.monotonic() - t0) * 1000)
        meta["error"] = str(e)[:200]
        logger.warning(
            "workspace.invoke_direct.localize_failed",
            extra={
                "event": "workspace.invoke_direct.localize",
                "agent_id": agent.get("id"),
                "target_lang": target_lang,
                "error": str(e)[:300],
            },
            exc_info=True,
        )
        return result_obj, meta

    if is_str:
        meta["ran"] = True
        return content, meta

    # JSON: o LLM às vezes embrulha em cerca markdown — descasca antes de parsear.
    cleaned = content
    if cleaned.startswith("```"):
        m = _re.search(r"```(?:json)?\s*(.*?)```", cleaned, _re.DOTALL)
        if m:
            cleaned = m.group(1).strip()
    try:
        localized = _json.loads(cleaned)
    except (ValueError, TypeError) as e:
        # Tradução veio malformada — preserva o original pra não quebrar o
        # render estruturado da UI.
        meta["error"] = f"JSON da traducao invalido: {e}"
        return result_obj, meta
    meta["ran"] = True
    return localized, meta


def _build_invoke_trace(
    *,
    agent: dict,
    skill: dict | None,
    final_state: str,
    duration_ms: int,
    execution_log: list[dict],
    diagnostics: list[dict] | None = None,
    mcp_tools: list[dict] | None = None,
    api_tools: list[dict] | None = None,
    api_tools_count: int = 0,
    evidence_count: int = 0,
    evidence_sources: list | None = None,
    interaction_id: str | None = None,
) -> dict:
    """Monta o trace canônico de uma invocação direta (slash invoke, sem LLM).

    Bug (2026-06-02): o caminho /invoke-binding-direct NÃO produzia trace
    algum. Resultado: o painel "Execution Log" mostrava "0 entrada(s)" e a
    "Rastreabilidade" ficava totalmente vazia (lastTrace=null + sessão
    aberta → nenhum dos x-if do template renderiza). User pediu que os dois
    painéis SEMPRE apareçam.

    A forma aqui espelha exatamente o trace persistido pelo /chat declarativo
    (workspace.py:584-617) para que o frontend (Rastreabilidade + Execution
    Log + Métricas + drilldowns MCP/API) renderize idêntico — tanto na
    invocação ao vivo (response body) quanto no reload da sessão (trace_data).

    Campos consumidos pelo frontend (workspace.html):
    - lastTrace.final_state / .mode / .duration_ms / .transitions /
      .pipeline_steps / .evidence_score / .interaction_id
    - lastTrace.trace.{total_steps, evidence_count, evidence_sources,
      diagnostics, mcp_tools[{name,status,server,latency_ms}],
      api_tools[{binding_id,status_code,latency_ms,...}], api_tools_count,
      execution_log[{cat,icon,title,detail,level}]}
    """
    return {
        "interaction_id": interaction_id,
        "agent_id": agent.get("id"),
        "final_state": final_state,
        "evidence_score": 0.0,
        "transitions": [],
        "pipeline_steps": [],
        "duration_ms": duration_ms,
        "status": "completed",
        "mode": "agent",
        "trace": {
            "total_steps": len(execution_log),
            "evidence_count": evidence_count,
            "evidence_sources": evidence_sources or [],
            "diagnostics": diagnostics or [],
            "agent_name": agent.get("name", ""),
            "agent_kind": agent.get("kind", ""),
            "agent_model": "(invocação direta)",
            "agent_provider": "direct",
            "agent_version": agent.get("version", "1.0.0"),
            "agent_domain": agent.get("domain", ""),
            "skill_detail": {
                "name": (skill.get("name") if skill else "") or "",
                "version": (skill.get("version") if skill else "") or "",
                "execution_mode": "direct",
            },
            "mcp_tools": mcp_tools or [],
            "api_tools": api_tools or [],
            "api_tools_count": api_tools_count,
            "api_bindings_executed": api_tools or [],
            "tokens": {"input": 0, "output": 0, "total": 0, "calls": 0, "input_billed_sum": 0, "total_billed": 0},
            "execution_log": execution_log,
        },
    }


async def _persist_invoke_turn(
    *,
    session_id: str,
    message: str,
    output_text: str,
    agent_id: str,
    title_fallback: str,
    trace_data: dict | None = None,
) -> str | None:
    """Grava 1 turn (user + assistant) na sessão e devolve o interaction_id.

    Comportamento (alinhado com /chat declarativo, workspace.py:526-562):
    - `session_id` vazio → cria nova sessão (UUID novo, title = primeiros 80
      chars de `message` ou `title_fallback`).
    - Sessão já existe → calcula next_turn baseado no maior turn_number atual.
    - Falha silenciosamente (loga e devolve None) — não derrubar a invocação
      do tool por erro de persistência.

    `message` é o texto humano que o frontend já mostra na bolha do user
    (ex.: "🛠️ Tavily MCP Server (search) · query=agentes de IA autonomos").
    `output_text` é o conteúdo do assistant na mesma forma que o frontend
    renderiza (string crua OU fenced JSON) — assim o round-trip pelo
    /sessions/{id} GET reproduz exatamente o que estava no DOM.

    `trace_data` (2026-06-02): quando fornecido, é gravado na coluna
    `trace_data` da interaction (mesmo shape do /chat) para que ao recarregar
    a sessão pela sidebar o Execution Log + Rastreabilidade reapareçam. Antes
    desta extensão o slash invoke gravava só os turns e o trace voltava como
    placeholder vazio ({}), com "0 entrada(s)".
    """
    if not message:
        # Sem texto de usuário não dá pra reconstruir a bolha "EU" no
        # round-trip. Não persiste — comportamento legado (efêmero).
        return None
    try:
        sid = (session_id or "").strip() or str(uuid.uuid4())
        existing = await interactions_repo.find_by_id(sid) if session_id else None
        if not existing:
            await interactions_repo.create({
                "id": sid,
                "title": (message or title_fallback)[:80].strip(),
                "agent_id": agent_id,
                "channel": "workspace",
                "journey_id": "",
                "state": "LogAndClose",
                "ended_at": naive_utc_now(),
            })
            next_turn = 1
        else:
            old_turns = await turns_repo.find_all(interaction_id=sid, limit=500)
            next_turn = max((int(t.get("turn_number") or 0) for t in old_turns), default=0) + 1
            await interactions_repo.update(sid, {
                "state": "LogAndClose",
                "ended_at": naive_utc_now(),
            })
        await turns_repo.create({
            "id": str(uuid.uuid4()),
            "turn_number": next_turn,
            "user_text_redacted": message,
            "interaction_id": sid,
        })
        await turns_repo.create({
            "id": str(uuid.uuid4()),
            "turn_number": next_turn + 1,
            "output_text_redacted": output_text,
            "interaction_id": sid,
        })
        # Persistir trace_data quando fornecido (2026-06-02). Round-trip do
        # /sessions/{id} GET reconstrói Rastreabilidade + Execution Log a
        # partir daqui. Carimba o interaction_id real no trace.
        if trace_data is not None:
            td = dict(trace_data)
            td["interaction_id"] = sid
            await interactions_repo.update(
                sid, {"trace_data": json.dumps(td, ensure_ascii=False, default=str)}
            )
        return sid
    except Exception as e:
        logger.warning(
            "workspace.invoke_direct.persist_failed",
            extra={
                "event": "workspace.invoke_direct",
                "agent_id": agent_id,
                "error_type": type(e).__name__,
                "error": str(e)[:200],
            },
        )
        return None


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

    if data.binding_kind not in ("mcp", "api", "tabular", "rag"):
        raise HTTPException(
            501,
            f"binding_kind '{data.binding_kind}' não suportado. "
            "Suportados: mcp (A.1), api (A.2), tabular+rag (A.3).",
        )

    # 1. Resolve agent + skill (comum a todos os paths)
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
    # Branch: binding_kind in ("api", "tabular") — Onda A.2 + A.3
    # ──────────────────────────────────────────────────────
    # Ambos rodam via execute_declarative (mesma orquestração com retry,
    # output_mapping, etc.). Diferença = só o schema de fields (api vem
    # de api_bindings, tabular de data_tables).
    if data.binding_kind in ("api", "tabular"):
        return await _invoke_api_binding_direct(
            data=data, agent=agent, skill=sk, parsed=parsed, raw_md=raw_md,
        )

    # ──────────────────────────────────────────────────────
    # Branch: binding_kind == "rag" — Onda A.3
    # ──────────────────────────────────────────────────────
    if data.binding_kind == "rag":
        return await _invoke_rag_binding_direct(
            data=data, agent=agent, skill=sk, parsed=parsed,
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

    # 7b. Localização opt-in (2026-06-03): invoke direto é sem-LLM, então o
    # resultado cru de uma tool MCP (busca Tavily etc) volta no idioma da fonte.
    # Quando data.translate_result=True E a tool não falhou, roda UM passe de
    # LLM pra traduzir os campos textuais pro idioma de resposta resolvido
    # (agent.response_language > settings.default_response_language > pt-BR).
    # Best-effort: erro na tradução devolve o resultado original (não derruba).
    localize_meta: dict | None = None
    if data.translate_result and not is_error:
        from app.agents.engine import _resolve_response_language
        from app.core.config import get_settings as _get_settings_loc
        target_lang = _resolve_response_language(agent, _get_settings_loc())
        result_obj, localize_meta = await _localize_invoke_result(
            result_obj=result_obj, target_lang=target_lang, agent=agent,
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
            "translate_requested": bool(data.translate_result),
            "localized": bool(localize_meta and localize_meta.get("ran")),
            "localize_lang": (localize_meta or {}).get("lang"),
            "localize_latency_ms": (localize_meta or {}).get("latency_ms"),
            "localize_error": (localize_meta or {}).get("error"),
        },
    )

    # 8. Persistência (2026-06-01): grava o turn na sessão para que ao
    # recarregar (sidebar de Sessões) a invocação reapareça com os mesmos
    # cards bonitos. Antes disso, a interação vivia só no DOM Alpine e
    # sumia em F5 / troca de sessão. Falha não derruba a invocação.
    if isinstance(result_obj, str):
        output_text = result_obj
    else:
        output_text = "```json\n" + _json.dumps(result_obj, ensure_ascii=False, indent=2) + "\n```"

    # 8b. Trace canônico (2026-06-02): sem isso, Execution Log + Rastreabilidade
    # ficavam vazios para invocações diretas. Espelha o /chat declarativo.
    op_label = arguments.get("operation") or ""
    exec_log = [
        {
            "cat": "tools", "icon": "🛠️",
            "title": tool_name + (f" ({op_label})" if op_label else ""),
            "detail": "params: " + (", ".join(sorted((data.params or {}).keys())) or "—"),
            "level": "info",
        },
        {
            "cat": "result", "icon": "✓" if not is_error else "✗",
            "title": "Resultado da ferramenta",
            "detail": f"{latency_ms}ms · {'ok' if not is_error else 'erro'}",
            "level": "success" if not is_error else "danger",
        },
    ]
    # Entrada extra no log quando a localização opt-in foi solicitada — mostra
    # se a tradução rodou (e a latência do passe de LLM) ou por que não aplicou.
    if localize_meta is not None:
        from app.agents.engine import _LANGUAGE_LABELS as _LBL
        _lang = localize_meta.get("lang") or ""
        if localize_meta.get("ran"):
            exec_log.append({
                "cat": "localize", "icon": "🌐",
                "title": f"Tradução → {_LBL.get(_lang, _lang)}",
                "detail": f"{localize_meta.get('latency_ms', 0)}ms · LLM",
                "level": "info",
            })
        else:
            exec_log.append({
                "cat": "localize", "icon": "🌐",
                "title": "Tradução não aplicada",
                "detail": (localize_meta.get("error") or "—")[:120],
                "level": "warning",
            })
    mcp_tools_trace = [{
        "name": tool_name,
        "status": "error" if is_error else "completed",
        "server": tool.get("server_label") or tool.get("server") or tool.get("server_name") or "",
        "latency_ms": latency_ms,
    }]
    diag = [{
        "level": "danger" if is_error else "success",
        "text": (
            f"Invocação direta de '{tool_name}'"
            + (f" (operação {op_label})" if op_label else "")
            + (" retornou erro." if is_error else " concluída.")
        ),
    }]
    if localize_meta is not None:
        if localize_meta.get("ran"):
            diag.append({
                "level": "info",
                "text": f"Resultado traduzido para {localize_meta.get('lang')} via LLM (pós-execução, opt-in).",
            })
        else:
            diag.append({
                "level": "warning",
                "text": f"Tradução opt-in não aplicada: {localize_meta.get('error') or 'motivo desconhecido'}. Resultado exibido no idioma original.",
            })
    trace_obj = _build_invoke_trace(
        agent=agent, skill=sk,
        final_state="Failed" if is_error else "LogAndClose",
        duration_ms=latency_ms, execution_log=exec_log,
        diagnostics=diag, mcp_tools=mcp_tools_trace,
    )
    interaction_id = await _persist_invoke_turn(
        session_id=data.session_id,
        message=data.message,
        output_text=output_text,
        agent_id=data.agent_id,
        title_fallback=f"Invocação · {tool_name}",
        trace_data=trace_obj,
    )
    trace_obj["interaction_id"] = interaction_id

    return {
        "ok": not is_error,
        "result": result_obj,
        "result_raw": result_raw if isinstance(result_raw, str) else None,
        "schema": schema,
        "payload_sent": arguments,
        "latency_ms": latency_ms,
        "tool_name": tool_name,
        "interaction_id": interaction_id,
        "localized": bool(localize_meta and localize_meta.get("ran")),
        "trace": trace_obj,
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
        normalize_declarative_skill_binding,
        validate_params_against_schema,
    )

    # 1. Confirma que skill_id casa com binding_id (single-skill aware)
    if str(skill.get("id") or "") != data.binding_id:
        raise HTTPException(
            404,
            f"Binding {data.binding_kind} '{data.binding_id}' não corresponde "
            f"à skill '{data.skill_id}'. Em {data.binding_kind}, binding_id "
            "deve ser o skill_id.",
        )

    # 2. Gera schema canônico (revalida declarativa + tem api ou data_tables)
    schema = normalize_declarative_skill_binding(skill, skill_md=raw_md, parsed_skill=parsed)
    if not schema:
        raise HTTPException(
            422,
            f"Skill '{data.skill_id}' não é declarativa OU não tem "
            "## API Bindings nem ## Data Tables parseáveis. Apenas "
            "declarativas suportam invoke-binding-direct kind='api|tabular'.",
        )
    # Se user disse "api" mas SKILL é só tabular (ou vice-versa), reflete
    # o que existe — não erra. UX permissiva.
    if data.binding_kind not in ("api", "tabular"):
        raise HTTPException(400, "binding_kind precisa ser 'api' ou 'tabular'.")

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
    #
    # A sessão é dona do ROUTE, não do engine. Derivamos o `sid` ANTES de
    # execute_declarative e passamos register_interaction=False para que o
    # engine NÃO crie uma 2ª interaction "<agent> (declarativo)" órfã — que
    # aparecia na sidebar como sessão VAZIA "Busca endereço" (bug 2026-06-02).
    # O MESMO `sid` é repassado ao _persist_invoke_turn, então a interaction
    # real (título = mensagem do user) e os logs de auditoria (api_call_logs /
    # binding_executions, que usam o sid como TEXT sem FK) ficam ligados.
    sid = (data.session_id or "").strip() or str(uuid.uuid4())
    t0 = time.monotonic()
    try:
        from app.agents.declarative_engine import execute_declarative
        decl = await execute_declarative(
            agent=agent,
            skill_parsed=parsed,
            inputs=inputs,
            context=None,
            session_id=sid,
            dry_run=False,
            register_interaction=False,  # route é dono da sessão
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
    # Alinha com a semântica de final_state do engine declarativo:
    #   completed → tudo 2xx, sem erro             → sucesso (verde ✓)
    #   partial   → ≥1 binding 2xx, mas houve erro → sucesso PARCIAL (verde + aviso)
    #   dry_run   → plano resolvido sem rede        → sucesso
    #   failed    → nenhum 2xx (ou erro fatal)      → falha (vermelho 🔺)
    # Antes computávamos `is_ok = any_success and not errors_out`, o que pintava
    # de "Failed" vermelho uma resposta HTTP 200 com mero aviso de mapping
    # (final_state="partial") apesar dos dados válidos renderizados na bolha
    # (bug 2026-06-02). Agora só `failed` pinta vermelho.
    decl_state = (decl.get("final_state") or "").lower()
    if decl_state:
        is_ok = decl_state in ("completed", "partial", "dry_run")
    else:
        # Fallback p/ resultados legados/mocks sem final_state explícito.
        is_ok = bool(any_success and not errors_out)
    is_partial = is_ok and bool(errors_out)

    # 7. Log estruturado (auditoria)
    logger.info(
        "workspace.invoke_direct.declarative_completed",
        extra={
            "event": "workspace.invoke_direct",
            "agent_id": data.agent_id,
            "skill_id": data.skill_id,
            "binding_kind": data.binding_kind,  # api OU tabular
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

    # 8. Persistência (2026-06-01, Bug 2): paridade com o caminho MCP — sem
    # isso, invocações de API/Tabular binding via slash não eram gravadas
    # como interaction/turn, e a sessão aparecia vazia ao recarregar pela
    # sidebar. A PR #243 instrumentou só o ramo MCP; aqui completamos a
    # paridade. Fenceia com base no `result_obj` (que ainda preserva o tipo
    # original) e não no `output_text` (já serializado como JSON puro).
    # Sem o fence, `isStructuredContent` no round-trip não detecta JSON
    # e mostra paredão em vez dos cards.
    if isinstance(result_obj, str):
        persist_text = result_obj
    else:
        persist_text = "```json\n" + _json.dumps(result_obj, ensure_ascii=False, indent=2) + "\n```"

    # 8b. Trace canônico (2026-06-02): paridade com MCP/RAG. Constrói o
    # execution_log a partir dos bindings executados (mesma lógica do /chat
    # declarativo, workspace.py:572-582) para que Rastreabilidade + Execution
    # Log + drilldown "API tools" apareçam ao vivo e no reload da sessão.
    exec_log: list[dict] = []
    api_tools_trace: list[dict] = []
    for b in executed:
        st = b.get("status", 0)
        lvl = "success" if 200 <= st < 300 else "danger"
        # O dict de resultado do engine NÃO tem method/path/connector — só
        # binding_id, status, latency_ms, attempts (ver _execute_planned_binding
        # / _execute_data_tables_phase). Usar method/path produzia "?? ??" no
        # log (bug 2026-06-02). binding_id já identifica (ex.: "ep-cep",
        # "table:vendas"); o prefixo "table:" distingue tabular de HTTP.
        bid = str(b.get("binding_id") or "binding")
        is_table = bid.startswith("table:") or b.get("kind") == "table"
        lat = b.get("latency_ms") or b.get("duration_ms") or 0
        attempts = b.get("attempts", 1)
        err = b.get("error")
        exec_log.append({
            "cat": "tabular" if is_table else "api",
            "icon": "📊" if is_table else "🌐",
            "title": bid,
            "detail": (
                f"status={st} · {lat}ms"
                + (f" · {attempts} tentativa(s)" if attempts and attempts != 1 else "")
                + (f" · {str(err)[:80]}" if err else "")
            ),
            "level": lvl,
        })
        api_tools_trace.append({
            "binding_id": bid,
            "status_code": st,
            "latency_ms": lat,
            "attempts": attempts,
            "error": err or "",
            "is_compensation": bool(b.get("is_compensation")),
            "skipped_by_breaker": bool(b.get("skipped_by_breaker")),
        })
    if not exec_log:
        # SKILL declarativa que não chamou nenhum binding HTTP (ex.: tabular
        # puro / template-only). Registra ao menos 1 entrada pra UI não ficar
        # vazia — o user pediu que o painel SEMPRE mostre algo.
        exec_log.append({
            "cat": "skill", "icon": "⚙️",
            "title": f"SKILL declarativa · {skill.get('name') or data.binding_kind}",
            "detail": f"{latency_ms}ms · {len(executed)} binding(s) executado(s)",
            "level": "success" if is_ok else "warning",
        })
    # Diagnóstico: sucesso pleno (verde), parcial (verde/amber com aviso) ou
    # falha (vermelho). `partial` é sucesso COM ressalva, não erro.
    diag_level = "success" if (is_ok and not is_partial) else ("warning" if is_ok else "danger")
    diag = [{
        "level": diag_level,
        "text": (
            f"Modo declarativo (invoke direto): {len(executed)} binding(s) executado(s)"
            + (f" · {len(errors_out)} aviso(s)/erro(s)" if errors_out else "")
            + (" · resultado parcial" if is_partial else "")
        ),
    }]
    trace_obj = _build_invoke_trace(
        agent=agent, skill=skill,
        # Só `failed` pinta vermelho na Rastreabilidade; completed/partial/dry_run
        # → LogAndClose (verde ✓). O aviso do parcial fica no diagnóstico acima.
        final_state="LogAndClose" if is_ok else "Failed",
        duration_ms=latency_ms, execution_log=exec_log,
        diagnostics=diag, api_tools=api_tools_trace,
        api_tools_count=len(executed),
    )
    interaction_id = await _persist_invoke_turn(
        session_id=sid,  # mesmo sid passado ao engine — uma única sessão
        message=data.message,
        output_text=persist_text,
        agent_id=data.agent_id,
        title_fallback=f"Invocação · {skill.get('name') or data.binding_kind}",
        trace_data=trace_obj,
    )
    trace_obj["interaction_id"] = interaction_id

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
        "interaction_id": interaction_id,
        "trace": trace_obj,
    }


# ═══════════════════════════════════════════════════════════════
# Onda A.3 — Helper de invocação RAG
# ═══════════════════════════════════════════════════════════════


async def _invoke_rag_binding_direct(
    *,
    data: "InvokeBindingDirectRequest",
    agent: dict,
    skill: dict,
    parsed,
):
    """Invoca busca RAG (Retriever.search) numa knowledge_source específica.

    binding_id = knowledge_source.id. binding_kind="rag". User envia
    {query, top_n}. Backend:
    1. Lookup do source no knowledge_repo
    2. Gate de governance: source DEVE estar em skill.evidence_policy_parsed.sources
       (slash invoke direto bypassa a camada de evidence policy do engine,
       então re-implementamos o gate aqui)
    3. Valida params (query required)
    4. Chama retriever.search(query, top_n, allowed_source_ids=[binding_id])
    5. Retorna chunks formatados pra UI renderizar
    """
    import time
    from app.core.database import knowledge_repo
    from app.workspace.binding_schema import (
        normalize_rag_binding,
        validate_params_against_schema,
    )

    # 1. Lookup source
    source = await knowledge_repo.find_by_id(data.binding_id)
    if not source:
        raise HTTPException(
            404,
            f"Knowledge source '{data.binding_id}' não encontrada no Registry.",
        )

    # 2. Gate de governance: skill precisa autorizar esta source
    policy = (getattr(parsed, "evidence_policy_parsed", None) or {})
    allowed_sources = policy.get("sources") or []
    if not allowed_sources:
        raise HTTPException(
            403,
            f"Skill '{data.skill_id}' não declara nenhuma source em "
            "## Evidence Policy. Slash invoke RAG requer policy explícita.",
        )
    if data.binding_id not in allowed_sources:
        raise HTTPException(
            403,
            f"Source '{data.binding_id}' não está autorizada em "
            f"## Evidence Policy da skill '{data.skill_id}'. "
            f"Autorizadas: {allowed_sources}.",
        )
    if not source.get("authorized", 0):
        raise HTTPException(
            403,
            f"Source '{data.binding_id}' está marcada como NÃO autorizada "
            "no Registry. Habilite em /knowledge.",
        )

    # 3. Gera schema canônico e valida params
    schema = normalize_rag_binding(source)
    ok, errors = validate_params_against_schema(schema, data.params or {})
    if not ok:
        raise HTTPException(422, {"errors": errors, "schema": schema})

    query = str(data.params.get("query") or "").strip()
    try:
        top_n = int(data.params.get("top_n", 5))
    except (ValueError, TypeError):
        top_n = 5
    # Clamp defensivo (Retriever aceita o que vier, mas evitamos 1000 chunks)
    if top_n < 1:
        top_n = 1
    if top_n > 50:
        top_n = 50

    # 4. Executa busca
    t0 = time.monotonic()
    try:
        from app.evidence.runtime import retriever as _retriever
        results = await _retriever.search(
            query=query,
            skill_evidence_policy=policy if policy else None,
            top_n=top_n,
            allowed_source_ids=[data.binding_id],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "workspace.invoke_direct.rag_error",
            extra={
                "event": "workspace.invoke_direct",
                "agent_id": data.agent_id,
                "skill_id": data.skill_id,
                "binding_kind": "rag",
                "binding_id": data.binding_id,
                "latency_ms": latency_ms,
                "error": str(e)[:300],
            },
        )
        raise HTTPException(500, f"Erro ao buscar RAG: {str(e)[:300]}")

    # 5. Format result
    chunks = []
    for r in results or []:
        chunks.append({
            "evidence_id": getattr(r, "evidence_id", "") or "",
            "snippet": getattr(r, "snippet_text", "") or "",
            "score": float(getattr(r, "relevance_score", 0.0) or 0.0),
            "source_name": getattr(r, "source_name", "") or "",
            "source_id": getattr(r, "source_id", "") or "",
            "confidentiality": getattr(r, "confidentiality", "internal") or "internal",
        })

    result_obj = {
        "chunks": chunks,
        "total": len(chunks),
        "query": query,
        "source": source.get("name") or "",
    }

    # 6. Log estruturado
    logger.info(
        "workspace.invoke_direct.rag_completed",
        extra={
            "event": "workspace.invoke_direct",
            "agent_id": data.agent_id,
            "skill_id": data.skill_id,
            "binding_kind": "rag",
            "binding_id": data.binding_id,
            "source_name": source.get("name") or "",
            "schema_source": schema.get("schema_source"),
            "latency_ms": latency_ms,
            "ok": True,
            "chunk_count": len(chunks),
            "top_n": top_n,
        },
    )

    # 7. Persistência (2026-06-01, Bug 2): paridade com MCP/API — sem isso,
    # invocações RAG via slash não eram gravadas e sumiam no reload da
    # sessão. result_obj é um dict (chunks + total + query + source), então
    # vai como fenced JSON pro round-trip fiel.
    import json as _json
    persist_text = "```json\n" + _json.dumps(result_obj, ensure_ascii=False, indent=2) + "\n```"

    # 7b. Trace canônico (2026-06-02): paridade com MCP/API. Chunks viram
    # evidências (evidence_count + evidence_sources) e o painel de Evidências
    # da Rastreabilidade passa a renderizar. execution_log com a busca + total.
    src_name = source.get("name") or data.binding_id
    exec_log = [
        {
            "cat": "evidence", "icon": "🔍",
            "title": f"Busca RAG · {src_name}",
            "detail": f"query='{query[:60]}' · top_n={top_n}",
            "level": "info",
        },
        {
            "cat": "result", "icon": "✓" if chunks else "⚠",
            "title": f"{len(chunks)} chunk(s) recuperado(s)",
            "detail": f"{latency_ms}ms",
            "level": "success" if chunks else "warning",
        },
    ]
    evidence_sources = sorted({c["source_name"] for c in chunks if c.get("source_name")})
    diag = [{
        "level": "success" if chunks else "warning",
        "text": f"Busca RAG retornou {len(chunks)} chunk(s) de '{src_name}'.",
    }]
    trace_obj = _build_invoke_trace(
        agent=agent, skill=skill,
        final_state="LogAndClose",
        duration_ms=latency_ms, execution_log=exec_log,
        diagnostics=diag, evidence_count=len(chunks),
        evidence_sources=evidence_sources,
    )
    interaction_id = await _persist_invoke_turn(
        session_id=data.session_id,
        message=data.message,
        output_text=persist_text,
        agent_id=data.agent_id,
        title_fallback=f"Busca RAG · {src_name}",
        trace_data=trace_obj,
    )
    trace_obj["interaction_id"] = interaction_id

    return {
        "ok": True,
        "result": result_obj,
        "result_raw": None,
        "schema": schema,
        "payload_sent": {
            "query": query,
            "top_n": top_n,
            "allowed_source_ids": [data.binding_id],
        },
        "latency_ms": latency_ms,
        "tool_name": source.get("name") or "",
        "interaction_id": interaction_id,
        "trace": trace_obj,
    }
