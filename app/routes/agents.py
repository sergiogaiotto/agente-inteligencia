"""Rotas de agentes — AOBD, Router, Subagent."""
import logging
import os
import re
import time
import uuid, json
from datetime import datetime, timezone
from urllib.parse import quote

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.auth import require_user
from app.core.text_sanitize import strip_emoji
from app.models.schemas import (
    AgentCreate, AgentUpdate, AgentInvokeRequest, AgentInvokeResponse,
    PreflightReport,
)
from app.core.database import (
    agents_repo, audit_repo, skills_repo,
    interactions_repo, turns_repo, tool_calls_repo, api_call_logs_repo,
    binding_executions_repo,
)

# Decoder movido p/ app/core/attachments (37.0.0): o invoke de PIPELINE
# ganhou o ramo base64 e compartilha o decoder. O alias da FUNÇÃO é seam de
# teste (tests importam app.routes.agents._decode_attachments); os limites
# vivem SÓ no módulo novo — monkeypatch neles lá (alias de constante seria
# seam falso: o decoder lê o global do módulo dele, não daqui).
from app.core.attachments import decode_attachments as _decode_attachments

# Health Score: abaixo deste nº de interações em 30d, success_rate (60% do
# peso) é ruído estatístico — 1 interação não-Recommend derruba o score de
# 100→40 num penhasco. O diagnostics expõe `reliable=False` para o frontend
# tratar como "provisório" (cor neutra + aviso) em vez de pintar verde/
# vermelho com falsa confiança.
MIN_RELIABLE_HEALTH_SAMPLE = 20


async def _enforce_apikey_budget(request: Request) -> None:
    """F6: pré-checa o teto de custo da API Key (402 quando a janela estourou).

    Fecha o gap: o débito/quota F6 estava wired SÓ no invoke de PIPELINE
    (/pipelines/{id}/invoke). O invoke de AGENTE (/agents/{id}/invoke) — que a
    contenção P0 PERMITE a uma key — não tinha quota: com o multimodal roteando
    pro azure/gpt-4o (custo real), uma key gastava sem teto por aqui. No-op p/
    cookie/UI, key sem orçamento, ou toggle OFF."""
    api_key_id = getattr(request.state, "api_key_id", None)
    if not api_key_id:
        return
    from app.core.api_key_budget import enforce_budget
    await enforce_budget(api_key_id)


async def _debit_and_attribute_apikey(request: Request, result: dict) -> None:
    """F6+F12: debita o custo REAL da invocação no ledger da key E atribui a
    interação à key (metadata.via). Best-effort e gated pelo toggle — NUNCA
    derruba a resposta (o invoke já executou). No-op p/ cookie/UI."""
    api_key_id = getattr(request.state, "api_key_id", None)
    if not api_key_id:
        return
    interaction_id = (result or {}).get("interaction_id")
    # F6 — débito (gated pelo toggle dentro de record_cost).
    try:
        from app.core.api_key_budget import cost_and_tokens_from_result, record_cost
        cost, tokens = cost_and_tokens_from_result(result)
        await record_cost(api_key_id, cost, tokens, interaction_id=interaction_id)
    except Exception as e:  # noqa: BLE001 — débito nunca derruba o invoke
        logger.warning("event=apikey_cost_record_failed error=%s", e)
    # F12 — atribuição por-key na metadata da interação (observabilidade).
    if not interaction_id:
        return
    try:
        row = await interactions_repo.find_by_id(interaction_id) or {}
        md = row.get("metadata")
        if isinstance(md, str):
            md = json.loads(md) if md.strip() else {}
        if not isinstance(md, dict):
            md = {}
        md.update({
            "via": "api_key",
            "api_key_id": api_key_id,
            "api_key_name": getattr(request.state, "api_key_name", None),
        })
        await interactions_repo.update(interaction_id, {"metadata": json.dumps(md, ensure_ascii=False)})
    except Exception as e:  # noqa: BLE001 — atribuição nunca derruba o invoke
        logger.warning("event=apikey_attribution_failed interaction_id=%s error=%s", interaction_id, e)


def _schedule_agent_invoke_cost(request: Request, agent_id: str, result: dict) -> None:
    """SSOT de custo (33.7.0) do invoke DIRETO de agente (source='agent_invoke'),
    OFF-PATH e org-wide: grava 1 linha em `invocation_costs` cobrindo cookie/UI E
    X-API-Key (o débito por-key só via a parte da key). Best-effort; o invoke não
    espera por isto (invariante de desempenho)."""
    from app.core.analytics_tasks import schedule_analytics

    async def _rec():
        from app.core.cost_ledger import record_invocation_cost
        from app.core.api_key_budget import cost_and_tokens_from_result
        from app.core.auth import read_session_uid
        r = result or {}
        cost, tokens = cost_and_tokens_from_result(r)
        api_key_id = getattr(request.state, "api_key_id", None)
        await record_invocation_cost(
            interaction_id=r.get("interaction_id"),
            agent_id=agent_id,
            user_id=(None if api_key_id else read_session_uid(request)),
            api_key_id=api_key_id,
            channel=("api_key" if api_key_id else "session"),
            source="agent_invoke",
            cost_usd=cost, tokens_used=tokens,
            latency_ms=(r.get("duration_ms") or 0),
            final_state=r.get("final_state"),
        )

    schedule_analytics(_rec())


router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

@router.get("")
async def list_agents(limit: int = 50, offset: int = 0, kind: str = None, status: str = None, domain: str = None):
    f = {}
    if kind: f["kind"] = kind
    if status: f["status"] = status
    if domain: f["domain"] = domain
    agents = await agents_repo.find_all(limit=limit, offset=offset, **f)
    return {"agents": agents, "total": await agents_repo.count(**f)}

async def _is_callable_externally(agent: dict) -> tuple[bool, str]:
    """Regra de exposição do /invoke pro cliente externo:
    - orquestrador (aobd/router) COM pipeline configurada (tem outgoing) → ok
    - subagent standalone (sem nenhuma conexão de mesh) → ok
    - resto → bloqueia, força invocar via orquestrador.
    """
    from app.core.database import mesh_repo
    aid = agent["id"]
    kind = (agent.get("kind") or "subagent").lower()
    outgoing = await mesh_repo.find_all(source_agent_id=aid, limit=1)
    incoming = await mesh_repo.find_all(target_agent_id=aid, limit=1)
    has_out = bool(outgoing)
    has_in = bool(incoming)
    if kind in ("aobd", "router"):
        if has_out:
            return True, "orquestrador com pipeline"
        return False, "orquestrador sem pipeline — configure conexões no AI Mesh antes de invocar"
    # subagent (ou kind desconhecido por default)
    if has_in or has_out:
        return False, "subagent é parte de um pipeline — invoke o orquestrador de entrada"
    return True, "subagent standalone"


@router.get("/callable")
async def list_callable_agents(limit: int = 100):
    """Lista só agentes invocáveis externamente via /invoke (orquestradores com
    pipeline + subagents standalone). Pensado para clientes externos descobrirem
    quem é seguro invocar sem precisar entender a topologia do mesh."""
    rows = await agents_repo.find_all(limit=max(1, min(500, limit)))
    out = []
    for a in rows:
        if (a.get("status") or "active") != "active":
            continue
        ok, reason = await _is_callable_externally(a)
        if ok:
            out.append({
                "id": a["id"],
                "name": a.get("name"),
                "kind": a.get("kind"),
                "domain": a.get("domain"),
                "reason": reason,
            })
    return {"agents": out, "total": len(out)}


@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    a = await agents_repo.find_by_id(agent_id)
    if not a: raise HTTPException(404, "Agente não encontrado")
    return a

_BOOL_FIELDS = ("require_evidence", "accepts_images", "accepts_documents", "allow_general_knowledge")


@router.post("/preflight", response_model=PreflightReport)
async def preflight_agent(data: AgentCreate) -> PreflightReport:
    """Roda 10 checks semânticos contra o payload (sem persistir).
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
    # Snapshot inicial do system_prompt (46.0.0, PR1 do arco Otimização).
    if (d.get("system_prompt") or "").strip():
        from app.core import revisions as _rev
        await _rev.safe_record(
            entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=aid,
            content=d["system_prompt"], version=d.get("version"),
            source="create",
        )
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
    # reasoning_effort é nullable e "limpável": se o cliente ENVIOU a chave (mesmo
    # como null, ex. wizard "Padrão do modelo"), respeita o null pra LIMPAR o valor
    # salvo. Sem isto, o filtro `if v is not None` acima dropava o null e o valor
    # antigo persistia (footgun de null-drop em PUT — ver feedback de settings).
    if "reasoning_effort" in data.model_fields_set:
        upd["reasoning_effort"] = data.reasoning_effort

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
    result = await agents_repo.update(agent_id, upd)
    # Histórico do system_prompt (46.0.0, PR1): backfill do antigo na 1ª
    # edição pós-feature + snapshot do novo. DEPOIS do update (self-review:
    # gravar antes deixaria revisão fantasma se o update falhasse) e
    # best-effort — nunca quebra o PUT.
    if "system_prompt" in upd and \
            (upd["system_prompt"] or "") != (existing.get("system_prompt") or ""):
        from app.core import revisions as _rev
        await _rev.safe_backfill(
            entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=agent_id,
            old_content=existing.get("system_prompt") or "",
            version=existing.get("version"),
        )
        await _rev.safe_record(
            entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=agent_id,
            content=upd["system_prompt"] or "",
            version=upd.get("version") or existing.get("version"),
            source="update",
        )
    return result


@router.get("/{agent_id}/prompt-revisions")
async def list_agent_prompt_revisions(agent_id: str):
    """Histórico do system_prompt (46.0.0) — sem o conteúdo (leve p/ a UI)."""
    if not await agents_repo.find_by_id(agent_id):
        raise HTTPException(404)
    from app.core import revisions as _rev
    return {"revisions": await _rev.list_revisions(
        _rev.ENTITY_AGENT_PROMPT, agent_id)}


@router.get("/{agent_id}/prompt-revisions/{revision_id}")
async def get_agent_prompt_revision(agent_id: str, revision_id: str):
    from app.core import revisions as _rev
    rev = await _rev.get_revision(revision_id)
    if not rev or rev.get("entity_type") != _rev.ENTITY_AGENT_PROMPT \
            or rev.get("entity_id") != agent_id:
        raise HTTPException(404, "Revisão não encontrada para este agente")
    return rev


@router.post("/{agent_id}/prompt-revisions/{revision_id}/rollback")
async def rollback_agent_prompt_revision(agent_id: str, revision_id: str,
                                         request: Request):
    """Restaura um system_prompt antigo como SAVE NOVO (version bump; revisão
    source='rollback' com parent na restaurada) — histórico intacto. Só o
    system_prompt muda; o resto da config do agente fica como está."""
    from app.core import revisions as _rev
    existing = await agents_repo.find_by_id(agent_id)
    if not existing:
        raise HTTPException(404)
    rev = await _rev.get_revision(revision_id)
    if not rev or rev.get("entity_type") != _rev.ENTITY_AGENT_PROMPT \
            or rev.get("entity_id") != agent_id:
        raise HTTPException(404, "Revisão não encontrada para este agente")
    _caller = getattr(request.state, "auth_user", None) or {}
    new_version = _bump_version(existing.get("version", "1.0.0"))
    await agents_repo.update(agent_id, {
        "system_prompt": rev.get("content") or "", "version": new_version})
    await _rev.safe_record(
        entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=agent_id,
        content=rev.get("content") or "", version=new_version,
        source="rollback", author_user_id=_caller.get("id"),
        note=f"restaurado da revisão {revision_id} "
             f"(v{rev.get('version') or '?'})",
        parent_revision_id=revision_id,
    )
    await audit_repo.create({
        "entity_type": "agent", "entity_id": agent_id,
        "action": "prompt_rollback",
        "details": json.dumps({"revision_id": revision_id,
                               "new_version": new_version}),
    })
    return {"message": "System prompt restaurado como nova versão",
            "version": new_version}

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


async def _resolve_agent(ref: str) -> dict | None:
    """Resolve um agente por UUID (id PK) ou, em fallback, por name exato.
    Retorna None se nada bate. Lança 409 se o name é ambíguo (>1 hit) — caller
    é forçado a usar UUID nesses casos."""
    agent = await agents_repo.find_by_id(ref)
    if agent:
        return agent
    matches = await agents_repo.find_all(name=ref, limit=2)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            409,
            f"Nome '{ref}' é ambíguo — há {len(matches)}+ agentes com esse nome. Use o UUID.",
        )
    return None


@router.post("/{agent_id}/invoke", response_model=AgentInvokeResponse)
async def invoke_agent(agent_id: str, data: AgentInvokeRequest, request: Request) -> AgentInvokeResponse:
    from app.agents.engine import execute_interaction
    from app.agents.declarative_engine import execute_declarative
    from app.routes.workspace import _filter_attachments_by_agent
    from app.skill_parser.parser import parse_skill_md

    agent = await _resolve_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado (tentei UUID e name)")
    # Normaliza: a partir daqui agent_id é sempre o UUID — downstream
    # (execute_interaction, audit, etc) precisa do PK pra não tentar
    # find_by_id novamente com o name.
    agent_id = agent["id"]

    # IDOR (33.13.0): reusar um session_id exige ser DONO da interaction (senão
    # reinjetaria a conversa alheia no LLM). request.state.auth_user é populado
    # pelo ApiAuthMiddleware (cookie OU dono da API-key). ON-PATH, antes de executar.
    from app.core.interaction_access import assert_can_access_interaction, stamp_interaction_owner
    _caller = getattr(request.state, "auth_user", None) or {}
    await assert_can_access_interaction(data.session_id, _caller)
    # Escopo por-key (Onda 6): read_only → 403 (allowed_pipeline_ids se aplica no
    # invoke por-pipeline; aqui, invoke de agente, só o gate read_only).
    from app.core.apikey_scope import assert_api_key_can_invoke
    assert_api_key_can_invoke(request)

    # Regra de exposição: bloqueia invocação direta de subagents que fazem parte
    # de pipeline e de orquestradores sem pipeline configurada. Bypass via env
    # ALLOW_DIRECT_SUBAGENT_INVOKE=true (rollback emergencial, não recomendado).
    if os.getenv("ALLOW_DIRECT_SUBAGENT_INVOKE", "").lower() not in ("1", "true", "yes"):
        ok, reason = await _is_callable_externally(agent)
        if not ok:
            raise HTTPException(403, detail={
                "message": "Agente não pode ser invocado diretamente via API externa",
                "reason": reason,
                "hint": "Use GET /api/v1/agents/callable pra descobrir os agentes invocáveis.",
            })

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

    # Anexos: decodifica base64 + filtra por accepts_images/accepts_documents.
    # Modo declarativo não tem onde injetar arquivos (bindings HTTP usam só inputs),
    # então rejeita aqui em vez de silenciosamente ignorar.
    attachments_internal: list = []
    rejected_attachments: list = []
    if data.attachments:
        if is_declarative:
            raise HTTPException(400, detail={
                "message": "Modo declarativo não suporta anexos",
                "hint": "Use um agente em modo LLM para enviar arquivos, ou inclua os dados em 'inputs'.",
            })
        decoded, rejected_decode = _decode_attachments(data.attachments)
        # include_chain=True: a poda da porta decide pela UNIÃO das capacidades da
        # CADEIA do mesh (entrada + downstream), não só do agente de entrada. Sem
        # isto, invocar um ORQUESTRADOR que não aceita imagens (accepts_images=0)
        # PODAVA a imagem na porta ANTES de ela chegar ao especialista de visão
        # downstream → o SA recebia has_image=False, ficava no modelo text-only e a
        # imagem sumia (SA de visão respondia "nenhuma imagem enviada"). O caminho
        # workspace/UI já usa include_chain=True (workspace.py) — o invoke via API
        # ficou pra trás. Agente-folha sem downstream → união = próprias flags.
        attachments_internal, rejected_filter = await _filter_attachments_by_agent(
            decoded, agent["id"], include_chain=True
        )
        rejected_attachments = rejected_decode + rejected_filter

    start = time.time()

    # F6: pré-check de quota de custo por API Key (402 se a janela já estourou),
    # ANTES de qualquer execução com custo de LLM. Dry-run é isento (não gasta).
    if not (data.options and data.options.dry_run):
        await _enforce_apikey_budget(request)

    if is_declarative:
        dry_run = bool(data.options and data.options.dry_run)
        # Dono na CRIAÇÃO (35.15.1, achado da auditoria #4): o 3º branch da rota
        # (declarativo) criava a interaction SEM dono — só o stamp pós-execução
        # (:717) carimbava, e um crash/dry-run deixava a row órfã → com FF7 o
        # criador tomava 404 no retry. Seta o ContextVar ANTES da criação (o
        # execute_declarative o lê no interactions_repo.create). Paridade com os
        # branches pipeline (:778) e LLM (:847), que passam owner_user_id direto.
        from app.core.interaction_access import set_interaction_owner_for_creation
        set_interaction_owner_for_creation(_caller.get("id"))
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
            # Frase humana do ## Response Template (None se a skill não tem o bloco).
            "answer": result.get("answer"),
        }
        if dry_run:
            outputs_dict["plans"] = result.get("dry_run_plans") or []
            outputs_dict["dry_run"] = True
        else:
            # IDOR (35.2.0, fast-follow #581): o branch declarativo retornava SEM
            # carimbar o dono — a interaction ficava órfã (legada-sem-dono) e
            # reutilizável como session_id por terceiros; os branches pipe/LLM
            # já carimbavam.
            from app.core.interaction_access import stamp_interaction_owner
            await stamp_interaction_owner(result.get("interaction_id"), _caller.get("id"))
            # F12: atribui a interação declarativa à key (débito ~$0 — sem LLM).
            # SSOT (33.7.0): registra o invoke direto de agente org-wide, off-path.
            _schedule_agent_invoke_cost(request, agent_id, result)
            await _debit_and_attribute_apikey(request, result)
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
            rejected_attachments=rejected_attachments,
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

    # Detecta pipeline: agente com outgoing mesh_connections é entry de uma
    # cadeia. Nesse caso execute_pipeline itera por cada agente da chain e
    # carrega os MCP bindings de CADA um — única forma de subagentes
    # downstream rodarem suas próprias tools (Tavily etc). execute_interaction
    # sozinha só executa o agente entry com os bindings DELE, então MCPs de
    # subagentes ficavam de fora (bug observado: /workspace/chat?mode=pipeline
    # consumia Tavily, /invoke não).
    from app.core.database import mesh_repo
    is_pipeline_entry = bool(await mesh_repo.find_all(source_agent_id=agent["id"], limit=1))

    if is_pipeline_entry:
        from app.agents.engine import execute_pipeline
        try:
            pipe_result = await execute_pipeline(
                entry_agent_id=agent_id,
                user_input=user_input,
                channel=data.channel or "api",
                attachments=attachments_internal or None,
                # Multi-turno via API: reusa a session quando o caller informa
                # session_id (consistente com /workspace/chat). Sem ele → nova
                # interaction. context_mode='auto' reinjeta histórico; 'none'
                # = stateless (integração idempotente).
                session_id=data.session_id,
                context_mode=data.context_mode or "auto",
                # Dono na CRIAÇÃO (35.14.7, achado de auditoria #3): o invoke de
                # AGENTE nascia órfão (só o stamp pós-execução carimbava) — um
                # crash no meio deixava a row NULL e, com FF7, o PRÓPRIO criador
                # que deu o session_id recebia 404 no retry. Nasce com dono.
                owner_user_id=_caller.get("id"),
            )
        except ValueError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            raise HTTPException(500, f"Erro na execução do pipeline: {e}")

        # IDOR (33.13.0): carimba o dono na interaction (1º acesso, best-effort).
        await stamp_interaction_owner(pipe_result.get("interaction_id"), _caller.get("id"))

        pipe_status = pipe_result.get("status") or "completed"
        any_completed = pipe_result.get("completed_agents", 0) > 0
        invoke_status = "ok" if any_completed and pipe_status == "completed" else (
            "partial" if any_completed else "failed"
        )
        duration = pipe_result.get("duration_ms") or round((time.time() - start) * 1000, 2)

        await audit_repo.create({
            "entity_type": "agent",
            "entity_id": agent_id,
            "action": "invoked",
            "details": json.dumps({
                "mode": "pipeline",
                "session_id": pipe_result.get("interaction_id"),
                "total_agents": pipe_result.get("total_agents", 0),
                "completed_agents": pipe_result.get("completed_agents", 0),
                "passthrough_agents": pipe_result.get("passthrough_agents", 0),
                "duration_ms": duration,
            }, ensure_ascii=False),
        })

        # SSOT (33.7.0): registra o invoke direto de agente org-wide, off-path.
        _schedule_agent_invoke_cost(request, agent_id, pipe_result)
        # F6/F12: debita o custo real (soma dos steps) no ledger da key + atribui
        # a interação à key. Best-effort; gated pelo toggle. Só via X-API-Key.
        await _debit_and_attribute_apikey(request, pipe_result)

        return AgentInvokeResponse(
            session_id=pipe_result.get("interaction_id"),
            agent_id=agent_id,
            status=invoke_status,
            outputs={
                "answer": pipe_result.get("output", ""),
                "final_state": pipe_result.get("final_state"),
                "pipeline_steps": pipe_result.get("pipeline_steps", []),
                "total_agents": pipe_result.get("total_agents", 0),
                "completed_agents": pipe_result.get("completed_agents", 0),
                "passthrough_agents": pipe_result.get("passthrough_agents", 0),
            },
            context=data.context or {},
            trace_id=pipe_result.get("interaction_id"),
            duration_ms=duration,
            evidence_score=pipe_result.get("evidence_score"),
            rejected_attachments=rejected_attachments,
            # Cond-C (36.1.0): decisão estruturada do agente que respondeu —
            # o execute_pipeline a extrai ANTES do strip da linha.
            decision=pipe_result.get("decision"),
        )

    # Subagent standalone — execução de um único agente
    try:
        result = await execute_interaction(
            agent_id=agent_id,
            user_input=user_input,
            session_id=data.session_id,
            channel=data.channel or "api",
            journey=data.journey or "",
            attachments=attachments_internal or None,
            pipeline_context=pipeline_context,
            context_mode=data.context_mode or "auto",
            # Dono na CRIAÇÃO (35.14.7): idem branch pipeline — a interaction de
            # agente avulso nasce carimbada (era órfã até o stamp pós-execução).
            owner_user_id=_caller.get("id"),
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Erro na execução: {str(e)}")

    # IDOR (33.13.0): carimba o dono na interaction (1º acesso, best-effort).
    await stamp_interaction_owner(result.get("interaction_id"), _caller.get("id"))

    # Cond-C (35.19.0/36.1.0): a linha DECISAO é protocolo de máquina — a versão
    # ESTRUTURADA entra no envelope (`decision`) e a linha sai da resposta
    # apresentada (trace preserva; pipeline faz o equivalente na montagem
    # final). Helper combinado = 1 resolução de schema (review).
    _decision = None
    if result.get("output"):
        from app.agents.engine import decision_and_display_output
        _decision, result["output"] = await decision_and_display_output(result["output"], agent_id)

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

    # SSOT (33.7.0): registra o invoke direto de agente org-wide, off-path.
    _schedule_agent_invoke_cost(request, agent_id, result)
    # F6/F12: débito do custo real (do trace, agente single) + atribuição à key.
    await _debit_and_attribute_apikey(request, result)

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
        rejected_attachments=rejected_attachments,
        decision=_decision,
    )


# ───────────────────────────────────────────────────────────────────
# Histórico de invocações por agente — observabilidade
# ───────────────────────────────────────────────────────────────────

def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _serialize_row(r: dict) -> dict:
    return {k: _iso(v) for k, v in r.items()}


@router.get("/{agent_id}/last-activity")
async def get_agent_last_activity(agent_id: str):
    """Onda C.1: última invocação do agente — usado pelo painel de detalhe
    pra mostrar "Última atividade: 2h atrás · OK · 1.4s".

    Consulta interactions (tabela canonical de atividade NL) por agent_id,
    ordena por created_at desc, pega 1.

    Returns:
        {has_activity: bool, last_ts, state, ok, duration_ms,
         interaction_id, channel}
        Quando agente nunca foi invocado, has_activity=False e demais campos
        null/0 — UI mostra "Nunca invocado".

    Definição de `ok`:
        state in ('LogAndClose', 'completed', 'success') → True
        state in ('Refuse', 'Failed', 'error') → False
        Outros estados intermediários → None (em andamento ou inconclusivo)
    """
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    # Pega a 1 mais recente
    rows = await interactions_repo.find_all(
        limit=1, offset=0, agent_id=agent_id,
    )
    if not rows:
        return {
            "has_activity": False,
            "last_ts": None,
            "state": None,
            "ok": None,
            "duration_ms": 0,
            "interaction_id": None,
            "channel": None,
        }

    r = rows[0]
    state = (r.get("state") or "").strip()
    ok: bool | None
    if state in ("LogAndClose", "completed", "success"):
        ok = True
    elif state in ("Refuse", "Failed", "error", "failed"):
        ok = False
    else:
        ok = None

    # Duration: se temos started_at + ended_at, calcula; senão 0
    duration_ms = 0
    started = r.get("started_at")
    ended = r.get("ended_at")
    if started and ended and hasattr(started, "timestamp"):
        try:
            duration_ms = int((ended.timestamp() - started.timestamp()) * 1000)
        except Exception:
            duration_ms = 0

    return {
        "has_activity": True,
        "last_ts": _iso(r.get("created_at")),
        "state": state,
        "ok": ok,
        "duration_ms": duration_ms,
        "interaction_id": r.get("id"),
        "channel": r.get("channel") or "api",
    }


@router.get("/{agent_id}/stats")
async def get_agent_stats(agent_id: str, window: str = "7d"):
    """Onda C.2: stats agregados de uso do agente — usado pelo painel
    de detalhe pra mostrar "42 invocações · 90% sucesso · 12k tokens · $0.42".

    Agrega 5 tabelas em 1 só response: interactions (success rate),
    turns (tokens + latência p50/p99), tool_calls (count + cost), api_call_logs
    (count), binding_executions (count).

    Args:
        window: '24h' | '7d' | '30d' | 'all'. Inválido cai pra '7d'.

    Returns:
        {window, since, interactions, tokens, latency_ms, tool_calls,
         api_calls, binding_executions, estimated_cost_usd}
        Quando agente não tem atividade no período, todos counts = 0.
    """
    from datetime import datetime, timedelta
    from app.core.database import _get_pool
    from app.core.datetime_utils import naive_utc_now

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    # Resolve since — UTC naive: as colunas TIMESTAMP (sem tz) fazem o asyncpg
    # rejeitar datetime tz-aware no bind ("can't subtract offset-naive and
    # offset-aware datetimes").
    now = naive_utc_now()
    window_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
    if window == "all":
        since = datetime(1970, 1, 1)
        window_norm = "all"
    elif window in window_map:
        since = now - window_map[window]
        window_norm = window
    else:
        since = now - window_map["7d"]
        window_norm = "7d"

    pool = _get_pool()
    async with pool.acquire() as con:
        # Q1: Interactions success rate
        q1 = await con.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE state IN ('LogAndClose','completed','success')) AS ok,
                COUNT(*) FILTER (WHERE state IN ('Refuse','Failed','error','failed')) AS errors,
                COUNT(*) FILTER (WHERE state NOT IN ('LogAndClose','completed','success','Refuse','Failed','error','failed')) AS in_progress
            FROM interactions
            WHERE agent_id = $1 AND created_at >= $2
        """, agent_id, since)

        # Q2: Tokens + latency p50/p99 (turns JOINed com interactions)
        q2 = await con.fetchrow("""
            SELECT
                COALESCE(SUM(t.tokens_used), 0) AS total_tokens,
                COALESCE(AVG(t.latency_ms), 0)::float AS avg_latency,
                COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.latency_ms), 0)::float AS p50_latency,
                COALESCE(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY t.latency_ms), 0)::float AS p99_latency,
                COUNT(*) AS turn_count
            FROM turns t
            JOIN interactions i ON t.interaction_id = i.id
            WHERE i.agent_id = $1 AND i.created_at >= $2
        """, agent_id, since)

        # Q3: Tool calls breakdown (top tools)
        q3 = await con.fetch("""
            SELECT
                tc.tool_name,
                COUNT(*) AS count,
                COALESCE(AVG(tc.latency_ms), 0)::float AS avg_latency,
                COALESCE(SUM(tc.cost_usd), 0)::float AS cost_total
            FROM tool_calls tc
            JOIN interactions i ON tc.interaction_id = i.id
            WHERE i.agent_id = $1 AND i.created_at >= $2
            GROUP BY tc.tool_name
            ORDER BY count DESC
            LIMIT 10
        """, agent_id, since)

        # Q4: API calls
        q4 = await con.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status_code BETWEEN 200 AND 299) AS ok,
                COUNT(*) FILTER (WHERE status_code >= 400 OR status_code = 0) AS errors,
                COALESCE(AVG(latency_ms), 0)::float AS avg_latency
            FROM api_call_logs
            WHERE agent_id = $1 AND created_at >= $2
        """, agent_id, since)

        # Q5: Binding executions (declarative engine)
        q5 = await con.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status_code BETWEEN 200 AND 299) AS ok,
                COUNT(*) FILTER (WHERE status_code >= 400 OR status_code = 0) AS errors
            FROM binding_executions
            WHERE agent_id = $1 AND created_at >= $2
        """, agent_id, since)

    # Build response
    int_total = q1["total"] or 0
    int_ok = q1["ok"] or 0
    success_rate = (int_ok / int_total) if int_total > 0 else None

    tool_calls_total = sum(r["count"] for r in q3) if q3 else 0
    tool_calls_cost = sum(r["cost_total"] or 0 for r in q3) if q3 else 0.0

    return {
        "window": window_norm,
        "since": since.isoformat(),
        "interactions": {
            "total": int_total,
            "ok": int_ok,
            "errors": q1["errors"] or 0,
            "in_progress": q1["in_progress"] or 0,
            "success_rate": success_rate,
        },
        "tokens": {
            "total": int(q2["total_tokens"] or 0),
            "turn_count": q2["turn_count"] or 0,
        },
        "latency_ms": {
            "avg": int(q2["avg_latency"] or 0),
            "p50": int(q2["p50_latency"] or 0),
            "p99": int(q2["p99_latency"] or 0),
        },
        "tool_calls": {
            "total": tool_calls_total,
            "by_tool": [
                {
                    "name": r["tool_name"] or "(sem nome)",
                    "count": r["count"],
                    "avg_latency_ms": int(r["avg_latency"] or 0),
                    "cost_usd": round(r["cost_total"] or 0, 4),
                }
                for r in q3
            ],
        },
        "api_calls": {
            "total": q4["total"] or 0,
            "ok": q4["ok"] or 0,
            "errors": q4["errors"] or 0,
            "avg_latency_ms": int(q4["avg_latency"] or 0),
        },
        "binding_executions": {
            "total": q5["total"] or 0,
            "ok": q5["ok"] or 0,
            "errors": q5["errors"] or 0,
        },
        "estimated_cost_usd": round(tool_calls_cost, 4),
    }


@router.get("/{agent_id}/invocations")
async def list_agent_invocations(
    agent_id: str,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Lista interações deste agente (mais recentes primeiro). Filtro opcional por state da FSM."""
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    filters: dict = {"agent_id": agent_id}
    if state:
        filters["state"] = state
    limit_clamped = max(1, min(200, limit))
    offset_clamped = max(0, offset)
    rows = await interactions_repo.find_all(limit=limit_clamped, offset=offset_clamped, **filters)
    total = await interactions_repo.count(**filters)

    return {
        "agent": {"id": agent["id"], "name": agent.get("name"), "kind": agent.get("kind")},
        "invocations": [
            {
                "id": r["id"],
                "title": r.get("title") or "",
                "channel": r.get("channel") or "api",
                "state": r.get("state") or "",
                "journey_id": r.get("journey_id") or "",
                "started_at": _iso(r.get("started_at")),
                "ended_at": _iso(r.get("ended_at")),
                "created_at": _iso(r.get("created_at")),
            }
            for r in rows
        ],
        "total": total,
        "limit": limit_clamped,
        "offset": offset_clamped,
    }


@router.get("/{agent_id}/invocations/{interaction_id}")
async def get_invocation_detail(agent_id: str, interaction_id: str, request: Request):
    """Detalhe completo de uma invocação: turns + tool_calls + api_call_logs (matchados
    por janela temporal, já que api_call_logs não tem interaction_id) + audit + tempo_link."""
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")
    itx = await interactions_repo.find_by_id(interaction_id)
    if not itx or itx.get("agent_id") != agent_id:
        raise HTTPException(404, "Invocação não encontrada para este agente")
    # IDOR (33.15.0): só o DONO (ou root) vê os turns/tool_calls da invocação.
    # request.state.auth_user vem do ApiAuthMiddleware (cookie OU dono da API-key).
    from app.core.interaction_access import assert_can_access_interaction
    await assert_can_access_interaction(interaction_id, getattr(request.state, "auth_user", None) or {})

    turns = await turns_repo.find_all(interaction_id=interaction_id, limit=200)
    turns.sort(key=lambda t: t.get("turn_number") or 0)
    tool_calls = await tool_calls_repo.find_all(interaction_id=interaction_id, limit=200)

    # Preferência: FK direto (api_call_logs.interaction_id, populado pelo declarative
    # engine após Onda X). Fallback: janela temporal pra rows pré-migration (NULL).
    api_logs = await api_call_logs_repo.find_all(interaction_id=interaction_id, limit=500)
    if not api_logs:
        started = itx.get("started_at")
        # datetime.now(timezone.utc) — não datetime.utcnow() (tz-naive). A
        # coluna api_call_logs.created_at vem timestamptz do Postgres (tz-aware);
        # comparar com naive levanta TypeError. Bug fix 2026-06-01 reportado
        # pelo user (logs mostravam 500 em GET /agents/{id}/stats por bug
        # equivalente, já corrigido em get_agent_stats no commit 906809a).
        ended = itx.get("ended_at") or datetime.now(timezone.utc)
        api_logs_raw = await api_call_logs_repo.find_all(agent_id=agent_id, limit=500)
        api_logs = [
            log for log in api_logs_raw
            if not log.get("interaction_id")
            and started and log.get("created_at")
            and started <= log["created_at"] <= ended
        ]

    binding_execs = await binding_executions_repo.find_all(interaction_id=interaction_id, limit=200)
    # Ordena cronologicamente (find_all retorna DESC)
    binding_execs.reverse()

    audit_events = await audit_repo.find_all(entity_type="interaction", entity_id=interaction_id, limit=100)
    audit_events.reverse()  # cronológico

    trace_id = next((a["trace_id"] for a in audit_events if a.get("trace_id")), None)
    if not trace_id:
        try:
            td = json.loads(itx.get("trace_data") or "{}")
            trace_id = td.get("trace_id")
        except Exception:
            pass

    tempo_link = None
    if trace_id:
        grafana_url = os.getenv("GRAFANA_PUBLIC_URL", "http://localhost:3000").rstrip("/")
        left = ('{"datasource":"tempo","queries":[{"refId":"A","query":"' + trace_id +
                '"}],"range":{"from":"now-24h","to":"now"}}')
        tempo_link = f"{grafana_url}/explore?left={quote(left)}"

    return {
        "agent": {"id": agent["id"], "name": agent.get("name"), "kind": agent.get("kind")},
        "interaction": _serialize_row(itx),
        "turns": [_serialize_row(t) for t in turns],
        "binding_executions": [_serialize_row(be) for be in binding_execs],
        "tool_calls": [_serialize_row(tc) for tc in tool_calls],
        "api_call_logs": [_serialize_row(a) for a in api_logs],
        "audit_events": [_serialize_row(a) for a in audit_events],
        "trace_id": trace_id,
        "tempo_link": tempo_link,
    }


# ═══════════════════════════════════════════════════════════════════
# Diagnóstico do Agente (2026-06-01) — paridade com tela de Skill mas
# aproveitando dados que SÓ o Agente tem: modelo configurado, histórico
# de execução, conexões no mesh, capacidades (accepts_images etc).
# ═══════════════════════════════════════════════════════════════════


@router.get("/{agent_id}/diagnostics")
async def agent_diagnostics(agent_id: str):
    """Diagnóstico rico do agente — 3 grupos de informação.

    1. **cost**: custo/chamada e /mês usando o MODELO do agente (não
       gpt-4o-mini hardcoded). Estimativa de tokens via tiktoken (com
       fallback char/4). Base de 1000 chamadas/mês para comparar com
       a tela do Skill.
    2. **performance**: latência p50/p95 e taxa de sucesso (% de
       interactions que terminaram em Recommend) nos últimos 30 dias.
    3. **capabilities**: badges visuais (visão, documentos, tools_count,
       mesh_in/out) + alerta de incompat multimodal (accepts_images=true
       + modelo text-only força fallback em runtime).
    4. **health**: score 0-100 combinando success_rate + (1-drift) +
       presença de skill. Drift estimado como % de respostas que NÃO
       terminaram em Recommend.

    Endpoint stateless, calculado on-demand. Cache HTTP de 60s sugerido
    pelo header (frontend pode recachear ao trocar modelo do agente).
    """
    from app.core.database import (
        mesh_repo,
        skills_repo as _skills_repo,
        interactions_repo as _interactions_repo,
    )
    from app.core.llm_pricing import compute_cost, get_pricing
    from app.core.token_estimator import estimate_tokens

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    model = agent.get("model") or "gpt-4o-mini"
    provider = (agent.get("llm_provider") or "").lower()

    # ── Custo (estimativa) ──────────────────────────────────────────
    # Conta como tokens de input: system_prompt + SKILL.md inteiro.
    # Não inclui input do user, output do LLM, evidências RAG, tool
    # results — mesma base que a tela do Skill usa pra comparabilidade.
    system_prompt = (agent.get("system_prompt") or "").strip()
    skill_raw = ""
    skill_id = agent.get("skill_id")
    if skill_id:
        sk = await _skills_repo.find_by_id(skill_id)
        if sk:
            skill_raw = (sk.get("raw_content") or "").strip()

    total_input_chars = len(system_prompt) + len(skill_raw)
    total_input_tokens = estimate_tokens(system_prompt + "\n" + skill_raw, model=model)
    # Assume output médio de 500 tokens (paridade com base do Skill)
    assumed_output_tokens = 500
    # compute_cost trabalha em USD/1k tokens internamente; expomos na UI
    # também os preços por 1M para comparabilidade com tela do Skill e
    # documentação OpenAI.
    cost_call = compute_cost(provider, model, total_input_tokens, assumed_output_tokens)
    cost_month = cost_call * 1000  # base de 1000 chamadas/mês
    pricing_entry = get_pricing(provider, model) or {"input": 0.00015, "output": 0.0006}
    input_price_per_1m = pricing_entry["input"] * 1000
    output_price_per_1m = pricing_entry["output"] * 1000

    # ── Performance (histórico real) ────────────────────────────────
    perf_p50_ms = None
    perf_p95_ms = None
    success_rate = None
    interactions_30d = 0
    try:
        # find_all retorna DESC por created_at. Pega últimas 200.
        all_int = await _interactions_repo.find_all(agent_id=agent_id, limit=200)
        cutoff = datetime.now(timezone.utc).timestamp() - (30 * 86400)
        recent = []
        for itx in all_int:
            ca = itx.get("created_at")
            if hasattr(ca, "timestamp") and ca.timestamp() >= cutoff:
                recent.append(itx)
        interactions_30d = len(recent)
        if recent:
            durations = [
                int(itx.get("duration_ms") or 0)
                for itx in recent
                if itx.get("duration_ms")
            ]
            if durations:
                durations.sort()
                perf_p50_ms = durations[len(durations) // 2]
                idx95 = max(0, int(len(durations) * 0.95) - 1)
                perf_p95_ms = durations[idx95]
            # "Sucesso" = Recommend OU LogAndClose — paridade com engine.py
            # (~L2147), que loga AMBOS os estados como nível "success". Antes
            # só Recommend contava, penalizando agentes determinísticos (ex.:
            # lookup de CEP via skill declarativa) que legitimamente fecham em
            # LogAndClose sem recomendar nada.
            success_count = sum(
                1
                for itx in recent
                if (itx.get("state") or "").lower().startswith(("recommend", "logandclose"))
            )
            success_rate = round(success_count / len(recent), 3)
    except Exception as e:
        logger.warning(
            "agents.diagnostics.perf_query_failed",
            extra={
                "event": "agents.diagnostics",
                "agent_id": agent_id,
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )

    # ── Capabilities + incompat warning ─────────────────────────────
    from app.llm_routing import is_multimodal as _is_multimodal

    accepts_images = bool(agent.get("accepts_images"))
    accepts_documents = bool(agent.get("accepts_documents"))
    multimodal_warning = accepts_images and not _is_multimodal(provider, model)

    # Tools count: parse skill raw content para tool_bindings + api_bindings
    tools_mcp_count = 0
    tools_api_count = 0
    if skill_raw:
        try:
            from app.skill_parser.parser import parse_skill_md
            parsed = parse_skill_md(skill_raw)
            tools_api_count = len(parsed.api_bindings_parsed or [])
            # MCP tool bindings: linhas começando com '- `<uuid>` (nome)' na
            # seção ## Tool Bindings (mesma heurística do engine)
            tool_section = (parsed.tool_bindings or "")
            tools_mcp_count = tool_section.count("- `") if tool_section else 0
        except Exception:
            pass

    # Mesh in/out
    mesh_in = 0
    mesh_out = 0
    try:
        in_conns = await mesh_repo.find_all(target_agent_id=agent_id, limit=50)
        out_conns = await mesh_repo.find_all(source_agent_id=agent_id, limit=50)
        mesh_in = len(in_conns or [])
        mesh_out = len(out_conns or [])
    except Exception:
        pass

    # ── Health score ────────────────────────────────────────────────
    # Composto: 60% success_rate + 20% (skill presente) + 20% (sem
    # multimodal_warning). Se sem histórico, devolve apenas o "potencial"
    # baseado em config (skill + capabilities).
    score_components = []
    if success_rate is not None:
        score_components.append(("success_rate", success_rate, 60))
    score_components.append(("has_skill", 1.0 if skill_id else 0.0, 20))
    score_components.append(("no_multimodal_warning", 0.0 if multimodal_warning else 1.0, 20))
    total_weight = sum(w for _, _, w in score_components)
    weighted = sum(v * w for _, v, w in score_components)
    health_score = int(round((weighted / total_weight) * 100)) if total_weight else 50

    # Confiabilidade do score (ver MIN_RELIABLE_HEALTH_SAMPLE). Com amostra
    # pequena o frontend pinta neutro + "provisório" em vez de verde/vermelho.
    health_reliable = interactions_30d >= MIN_RELIABLE_HEALTH_SAMPLE

    return {
        "agent": {
            "id": agent_id,
            "name": agent.get("name"),
            "model": model,
            "provider": provider,
        },
        "cost": {
            "model": model,
            "input_chars": total_input_chars,
            "input_tokens_est": total_input_tokens,
            "output_tokens_assumed": assumed_output_tokens,
            "input_price_per_1m_usd": round(input_price_per_1m, 4),
            "output_price_per_1m_usd": round(output_price_per_1m, 4),
            "cost_per_call_usd": round(cost_call, 6),
            "cost_per_month_usd": round(cost_month, 4),
            "calls_per_month_base": 1000,
        },
        "performance": {
            "interactions_last_30d": interactions_30d,
            "p50_latency_ms": perf_p50_ms,
            "p95_latency_ms": perf_p95_ms,
            "success_rate": success_rate,  # None se sem histórico
            "drift_pct": round(1 - success_rate, 3) if success_rate is not None else None,
        },
        "capabilities": {
            "accepts_images": accepts_images,
            "accepts_documents": accepts_documents,
            "is_multimodal_model": _is_multimodal(provider, model),
            "multimodal_warning": multimodal_warning,
            "tools_mcp_count": tools_mcp_count,
            "tools_api_count": tools_api_count,
            "mesh_upstream_count": mesh_in,
            "mesh_downstream_count": mesh_out,
        },
        "health": {
            "score": health_score,
            "reliable": health_reliable,
            "min_sample": MIN_RELIABLE_HEALTH_SAMPLE,
            "sample_size": interactions_30d,
            "components": [
                {"name": n, "value": v, "weight": w}
                for n, v, w in score_components
            ],
        },
    }


# ═══════════════════════════════════════════════════════════════════
# "Conhecer o agente" — explicador que NÃO executa o agente.
# Assistente meta que lê a DEFINIÇÃO do agente (config + SKILL.md +
# posição no mesh + comportamento agregado) e responde perguntas sobre
# ele. ZERO efeito colateral: nunca chama execute_interaction, não cria
# interação/turno nem consome o orçamento do agente. Superfície de UI.
# ═══════════════════════════════════════════════════════════════════

_EXPLAINER_SYSTEM = (
    "Você é o \"Guia do Agente\" da plataforma Maestro. Sua função é EXPLICAR um "
    "agente para o operador humano: o que ele faz, seu propósito, quando é "
    "acionado, como está configurado, sua posição no fluxo (AI Mesh) e como "
    "costuma se comportar. Responda SEMPRE e SÓ com base na FICHA DO AGENTE.\n\n"
    "Regras:\n"
    "- Você NÃO é o agente e NÃO o executa. Nunca responda \"no papel\" dele nem "
    "simule a resposta que ele daria a um cliente. Se pedirem para você agir como "
    "o agente ou testá-lo, explique que este é o modo \"Conhecer\" (não executa) e "
    "sugira o Playground ou o botão Executar para testar de verdade.\n"
    "- Não invente nada fora da ficha. Se a informação não estiver lá, diga com "
    "franqueza o que não dá para saber.\n"
    "- Glossário pt-BR: Maestro (orquestrador), Triagem (roteador), Especialista; "
    "Dono, Curador.\n"
    "- Adapte a PROFUNDIDADE à pergunta: curta para dúvida simples; detalhada e "
    "didática quando pedirem para aprofundar. Cite as seções da skill, os valores "
    "de configuração, as regras condicionais e as frases-prova quando ajudarem."
)


def _agent_role_label(kind: str | None) -> str:
    return {
        "aobd": "Maestro (orquestrador)",
        "orchestrator": "Maestro (orquestrador)",
        "router": "Triagem (roteador)",
        "subagent": "Especialista",
    }.get(kind or "", kind or "agente")


async def _build_agent_ficha(agent_id: str, agent: dict) -> str:
    """Monta a 'ficha' textual do agente para o explicador — SÓ LEITURA.

    Nunca executa o agente: lê config + SKILL.md + arestas do mesh (com regras)
    + diagnóstico agregado (reusa `agent_diagnostics`, também só leitura)."""
    from app.core.database import mesh_repo
    L: list[str] = []
    L.append(f"# {agent.get('name') or agent_id}")
    L.append(
        f"- Papel: {_agent_role_label(agent.get('kind'))} (kind={agent.get('kind')}) "
        f"| Domínio: {agent.get('domain') or '—'} | Status: {agent.get('status')} "
        f"| Versão: {agent.get('version')}"
    )
    L.append(
        f"- Modelo: {agent.get('llm_provider')}/{agent.get('model')} "
        f"| task_type: {agent.get('task_type')} | temperatura: {agent.get('temperature')} "
        f"| reasoning_effort: {agent.get('reasoning_effort') or '—'} "
        f"| idioma: {agent.get('response_language') or '—'}"
    )
    L.append(
        f"- Exigir evidência (RAG): {'sim' if agent.get('require_evidence') else 'não'} "
        f"| Conhecimento geral do LLM: {'sim' if agent.get('allow_general_knowledge') else 'não'} "
        f"| Aceita documentos: {bool(agent.get('accepts_documents'))} "
        f"| Aceita imagens: {bool(agent.get('accepts_images'))}"
    )
    sp = (agent.get("system_prompt") or "").strip()
    if sp:
        L.append("\n## System prompt do agente\n" + sp)
    skill_raw = ""
    if agent.get("skill_id"):
        sk = await skills_repo.find_by_id(agent["skill_id"])
        if sk:
            skill_raw = (sk.get("raw_content") or "").strip()
    L.append(
        "\n## SKILL.md (a partitura executável do agente)\n"
        + (skill_raw or "(agente sem skill vinculada)")
    )
    # Posição no mesh: quem chama e para quem roteia + as regras condicionais
    try:
        in_conns = await mesh_repo.find_all(target_agent_id=agent_id, limit=50) or []
        out_conns = await mesh_repo.find_all(source_agent_id=agent_id, limit=50) or []
    except Exception:
        in_conns, out_conns = [], []
    ids = {c.get("source_agent_id") for c in in_conns} | {c.get("target_agent_id") for c in out_conns}
    ids.discard(agent_id)
    ids.discard(None)
    names: dict = {}
    for _id in ids:
        try:
            a = await agents_repo.find_by_id(_id)
            if a:
                names[_id] = a.get("name") or _id
        except Exception:
            pass

    def _edge_desc(conn: dict) -> str:
        ctype = conn.get("connection_type") or "sequential"
        expr = None
        ntp = 0
        cfg = conn.get("config")
        try:
            c = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
            expr = c.get("expr")
            ntp = len(c.get("test_phrases") or [])
        except Exception:
            pass
        extra = ""
        if ctype == "conditional" and expr:
            extra += f" — regra: `{expr}`"
        if ntp:
            extra += f" ({ntp} frases-prova)"
        return f"{ctype}{extra}"

    L.append("\n## Posição no AI Mesh")
    if in_conns:
        L.append("Quem aciona este agente (arestas de entrada):")
        for c in in_conns:
            L.append(f"  - {names.get(c.get('source_agent_id'), c.get('source_agent_id'))} → este [{_edge_desc(c)}]")
    else:
        L.append("Sem arestas de entrada — pode ser um agente-raiz (Início) ou isolado.")
    if out_conns:
        L.append("Para quem este agente roteia (arestas de saída):")
        for c in out_conns:
            L.append(f"  - este → {names.get(c.get('target_agent_id'), c.get('target_agent_id'))} [{_edge_desc(c)}]")
    else:
        L.append("Sem arestas de saída — é um nó terminal (não delega adiante).")
    # Comportamento agregado — reusa /diagnostics (só leitura, não executa nada)
    try:
        diag = await agent_diagnostics(agent_id)
        slim = {k: diag.get(k) for k in ("performance", "capabilities", "health", "cost")}
        L.append(
            "\n## Comportamento observado (diagnóstico agregado, só leitura)\n```json\n"
            + json.dumps(slim, ensure_ascii=False, default=str)[:2500]
            + "\n```"
        )
    except Exception:
        pass
    return "\n".join(L)


@router.post("/{agent_id}/explain")
async def explain_agent(agent_id: str, payload: dict, request: Request,
                        user: dict = Depends(require_user)):
    """Conhecer o agente — assistente que EXPLICA o agente (NÃO executa).

    Payload: ``{"message": str, "history": [{"role","content"}]?}``
    Retorno: ``{"answer", "agent_id", "agent_name"}``

    **ZERO efeito colateral**: não invoca o agente (nunca chama
    ``execute_interaction``), não cria interação/turno nem consome o orçamento
    do agente — só LÊ a definição e chama o LLM 'explicador' (custo sai no balde
    ``route=agent_explain``, fora do ledger do agente). Superfície de UI: o
    principal via X-API-Key recebe 403 acionável (gêmeo do guard de
    suggest-conditional — sem ele qualquer chave queimaria LLM sem limite aqui).
    """
    if getattr(request.state, "api_key_id", None):
        raise HTTPException(403, {
            "error": "explain_agent_ui_only",
            "message": "O modo 'Conhecer o agente' é da superfície de UI (sessão). "
                       "Para consumir o agente use POST /api/v1/agents/{id}/invoke ou o pipeline.",
        })
    message = (payload.get("message") or "").strip()
    if not message:
        return {"error": "Faça uma pergunta sobre o agente (ex.: 'o que você faz?', 'quando é acionado?')."}

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")

    ficha = await _build_agent_ficha(agent_id, agent)
    messages = [{"role": "system", "content": _EXPLAINER_SYSTEM + "\n\n=== FICHA DO AGENTE ===\n" + ficha}]
    for h in (payload.get("history") or [])[-10:]:
        role = "assistant" if str(h.get("role")) in ("assistant", "agent") else "user"
        c = (h.get("content") or h.get("text") or "").strip()
        if c:
            messages.append({"role": role, "content": c[:2000]})
    messages.append({"role": "user", "content": message[:4000]})

    from app.llm_routing import resolve_llm_for_task
    from app.routes.wizard import _wizard_llm_complete
    try:
        provider, model = await resolve_llm_for_task("reasoning")
    except Exception:
        provider, model = await resolve_llm_for_task("instruct")
    try:
        content, _, _ = await _wizard_llm_complete(
            messages, provider, model, route="agent_explain", temperature=0.2,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("agent_explain falhou", exc_info=True,
                     extra={"event": "agents.explain", "agent_id": agent_id})
        raise HTTPException(500, f"Erro ao explicar o agente: {e}")
    # Regra do produto: nenhuma resposta de API com emoji (garantia mesmo se o LLM insistir).
    return {"answer": strip_emoji((content or "").strip()), "agent_id": agent_id, "agent_name": agent.get("name")}
