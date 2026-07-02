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
import hashlib
from datetime import datetime

from app.core.datetime_utils import naive_utc_now
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


# ─────────────────────────────────────────────────────────────────────────────
# Args estruturados do invoke (D1/D2) — campo `args` opcional no contrato.
# Reusa o schema que o `/inputs-schema` já publica (## Inputs do agente-raiz) +
# os mesmos validadores/coersores do agent invoke e do chat. Os args são
# DOBRADOS na entrada como bloco "## Parâmetros estruturados" (raiz LLM lê como
# contexto; raiz declarativa extrai via `_extract_inputs_from_text`). Texto livre
# em `message` permanece o caminho primário e intacto.
# ─────────────────────────────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _did_you_mean(name: str, candidates: list) -> Optional[str]:
    """Campo mais próximo (Levenshtein) — só sugere se for 'perto' o bastante,
    pra um typo (`uf`→`ufe`) virar dica sem inventar associação distante."""
    best, best_d = None, 1 << 30
    for c in candidates:
        d = _levenshtein(name.lower(), str(c).lower())
        if d < best_d:
            best, best_d = c, d
    if best is not None and best_d <= max(2, len(name) // 3):
        return best
    return None


def _validate_and_coerce_args(args: dict, schema: Optional[dict]) -> tuple:
    """Coage `args` contra o JSON Schema do agente-raiz e valida.

    Retorna ``(coerced, issues)`` — issues vazio = ok. Sem schema/properties →
    devolve os args como vieram, SEM validar (pipeline aceita texto livre, então
    args livres também passam). Política: coage tipos (lenient) → exige required →
    checa tipo/enum → REJEITA chave fora do contrato (governança) com did-you-mean.
    """
    if not isinstance(args, dict):
        return {}, []
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return args, []
    from app.routes.workspace import _coerce_inputs_by_schema
    from app.routes.agents import _JSON_TYPE_MAP
    coerced = _coerce_inputs_by_schema(args, schema)
    issues: list = []
    required = schema.get("required")
    if isinstance(required, list):
        for field in required:
            v = coerced.get(field)
            # ausente OU vazio (string em branco / None) → não satisfaz required.
            # Alinha com a validação do cliente (form do Playground).
            if field not in coerced or v is None or (isinstance(v, str) and not v.strip()):
                issues.append({"field": field, "code": "required_missing"})
    for field, val in coerced.items():
        if field not in props:
            issue = {"field": field, "code": "unknown_field"}
            dym = _did_you_mean(field, list(props))
            if dym:
                issue["did_you_mean"] = dym
            issues.append(issue)
            continue
        spec = props.get(field)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        py_type = _JSON_TYPE_MAP.get(expected) if expected else None
        if py_type and not isinstance(val, py_type):
            issues.append({"field": field, "code": "type_mismatch",
                           "expected": expected, "received": type(val).__name__})
            continue
        enum = spec.get("enum")
        # comparação tolerante a tipo: enum [1,2] SEM `type` não é coagido, e o valor
        # pode chegar como string "1" (form/JSON) — compara também por str pra não dar
        # 422 falso (e casar com a validação do cliente, que compara por String()).
        if isinstance(enum, list) and enum and val not in enum and str(val) not in [str(e) for e in enum]:
            issues.append({"field": field, "code": "enum_mismatch", "allowed": enum})
    return coerced, issues


def _fold_args_into_input(user_input: str, args: dict) -> str:
    """Anexa os args como bloco "## Parâmetros estruturados" (mesmo padrão do
    agent invoke, agents.py). O fenced ```json é o que `_extract_inputs_from_text`
    do engine pesca pro agente-raiz declarativo."""
    block = ("## Parâmetros estruturados\n```json\n"
             + json.dumps(args, ensure_ascii=False, indent=2) + "\n```")
    return f"{user_input}\n\n{block}" if user_input else block


async def _validate_and_fold_args(pid: str, p: dict, root: str, user_input: str, args: dict) -> tuple:
    """Valida/coage `args` contra o contrato do pipeline (SELADO se publicado, senão
    o ## Inputs vivo) e SEPARA em dois baldes: `param` (envelope selado, fora da prosa)
    e o resto (dobrado na prosa). Devolve ``(user_input_dobrado, sealed_param_dict)``.
    422 (nomeando cada campo) se os args não conferem. Sem schema → tudo cai na prosa."""
    schema, _sealed = await _resolve_invoke_schema(p, root)
    res = _resolve_args(args, schema)  # coage + aplica defaults + valida (fonte única)
    if res["issues"]:
        raise HTTPException(422, {
            "error": "args_validation_failed",
            "pipeline_id": pid,
            "root_agent_id": root,
            "issues": res["issues"],
            "schema_url": f"/api/v1/pipelines/{pid}/inputs-schema",
        })
    resolved = res["resolved"]
    uso = res["uso"]
    # args só com opcionais vazios (a coerção podou tudo) + sem texto livre → não há
    # nada a executar; evita rodar o pipeline (gasta LLM) com entrada vazia.
    if not user_input.strip() and not resolved:
        raise HTTPException(400, "Informe 'message' (ou 'input') ou 'args' com ao menos um valor.")
    # SPLIT: `param` (exato/determinístico) vai no envelope selado, fora da prosa;
    # o resto (`llm`/sem anotação) é dobrado como bloco que o LLM lê. Default = prosa
    # → comportamento legado de quem não usa `x-uso`.
    sealed = {k: v for k, v in resolved.items() if uso.get(k) == "param"}
    prose = {k: v for k, v in resolved.items() if uso.get(k) != "param"}
    folded = _fold_args_into_input(user_input, prose) if prose else user_input
    return folded, sealed


# ─────────────────────────────────────────────────────────────────────────────
# Defaults + proveniência + modo `dry` (pré-visualização) — camada sobre D1/D2.
# Aplica os `default` declarados no ## Inputs (antes ausentes: D2 nunca os usava),
# e expõe um modo que RESOLVE os args (coage→defaults→valida) devolvendo o payload
# final + a origem de cada campo (caller|default) SEM executar (não gasta LLM).
# ─────────────────────────────────────────────────────────────────────────────

def _apply_defaults(args: dict, schema: Optional[dict]) -> tuple:
    """Preenche campos AUSENTES que declaram `default` no schema. Retorna
    ``(merged, defaulted_keys)``. Aplicado ANTES da validação pra que um required
    com `default` seja satisfeito quando o caller omite."""
    base = dict(args) if isinstance(args, dict) else {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return base, set()
    import copy
    defaulted = set()
    for field, spec in props.items():
        if isinstance(spec, dict) and "default" in spec and field not in base:
            # deepcopy: default mutável (list/dict) não compartilha referência com o
            # schema re-parseado (footgun latente se algo mutar `resolved`).
            base[field] = copy.deepcopy(spec["default"])
            defaulted.add(field)
    return base, defaulted


def _field_uso(schema: Optional[dict], field: str) -> str:
    """Intenção do campo (envelope selado): 'param' = valor EXATO/determinístico,
    viaja fora da prosa e NÃO é tratado pelo LLM; 'llm' = interpretável, vai na
    prosa. Default 'llm' (= comportamento legado) quando não há anotação `x-uso`."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    spec = props.get(field) if isinstance(props, dict) else None
    uso = spec.get("x-uso") if isinstance(spec, dict) else None
    return "param" if uso == "param" else "llm"


def _resolve_args(args: dict, schema: Optional[dict]) -> dict:
    """Resolve os args: aplica defaults → coage → valida. Devolve
    ``{resolved, issues, provenance, uso}`` com provenance[campo] ∈ {caller, default}
    e uso[campo] ∈ {param, llm}. Fonte única de verdade da resolução, usada pela
    execução (split em baldes) e pela pré-visualização (dry)."""
    original = args if isinstance(args, dict) else {}
    merged, defaulted = _apply_defaults(original, schema)
    coerced, issues = _validate_and_coerce_args(merged, schema)
    provenance = {
        k: ("default" if (k in defaulted and k not in original) else "caller")
        for k in coerced
    }
    return {
        "resolved": coerced, "issues": issues, "provenance": provenance,
        "uso": {k: _field_uso(schema, k) for k in coerced},
    }


async def _fetch_root_schema(root: str) -> Optional[dict]:
    """JSON Schema (## Inputs) do agente-raiz, ou None. Raiz órfã (404) → None,
    sem vazar o 404 de "agente" num endpoint de PIPELINE."""
    from app.routes.agents import get_agent_inputs_schema
    try:
        return (await get_agent_inputs_schema(root)).get("inputs_schema")
    except HTTPException:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# D4 — contrato de args SELADO/versionado. Sela a ENTRADA como `allowed_agent_ids`
# sela o GRAFO: ao publicar, o ## Inputs do agente-raiz é congelado no pipeline
# (schema + hash + versão). O invoke de um pipeline PUBLICADO valida contra o SELO,
# não contra o skill vivo — a API do pipeline publicado não muda quando o autor
# edita o skill. Rascunho valida ao vivo (conveniência de autoria). Re-publicar
# re-sela (versão sobe só quando o hash muda).
# ─────────────────────────────────────────────────────────────────────────────

def _schema_hash(schema: Optional[dict]) -> str:
    canonical = json.dumps(schema or {}, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _parse_contract(v) -> Optional[dict]:
    """args_contract (JSONB) → dict. asyncpg pode devolver str ou dict conforme codec."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else None
        except (ValueError, TypeError):
            return None
    return None


async def _resolve_invoke_schema(p: dict, root: str) -> tuple:
    """Retorna ``(schema, sealed_bool)``. PUBLICADO + com selo → schema SELADO
    (estável). Rascunho/sem selo → schema VIVO do agente-raiz."""
    if p.get("status") == "publicado" and p.get("contract_hash"):
        sealed = _parse_contract(p.get("args_contract"))
        return (sealed or None), True  # {} selado (raiz sem ## Inputs) → None
    return (await _fetch_root_schema(root)), False


async def _seal_args_contract(pid: str) -> None:
    """Congela o ## Inputs do agente-raiz no pipeline (schema+hash+versão). Versão só
    incrementa quando o hash MUDA — re-publicar sem mudança mantém a versão (estável
    p/ integradores). JSONB → json.dumps antes do asyncpg."""
    from app.catalog.pipeline_defs import _build_subgraph
    sub = await _build_subgraph(pid)
    root = sub.get("root_agent_id")
    schema = await _fetch_root_schema(root) if root else None
    h = _schema_hash(schema)
    p = await pipelines_repo.find_by_id(pid) or {}
    prev_hash = p.get("contract_hash")
    prev_ver = p.get("contract_version") or 0
    version = prev_ver if (prev_hash == h and prev_ver) else prev_ver + 1
    await pipelines_repo.update(pid, {
        "args_contract": json.dumps(schema or {}),
        "contract_version": version,
        "contract_hash": h,
        "contract_sealed_at": naive_utc_now(),
    })


async def _plan_args(pid: str, p: dict, root: str, args: dict) -> dict:
    """Modo `dry`: resolve os args (coage/defaults/valida) contra o contrato (SELADO
    se publicado) e devolve o payload resolvido + proveniência, SEM executar. 422 se
    os args não conferem. Sinaliza se o contrato é selado e sua versão."""
    schema, sealed = await _resolve_invoke_schema(p, root)
    res = _resolve_args(args or {}, schema)
    if res["issues"]:
        raise HTTPException(422, {
            "error": "args_validation_failed",
            "pipeline_id": pid,
            "root_agent_id": root,
            "issues": res["issues"],
            "schema_url": f"/api/v1/pipelines/{pid}/inputs-schema",
        })
    return {
        "dry": True,
        "pipeline_id": pid,
        "root_agent_id": root,
        "resolved_args": res["resolved"],
        "provenance": res["provenance"],
        "uso": res["uso"],  # campo→param|llm: qual vai no envelope selado vs prosa
        "has_schema": bool(isinstance(schema, dict) and schema.get("properties")),
        "sealed": sealed,                                   # validou contra o contrato SELADO?
        "contract_version": p.get("contract_version"),      # versão do selo (None se rascunho)
    }


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


@router.get("/{pid}/inputs-schema")
async def get_pipeline_inputs_schema(pid: str):
    """Inputs ESPERADOS do pipeline = inputs do seu agente de ENTRADA (raiz).

    Resolve a raiz via ``_build_subgraph`` (a MESMA do invoke) e reusa o schema do
    agente (``## Inputs`` + variáveis ``inputs.*`` dos API Bindings). Read-only — ajuda
    a montar o payload no Playground ("inputs esperados" / "inserir template"). Sem
    raiz/inputs → schema vazio (o pipeline aceita texto livre na mensagem).
    """
    p = await _require(pid)
    from app.catalog.pipeline_defs import _build_subgraph
    sub = await _build_subgraph(pid)
    root = sub.get("root_agent_id")
    is_sealed = p.get("status") == "publicado" and bool(p.get("contract_hash"))

    # Metadata VIVA do agente-raiz (agent/skill/inputs_referenced/api_bindings + schema vivo).
    live = None
    if root:
        from app.routes.agents import get_agent_inputs_schema
        try:
            live = await get_agent_inputs_schema(root)
        except HTTPException as e:
            # Raiz órfã (agente removido) → engole só o 404, não vaza num endpoint de PIPELINE.
            if e.status_code != 404:
                raise
    base = {
        "pipeline_id": pid, "root_agent_id": root, "agent": None, "skill": None,
        "inputs_schema": None, "inputs_referenced": [], "api_bindings": [], "execution_mode": None,
    }
    if live:
        base.update(live)
    base["pipeline_id"] = pid
    base["root_agent_id"] = root
    base["sealed"] = is_sealed

    # D4: pipeline PUBLICADO expõe o contrato SELADO (o que o invoke valida), com
    # `contract_drift` quando o autor editou o skill depois (precisa re-publicar).
    if is_sealed:
        base["inputs_schema"] = _parse_contract(p.get("args_contract")) or None
        base["contract_version"] = p.get("contract_version")
        base["contract_hash"] = p.get("contract_hash")
        live_hash = _schema_hash(live.get("inputs_schema")) if live else None
        base["contract_drift"] = bool(live and live_hash != p.get("contract_hash"))
    return base


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
        patch["updated_at"] = naive_utc_now()
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
    await pipelines_repo.update(pid, {"status": to_state, "updated_at": naive_utc_now()})
    # D4: ao PUBLICAR, sela o contrato de args (congela o ## Inputs do agente-raiz).
    # O invoke de um pipeline publicado passa a validar contra o SELO. Best-effort:
    # falha ao selar NÃO impede a publicação (o invoke cai no schema vivo até re-selar).
    sealed_info = None
    if to_state == "publicado":
        try:
            await _seal_args_contract(pid)
            fresh = await pipelines_repo.find_by_id(pid) or {}
            sealed_info = {"version": fresh.get("contract_version"), "hash": fresh.get("contract_hash")}
        except Exception:
            pass
    await audit_repo.create({
        "entity_type": "pipeline",
        "entity_id": pid,
        "action": "status_changed",
        "details": json.dumps({"from": current, "to": to_state, "sealed_contract": sealed_info}),
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
    await pipelines_repo.update(pid, {"entry_agent_id": agent_id, "updated_at": naive_utc_now()})
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
    # `dry` dispensa entrada (resolve o que houver, até defaults). `args` presente
    # — MESMO `{}` — engaja o contrato (defaults podem preencher); por isso o guard
    # usa `is None` (igual ao gatilho de fold), não `not data.args`. Se nada resultar,
    # o 400 vem de dentro de _validate_and_fold_args.
    if not user_input and data.args is None and not data.dry:
        raise HTTPException(400, "Informe 'message' (ou 'input') ou 'args'.")

    # Resolve o subgrafo VIVO do pipeline (raiz + membros) — reusa o builder do
    # snapshot do catálogo (mesma lógica: membership + arestas intra-pipeline + raiz).
    from app.catalog.pipeline_defs import _build_subgraph
    sub = await _build_subgraph(pid)
    root = sub.get("root_agent_id")
    members = {n.get("id") for n in sub.get("nodes", []) if n.get("id")}
    if not root:
        raise HTTPException(422, "Pipeline sem agentes/raiz resolvível — nada a executar.")

    # Modo `dry`: RESOLVE os args (coage/defaults/valida) e devolve o payload
    # resolvido + proveniência SEM executar (não gasta LLM). 422 se inválidos.
    if data.dry:
        return await _plan_args(pid, p, root, data.args or {})

    # Args estruturados (D1/D2): `args` presente (mesmo {}) engaja o contrato —
    # coage/aplica defaults/valida contra o ## Inputs do agente-raiz (422 nomeando
    # cada campo) e SEPARA em prosa + envelope `param` selado. `args` omitido (None)
    # = texto livre puro.
    sealed_inputs = None
    if data.args is not None:
        user_input, sealed_inputs = await _validate_and_fold_args(pid, p, root, user_input, data.args)

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
            sealed_inputs=sealed_inputs or None,  # envelope param (out-of-band)
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
            # Governança: registra QUAIS args foram passados (só as chaves, não os
            # valores — evita vazar PII no log de auditoria).
            "arg_keys": sorted(data.args.keys()) if isinstance(data.args, dict) else [],
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
    verbosity: Optional[str] = Query(
        None, description="full | summary | minimal — projeta o pipeline_done final."
    ),
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
    # `is None` (não `not data.args`): `args:{}` engaja o contrato — paridade com o
    # /invoke sync e com o gatilho de fold abaixo.
    if not user_input and data.args is None:
        raise HTTPException(400, "Informe 'message' (ou 'input') ou 'args'.")

    from app.catalog.pipeline_defs import _build_subgraph
    sub = await _build_subgraph(pid)
    root = sub.get("root_agent_id")
    members = {n.get("id") for n in sub.get("nodes", []) if n.get("id")}
    if not root:
        raise HTTPException(422, "Pipeline sem agentes/raiz resolvível — nada a executar.")

    # Args estruturados (D1/D2): mesma validação/coerção/defaults do /invoke sync,
    # separando o envelope `param` selado da prosa.
    sealed_inputs = None
    if data.args is not None:
        user_input, sealed_inputs = await _validate_and_fold_args(pid, p, root, user_input, data.args)

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

    # Verbosidade: o pipeline_done final é PROJETADO igual ao /invoke sync (sessão→full;
    # X-API-Key→summary), pra que a console "ver como integração" não minta. Os eventos
    # intermediários (agent_*) são sempre crus (são o passo-a-passo, não o contrato).
    api_key_id = getattr(request.state, "api_key_id", None)
    explicit = verbosity or data.verbosity
    api_default = "summary"
    if api_key_id:
        api_default = await settings_store.get("api_invoke_default_verbosity", "summary")
    effective = resolve_verbosity(explicit, is_api_key=bool(api_key_id), api_default=api_default)

    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def _cb(event: dict) -> None:
        if isinstance(event, dict) and event.get("type") == "pipeline_done" and event.get("result"):
            res = event["result"]
            payload = {
                "pipeline_id": pid,
                "status": res.get("status", "completed"),
                "output": res.get("output", ""),
                "final_state": res.get("final_state"),
                "interaction_id": res.get("interaction_id"),
                "total_agents": res.get("total_agents", 0),
                "completed_agents": res.get("completed_agents", 0),
                "pipeline_steps": res.get("pipeline_steps", []),
                "duration_ms": res.get("duration_ms"),
            }
            event = {**event, "result": project_pipeline_result(payload, effective)}
        await queue.put(event)

    async def _run():
        from app.agents.engine import execute_pipeline
        try:
            result = await execute_pipeline(
                entry_agent_id=root,
                user_input=user_input,
                channel=data.channel or "api",
                session_id=data.session_id,
                attachments=pipeline_attachments or None,
                allowed_agent_ids=members,  # SELA ao subgrafo do pipeline
                sealed_inputs=sealed_inputs or None,  # envelope param (out-of-band)
                progress_callback=_cb,
            )
            # Auditoria (paridade com o /invoke sync — este é o caminho da UI/Playground):
            # registra a invocação + arg_keys. Envolta em try/except: auditoria NUNCA
            # pode derrubar o stream do usuário.
            try:
                await audit_repo.create({
                    "entity_type": "pipeline",
                    "entity_id": pid,
                    "action": "invoked",
                    "details": json.dumps({
                        "root_agent_id": root,
                        "member_count": len(members),
                        "completed_agents": (result or {}).get("completed_agents", 0),
                        "interaction_id": (result or {}).get("interaction_id"),
                        "actor_user_id": user.get("id"),
                        "via": "api_key" if api_key_id else "session",
                        "api_key_id": api_key_id,
                        "stream": True,
                        "arg_keys": sorted(data.args.keys()) if isinstance(data.args, dict) else [],
                    }, ensure_ascii=False),
                })
            except Exception:
                pass
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
