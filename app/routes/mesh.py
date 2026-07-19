"""Mesh + CAR — topologia e catálogo de roteadores §6."""
import uuid, json, logging
from fastapi import APIRouter, HTTPException, Depends, Request
from app.models.schemas import MeshConnectionCreate, CAREntryCreate
from app.core.database import mesh_repo, agents_repo, car_repo
from app.core.auth import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/mesh", tags=["mesh"])

# Tipos de conexão CANÔNICOS aceitos no mesh. Os 3 primeiros já apareciam na UI;
# `default` (else do fan-out 1-de-N) existe e é honrado no engine
# (engine.py::_is_default_branch) mas até então não era criável pela UI — o
# Fluxograma de agentes passa a expô-lo. A coluna mesh_connections.connection_type
# não tem CHECK no DB; esta validação de rota impede tipos inválidos por API.
_VALID_CONNECTION_TYPES = {"sequential", "parallel", "conditional", "default"}

def _fanout_roots(edges: list[dict]) -> list[str]:
    """IDs de origens com ≥2 arestas ``conditional`` de saída (fan-out 1-de-N).

    Sinaliza onde o operador pode ter cabeado destinos em PARALELO (irmãos do
    roteador) quando a intenção era uma CADEIA — um destino consome o resultado
    de outro (ex.: Tavily busca a partir do endereço que o Busca endereço
    resolveu). Genérico e SEM falso-positivo: só conta o padrão, não tenta
    adivinhar a dependência semântica. Consumido por `get_topology` → a UI
    (`mesh.html`) mostra um aviso fan-out × cadeia no cabeçalho do pipeline.
    """
    counts: dict[str, int] = {}
    for e in edges:
        if e.get("type") == "conditional":
            counts[e.get("source")] = counts.get(e.get("source"), 0) + 1
    return [src for src, n in counts.items() if n >= 2]


def _conditional_without_expr(edges: list[dict]) -> list[str]:
    """IDs de arestas ``conditional`` SEM regra (``expr`` vazia/ausente).

    No engine, uma condicional sem ``expr`` é tratada como "sempre passa"
    (idêntica a ``sequential``, ver engine.py::_should_skip_conditional) — a
    pegadinha que faz um roteador com fan-out rodar TODOS os destinos em vez de
    escolher 1-de-N. `get_topology` expõe isto pra UI avisar o operador a
    preencher a regra antes de publicar. Sem falso-positivo: só sinaliza o
    padrão (tipo condicional + expr em branco), não julga a intenção.
    """
    flagged: list[str] = []
    for e in edges:
        if e.get("type") != "conditional":
            continue
        cfg = e.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg) or {}
            except Exception:
                cfg = {}
        if not str((cfg or {}).get("expr") or "").strip():
            flagged.append(e.get("id"))
    return flagged


def _fanout_missing_default(edges: list[dict]) -> list[str]:
    """IDs de origens em fan-out condicional (≥2 arestas ``conditional``) que NÃO
    têm uma aresta ``default`` de saída.

    Sem a rota default·else, se a resposta do roteador não casar com NENHUMA
    ``expr``, não há fallback — o pipeline vira dead-end naquele ramo. Consumido
    por `get_topology` → o Fluxograma avisa no painel do nó (o selo ⚠ fan-out
    sozinho não distingue "tem default" de "não tem"). Sem falso-positivo: só o
    padrão estrutural (fan-out condicional sem aresta default), não julga intenção.
    """
    fanout = set(_fanout_roots(edges))
    has_default = {e.get("source") for e in edges if e.get("type") == "default"}
    return [src for src in fanout if src not in has_default]


def _mixed_inbound(edges: list[dict]) -> list[str]:
    """IDs de alvos com entrada MISTA: ≥1 aresta ``conditional`` E ≥1 aresta
    ``sequential``/``parallel`` chegando no MESMO nó.

    Semântica no engine (29.1.13, achados N1 "Hélios"/N2 "Arca"): o nó roda se
    QUALQUER aresta inbound disparar — a cadeia sequencial de um nó que EXECUTOU
    reativa o alvo mesmo quando a condicional não casou. É um padrão legítimo
    ("roda quando roteado OU após o nó X"), mas sutil: quem queria "só roda
    quando a condição casar" precisa remover a aresta sequencial. `get_topology`
    expõe isto pra UI informar o operador no painel do nó (estilo F5/fan-out).
    Sem falso-positivo: só o padrão estrutural, não julga intenção.
    """
    cond_targets: set = set()
    chain_targets: set = set()
    for e in edges:
        t = e.get("target")
        ty = e.get("type")
        if ty == "conditional":
            cond_targets.add(t)
        elif ty in ("sequential", "parallel"):
            chain_targets.add(t)
    return [t for t in cond_targets & chain_targets if t]


def _router_nonisolated_inbound(nodes: list[dict], edges: list[dict]) -> list[str]:
    """IDs de agentes ``kind=router`` que recebem uma aresta de ENTRADA em cadeia
    (``sequential``/``parallel``) cujo ``context_scope`` NÃO é ``isolated``.

    Um roteador classifica a INTENÇÃO ORIGINAL do usuário. Numa cadeia
    não-isolada o downstream recebe o OUTPUT do agente anterior PREFIXADO à
    mensagem (engine.py::execute_pipeline via `_resolve_context_scope`) — o
    roteador acaba classificando o texto do upstream (ex.: um orquestrador),
    não o do cliente, e desrote a intenção. Achado #4 do QA VPS 2026-07-19: com o mesh
    `Maestro(aobd)→Triagem(router)` em `sequential`, "quero devolver o tênis por
    arrependimento" (claramente comprador) caiu em `fora_de_escopo`; setar
    `context_scope: isolated` na aresta corrigiu (o roteador voltou a ver só a
    mensagem original). Fix do operador: `context_scope: isolated` na aresta de
    entrada do roteador, ou torná-lo a ENTRADA do pipeline (router-first).

    `get_topology` expõe isto pra a UI avisar no painel do nó. Sem falso-positivo:
    só o padrão estrutural (roteador + inbound em cadeia sem `isolated`); um
    roteador que é ENTRADA (sem inbound em cadeia) ou já `isolated` não aparece.
    Espelha a leitura do engine: só `mode=="isolated"` isola; ausente/inherit/
    scoped poluem.
    """
    router_ids = {n.get("id") for n in nodes if n.get("kind") == "router"}
    flagged: list[str] = []
    seen: set = set()
    for e in edges:
        tgt = e.get("target")
        if tgt not in router_ids or tgt in seen:
            continue
        if e.get("type") not in ("sequential", "parallel"):
            continue
        cfg = e.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg) or {}
            except Exception:
                cfg = {}
        scope_cfg = (cfg or {}).get("context_scope")
        mode = (scope_cfg.get("mode") if isinstance(scope_cfg, dict) else None) or "inherit"
        if str(mode).strip().lower() != "isolated":
            seen.add(tgt)
            flagged.append(tgt)
    return flagged


def _detect_roots(edges: list[dict]) -> list[str]:
    """Raízes do mesh = sources que NUNCA são target (entrada de uma cadeia).

    FONTE ÚNICA da detecção de raiz (PR3): `mesh.html` e `workspace.html`
    consomem isto via `/topology` em vez de recomputar client-side (fim da
    triplicação). Preserva a ordem de aparição dos sources. Fallback: se não
    houver nenhuma raiz (mesh em ciclo puro), devolve todos os sources distintos
    — mesmo comportamento que o `hierarchicalEdges` do mesh.html já tinha.
    """
    sources: list[str] = []
    seen: set = set()
    targets: set = set()
    for e in edges:
        s = e.get("source")
        if s and s not in seen:
            seen.add(s)
            sources.append(s)
        t = e.get("target")
        if t:
            targets.add(t)
    roots = [s for s in sources if s not in targets]
    return roots if roots else sources


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
            # `config` exposto para o frontend conseguir popular o form de
            # edição (especialmente expr de conexões conditional).
            edges.append({
                "id": c["id"],
                "source": src,
                "target": tgt,
                "type": c["connection_type"],
                "config": c.get("config") or "{}",
            })
        elif src not in {a["id"] for a in agents} or tgt not in {a["id"] for a in agents}:
            # Auto-cleanup: conexão órfã → agente deletado (não apenas inativo)
            try:
                await mesh_repo.delete(c["id"])
            except Exception:
                pass
    # PR3 — enriquecimento aditivo: `roots` (fonte única da detecção de raiz) e
    # `pipeline_id` por nó (membership). A UI agrupa/rotula por pipeline-entidade
    # no lugar dos `mesh_chain_names` soltos. Defensivo: membership é display-only;
    # se falhar, segue sem pipeline_id (nunca derruba a topologia).
    membership: dict = {}
    try:
        from app.core.database import pipeline_membership
        for m in await pipeline_membership.all():
            membership[m["agent_id"]] = m["pipeline_id"]
    except Exception:
        membership = {}
    for n in nodes:
        n["pipeline_id"] = membership.get(n["id"])
    return {
        "nodes": nodes,
        "edges": edges,
        "fanout_roots": _fanout_roots(edges),
        "conditional_no_expr": _conditional_without_expr(edges),
        "fanout_missing_default": _fanout_missing_default(edges),
        "mixed_inbound": _mixed_inbound(edges),
        "router_nonisolated_inbound": _router_nonisolated_inbound(nodes, edges),
        "roots": _detect_roots(edges),
    }

@router.post("/connections", status_code=201)
async def create_connection(data: MeshConnectionCreate):
    if data.connection_type not in _VALID_CONNECTION_TYPES:
        raise HTTPException(422, f"connection_type inválido: {data.connection_type!r}. Use um de: {', '.join(sorted(_VALID_CONNECTION_TYPES))}.")
    if data.source_agent_id == data.target_agent_id:
        raise HTTPException(422, "Origem e destino não podem ser o mesmo agente.")
    if not await agents_repo.find_by_id(data.source_agent_id) or not await agents_repo.find_by_id(data.target_agent_id):
        raise HTTPException(404, "Agente não encontrado")
    cid = str(uuid.uuid4())
    await mesh_repo.create({"id":cid,"source_agent_id":data.source_agent_id,"target_agent_id":data.target_agent_id,"connection_type":data.connection_type,"config":data.config or "{}"})
    return {"id": cid, "message": "Conexão criada"}

@router.put("/connections/{conn_id}")
async def update_connection(conn_id: str, data: MeshConnectionCreate):
    existing = await mesh_repo.find_by_id(conn_id)
    if not existing: raise HTTPException(404)
    if data.connection_type and data.connection_type not in _VALID_CONNECTION_TYPES:
        raise HTTPException(422, f"connection_type inválido: {data.connection_type!r}. Use um de: {', '.join(sorted(_VALID_CONNECTION_TYPES))}.")
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    return await mesh_repo.update(conn_id, upd)

@router.delete("/connections/{conn_id}")
async def delete_connection(conn_id: str):
    if not await mesh_repo.delete(conn_id): raise HTTPException(404)
    return {"message": "Conexão removida"}


# ═══════════════════════════════════════════════════════════════════
# Fluxograma de agentes (2026-06-12) — LAYOUT posicional (x,y) dos nós.
#
# UI-ONLY: vive em platform_settings sob a chave `mesh_node_positions`
# (MESMO store de mesh_groups/mesh_chain_names), NUNCA em
# mesh_connections.config — que é LIDO pelo engine em runtime
# (_should_skip_conditional / _resolve_context_scope). Apagar o layout
# NÃO altera a execução: as duas views (Topologia e Fluxograma) leem o
# MESMO grafo de mesh_connections; o x,y é só onde o Fluxograma desenha.
#
# Por que um endpoint DEDICADO (e não PUT /api/v1/settings): o save_settings
# re-serializa o modelo SettingsSave inteiro com defaults → salvar por ali a
# cada drag sobrescreveria mcp_per_tool_enabled, grounding_strict, chaves de
# LLM, etc. Aqui o set() faz upsert SÓ desta chave (ON CONFLICT por key).
# ═══════════════════════════════════════════════════════════════════

_MESH_POSITIONS_KEY = "mesh_node_positions"


@router.get("/layout")
async def get_layout():
    """Posições x,y dos nós do Fluxograma. Vazio ({}) se nunca salvo."""
    from app.core.database import settings_store
    raw = await settings_store.get(_MESH_POSITIONS_KEY, "")
    positions: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                positions = parsed
        except (ValueError, TypeError):
            positions = {}
    return {"positions": positions}


@router.put("/layout")
async def save_layout(payload: dict):
    """Persiste SÓ a chave mesh_node_positions (upsert por-chave) — NÃO toca
    nas demais settings. Sanitiza para {agent_id: {x: float, y: float}};
    descarta entradas malformadas (e bool, que é subclasse de int)."""
    from app.core.database import settings_store
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        raise HTTPException(422, "payload.positions deve ser um objeto {agent_id: {x, y}}")

    def _num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    clean: dict = {}
    for aid, pos in positions.items():
        if isinstance(pos, dict) and _num(pos.get("x")) and _num(pos.get("y")):
            clean[str(aid)] = {"x": round(float(pos["x"]), 1), "y": round(float(pos["y"]), 1)}
    await settings_store.set(_MESH_POSITIONS_KEY, json.dumps(clean))
    return {"message": "Layout salvo", "count": len(clean)}


# ═══════════════════════════════════════════════════════════════════
# FSM canônica resolvida por agente (PR3 — Fluxograma "abrir nó → FSM").
#
# A regra exec_mode→fases está hardcoded no engine (state_machine.py + os
# perfis em engine.py); este endpoint é a fonte ÚNICA consultável dela, para
# o Fluxograma RENDERIZAR sem duplicar a regra no JS (evita drift client×engine).
# Fases canônicas e ramos vêm de state_machine.py::State/TRANSITIONS; os perfis
# (fast/standard/rigorous/declarative) e o efeito de require_evidence=0 vêm da
# orquestração do engine.
# ═══════════════════════════════════════════════════════════════════

_FSM_PROFILE_NOTE = {
    "declarative": "HTTP via API Bindings / Data Tables — não percorre a FSM de LLM.",
    "fast": "1 chamada LLM · sem reflexão · verificação heurística (max_iter=1).",
    "standard": "reflexão adaptativa · verificação heurística (max_iter=2).",
    "rigorous": "reflexão + verificação por LLM (judge) (max_iter=3).",
}

_FSM_LEAVES = [
    {"label": "Recommend", "cond": "evidence_ok"},
    {"label": "Refuse", "cond": "evidence_insufficient"},
    {"label": "Escalate", "cond": "risk_or_fraud"},
]


def _build_fsm_profile(execution_mode, require_evidence) -> dict:
    """PURA: mapeia execution_mode (+ require_evidence) para a trilha canônica
    da FSM. Caminho feliz de fases; ramo policy_denied em PolicyCheck; folhas
    terminais Recommend/Refuse/Escalate (1-de-3, decididas em VerifyEvidence)
    convergindo em LogAndClose. Sem skill resolvido → perfil 'unresolved'."""
    mode = execution_mode if execution_mode in ("declarative", "fast", "standard", "rigorous") else None
    if mode is None:
        return {"execution_mode": None, "profile_label": "—",
                "note": "Sem skill — perfil de execução não resolvido.", "phases": [], "leaves": []}

    if mode == "declarative":
        return {
            "execution_mode": "declarative", "profile_label": "Declarativo",
            "note": _FSM_PROFILE_NOTE["declarative"],
            "phases": [
                {"id": "Intake", "label": "Intake", "desc": "recebe e normaliza", "state": "always"},
                {"id": "PolicyCheck", "label": "PolicyCheck", "desc": "permissões (OPA)", "state": "always", "branch": "policy_denied → Refuse"},
                {"id": "Declarative", "label": "Execução declarativa", "desc": "API Bindings / Data Tables — sem LLM", "state": "normal"},
                {"id": "LogAndClose", "label": "LogAndClose", "desc": "registra e fecha", "state": "always", "terminal": True},
            ],
            "leaves": [],
        }

    skip_ev = (require_evidence == 0)
    reflect = mode in ("standard", "rigorous")
    verify_llm = (mode == "rigorous")
    phases = [
        {"id": "Intake", "label": "Intake", "desc": "recebe e normaliza", "state": "always"},
        {"id": "PolicyCheck", "label": "PolicyCheck", "desc": "permissões (OPA)", "state": "always", "branch": "policy_denied → Refuse"},
        {"id": "RetrieveEvidence", "label": "RetrieveEvidence", "desc": "busca evidências (RAG)",
         "state": "skipped" if skip_ev else "normal", "note": "pulada · require_evidence=0" if skip_ev else ""},
        {"id": "DraftAnswer", "label": "DraftAnswer", "desc": "gera rascunho (LLM)", "state": "normal",
         "note": "+ reflexão" if reflect else ""},
        {"id": "VerifyEvidence", "label": "VerifyEvidence", "desc": "verifica consistência e cobertura",
         "state": "skipped" if skip_ev else "normal",
         "note": ("pulada · require_evidence=0" if skip_ev else ("por LLM (judge)" if verify_llm else "heurística"))},
        {"id": "LogAndClose", "label": "LogAndClose", "desc": "registra e fecha", "state": "always", "terminal": True},
    ]
    return {
        "execution_mode": mode, "profile_label": mode.capitalize(),
        "note": _FSM_PROFILE_NOTE[mode], "phases": phases, "leaves": list(_FSM_LEAVES),
    }


@router.get("/fsm/{agent_id}")
async def get_agent_fsm(agent_id: str):
    """FSM canônica resolvida do agente (Fluxograma 'abrir nó'). Resolve
    execution_mode pelo skill (mesma lógica de /agents/{id}/inputs-schema) e
    require_evidence pela linha do agente. Fonte ÚNICA — o JS apenas renderiza."""
    from app.skill_parser.parser import parse_skill_md
    from app.core.database import skills_repo
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{agent_id}' não encontrado")
    execution_mode = None
    if agent.get("skill_id"):
        skill_row = await skills_repo.find_by_id(agent["skill_id"])
        if skill_row and skill_row.get("raw_content"):
            try:
                execution_mode = parse_skill_md(skill_row["raw_content"]).execution_mode
            except Exception:
                execution_mode = None
    req_ev = agent.get("require_evidence")
    try:
        req_ev = int(req_ev) if req_ev is not None else None
    except (ValueError, TypeError):
        req_ev = None
    prof = _build_fsm_profile(execution_mode, req_ev)
    prof["agent_id"] = agent_id
    prof["require_evidence"] = req_ev
    return prof


# ═══════════════════════════════════════════════════════════════════
# Replay "Última execução" (PR4 — Fluxograma).
#
# O trace de um pipeline é PERSISTIDO em interactions.trace_data (JSON), gravado
# por execute_pipeline. Aqui devolvemos um shape ENXUTO e canvas-ready da execução
# de pipeline MAIS RECENTE: cada step keyed por agent_id (= id do nó), com status
# (ran/skipped), skip_reason + diagnóstico humano, e final_state (a folha da FSM).
# O canvas pinta nós (ran/skipped/erro) e arestas (disparou sse o ALVO rodou).
# ═══════════════════════════════════════════════════════════════════


def _extract_step_diag(step: dict) -> str:
    tr = step.get("trace")
    if isinstance(tr, dict):
        ds = tr.get("diagnostics")
        if isinstance(ds, list) and ds and isinstance(ds[0], dict):
            return str(ds[0].get("text") or "")
    return ""


@router.get("/last-run")
async def get_last_run():
    """Trace canvas-ready da execução de PIPELINE mais recente. Varre as
    interactions (DESC por created_at) e devolve a primeira com pipeline_steps
    não-vazio. `found=False` se nenhuma execução replayável existir."""
    from app.core.database import interactions_repo
    rows = await interactions_repo.find_all(limit=40)
    for itx in (rows or []):
        raw = itx.get("trace_data") or ""
        try:
            td = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(td, dict):
            continue
        steps = td.get("pipeline_steps")
        if not isinstance(steps, list) or not steps:
            continue
        out_steps = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            out_steps.append({
                "agent_id": s.get("agent_id"),
                "agent_name": s.get("agent_name"),
                "status": s.get("status"),
                "skip_reason": s.get("skip_reason"),
                "final_state": s.get("final_state"),
                "duration_ms": s.get("duration_ms"),
                "diagnostic": _extract_step_diag(s),
            })
        return {
            "found": True,
            "session_id": itx.get("id"),
            "title": itx.get("title"),
            "created_at": str(itx.get("created_at")) if itx.get("created_at") is not None else None,
            "final_state": td.get("final_state"),
            "entry_agent_id": td.get("agent_id"),
            "steps": out_steps,
        }
    return {"found": False, "steps": []}


@router.get("/groups")
async def get_groups():
    """Grupos do AI Mesh (chave `mesh_groups` em platform_settings — MESMA fonte
    da Topologia). O Fluxograma usa para tingir/rotular nós por grupo. Parse
    defensivo: descarta entradas sem id/name; `[]` em vazio/JSON inválido."""
    from app.core.database import settings_store
    raw = await settings_store.get("mesh_groups", "")
    groups: list = []
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            for g in parsed:
                if not isinstance(g, dict):
                    continue
                gid, name = g.get("id"), g.get("name")
                if not gid or not name:
                    continue
                aids = g.get("agent_ids")
                groups.append({
                    "id": str(gid),
                    "name": str(name),
                    "color": str(g.get("color") or "teal"),
                    "agent_ids": [str(a) for a in aids if a] if isinstance(aids, list) else [],
                })
    return {"groups": groups}


# ═══════════════════════════════════════════════════════════════════
# Conditional Routing Wizard (2026-06-01) — endpoint para o frontend
# avaliar uma expressão Jinja contra um contexto simulado, sem precisar
# salvar a conexão nem disparar um pipeline.
# ═══════════════════════════════════════════════════════════════════


@router.get("/conditional-vars")
async def conditional_vars():
    """Lista as variáveis disponíveis em expressões conditional, com
    descrição + tipo. Usado pelo wizard de Edição de Conexão (vars panel).
    """
    from app.agents.engine import CONDITIONAL_VARS_META
    return {"vars": CONDITIONAL_VARS_META}


@router.get("/agents/{agent_id}/decisions")
async def agent_decisions(agent_id: str):
    """Contrato de Decisão declarado pela skill do agente (Cond-C, 35.19.0).

    Retorna `{campo: [valores]}` da seção `## Decisions` da SKILL.md vinculada,
    ou `{}` quando o agente não declara contrato (sem skill / sem seção /
    malformada). Consumido pelo card "Decisão do agente" do editor de conexão:
    a UI oferece os campos/valores DECLARADOS do agente de ORIGEM da aresta em
    vez de o operador digitar de memória (o fim do 'escalar=sim' por telepatia).
    """
    from app.core.database import skills_repo
    from app.skill_parser.decisions_schema import extract_decisions_schema

    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise HTTPException(404, "Agente não encontrado")
    schema = None
    if agent.get("skill_id"):
        row = await skills_repo.find_by_id(agent["skill_id"])
        schema = extract_decisions_schema((row or {}).get("raw_content") or "")
    return {"agent_id": agent_id, "decisions": schema or {}}


@router.post("/connections/test-conditional")
async def test_conditional(payload: dict):
    """Avalia uma expressão Jinja boolean contra um contexto de exemplo.
    Usado pelo simulador do wizard antes do operador salvar.

    Payload: {
        "expr": str,
        "output": str (opcional)          — resposta simulada do upstream,
        "final_state": str (opcional)     — Recommend/Refuse/Escalate/LogAndClose,
        "input": str (opcional)           — pergunta original simulada do usuário,
        "attachments": list (opcional)    — [{"name","type"}] de anexos simulados,
        "session_text": str (opcional)    — perguntas recentes (memória de sessão),
        "inputs": dict (opcional)         — args selados (x-uso:param) p/ regras inputs.X,
        "decision": dict (opcional)       — decisões anunciadas simuladas p/ regras decision.X,
        "source_agent_id": str (opcional) — agente de ORIGEM da aresta: sem `decision`
                                            explícito, extrai a linha DECISAO do `output`
                                            simulado e valida contra o contrato da skill
                                            dele (espelha o runtime)
    }
    Returns: {"result": bool, "context": dict} OU {"error": str}

    Por que aceitar input/attachments/final_state (2026-06-18): o simulador
    antigo só passava `output` e fixava `final_state="Recommend"` no front, então
    QUALQUER regra sobre a pergunta (`input_lower`), anexos (`has_document`) ou
    decisão (`is_refuse`/`is_escalate`) simulava SEMPRE "não casa" mesmo correta —
    fabricando a confusão que o simulador deveria remover. Agora o contexto de
    teste casa o que `_build_conditional_context` monta em runtime.

    Política: fail-CLOSED — erro vira mensagem para o operador corrigir
    a expressão antes de salvar. Em runtime (`_should_skip_conditional`)
    o erro é fail-OPEN porque é melhor executar do que perder dado; aqui
    o operador QUER ver o erro para corrigir.
    """
    expr = (payload.get("expr") or "").strip()
    if not expr:
        return {"error": "Expressão vazia — sem regra para avaliar."}

    from app.agents.engine import _build_conditional_context, _eval_conditional

    atts = payload.get("attachments")
    _inputs = payload.get("inputs")
    # Contrato de Decisão (Cond-C, 35.19.0): decision explícito simula direto;
    # sem ele, com source_agent_id, espelha o runtime — extrai a linha DECISAO
    # do output simulado e valida contra o ## Decisions da skill do source.
    _decision = payload.get("decision")
    if not isinstance(_decision, dict):
        _decision = None
    _src = str(payload.get("source_agent_id") or "").strip()
    if _decision is None and _src:
        from app.agents.engine import _decision_vars_for_source
        _decision = await _decision_vars_for_source(_src, payload.get("output", "") or "")
    ctx = _build_conditional_context(
        output=payload.get("output", ""),
        final_state=payload.get("final_state", ""),
        user_input=payload.get("input", ""),
        attachments=atts if isinstance(atts, list) else [],
        session_text=payload.get("session_text", ""),
        # Postura B: permite simular regras sobre `inputs.<campo>` (args selados).
        inputs=_inputs if isinstance(_inputs, dict) else {},
        decision=_decision or {},
    )
    try:
        result = _eval_conditional(expr, ctx)
    except Exception as e:
        return {
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "context": ctx,
        }
    return {"result": bool(result), "context": ctx}


@router.post("/connections/suggest-conditional")
async def suggest_conditional(payload: dict, request: Request,
                              user: dict = Depends(require_user)):
    """Tradutor NL→Jinja (Fatia 4): descrição em pt-BR → regra condicional.

    Payload: {"description": str}
    Returns: {"expr": str, "valid": bool, "used_vars": [...], "unknown_vars": [...], "error": str}

    DNA "IA sugere → sistema PROVA": o LLM propõe uma expressão usando só as
    variáveis canônicas e o backend RECONCILIA contra `CONDITIONAL_VARS_META`
    (envolvendo em `{{ }}` antes de parsear — senão o guardrail vira selo
    sempre-verde). NÃO persiste: o operador revisa e salva via /connections.

    Auth: superfície de UI — principal via X-API-Key recebe 403 acionável
    (gêmeo do guard do suggest-args, 38.0.0: require_user aceita keys; sem o
    guard qualquer portador de key queimava LLM — rota instruct — sem limite
    por aqui). Integração não precisa do tradutor: escreve `config.expr`
    pronto via /connections.
    """
    if getattr(request.state, "api_key_id", None):
        raise HTTPException(403, {
            "error": "suggest_conditional_ui_only",
            "message": "Este endpoint é da superfície de UI (sessão). "
                       "Integrações gravam a regra pronta em config.expr "
                       "via POST/PUT /api/v1/mesh/connections.",
        })
    description = (payload.get("description") or "").strip()
    if not description:
        return {"error": "Descreva a regra em português (ex.: 'se mencionar pix ou anexar documento')."}

    from app.agents.engine import (
        CONDITIONAL_VARS_META, _eval_conditional, _build_conditional_context,
    )
    from app.agents.conditional_suggest import (
        build_suggest_messages, extract_expression, validate_conditional_expression,
        repair_unquoted_literals, normalize_norm_literals,
    )
    from app.llm_routing import resolve_llm_for_task
    from app.routes.wizard import _wizard_llm_complete

    # Contrato de Decisão (Cond-C, 35.19.0): com source_agent_id no payload e
    # contrato declarado na skill do source, o tradutor passa a CONHECER os
    # campos/valores reais — "quando escalar for sim" vira
    # `decision.escalar == 'sim'` em vez de um chute sobre output_lower.
    vars_meta = CONDITIONAL_VARS_META
    _src = str(payload.get("source_agent_id") or "").strip()
    if _src:
        try:
            from app.core.database import skills_repo
            from app.skill_parser.decisions_schema import extract_decisions_schema
            _agent = await agents_repo.find_by_id(_src)
            _row = (
                await skills_repo.find_by_id(_agent["skill_id"])
                if _agent and _agent.get("skill_id") else None
            )
            _schema = extract_decisions_schema((_row or {}).get("raw_content") or "")
            if _schema:
                vars_meta = [dict(v) for v in CONDITIONAL_VARS_META]
                _pairs = "; ".join(
                    f"decision.{k} aceita: " + " | ".join(vals)
                    for k, vals in _schema.items()
                )
                _k0, _v0 = next(iter(_schema.items()))
                for v in vars_meta:
                    if v["name"] == "decision":
                        v["desc"] += (
                            f" CONTRATO DECLARADO pelo agente de origem DESTA conexão: {_pairs}. "
                            f"Prefira decision.<campo> == '<valor>' quando a descrição "
                            f"citar um desses campos — ex.: decision.{_k0} == '{_v0[0]}'."
                        )
        except Exception:
            logger.warning("suggest-conditional: contrato do source ilegível", exc_info=True)

    provider, model = await resolve_llm_for_task("instruct")
    messages = build_suggest_messages(description, vars_meta)
    try:
        content, _, _ = await _wizard_llm_complete(
            messages, provider, model, route="conditional_suggest", temperature=0,
        )
    except HTTPException:
        raise  # 503 acionável (LLM inacessível) propaga
    except Exception as e:
        logger.error("suggest-conditional falhou", exc_info=True)
        raise HTTPException(500, f"Erro ao gerar a regra: {e}")

    expr = extract_expression(content)
    canonical = {v["name"] for v in CONDITIONAL_VARS_META}
    # Auto-conserta o erro comum do LLM: `pix in input_lower` → `'pix' in input_lower`
    # (literal sem aspas). Sem isso o guardrail rejeitaria uma regra que o usuário
    # claramente quis. Idempotente; literais já com aspas não mudam.
    expr = repair_unquoted_literals(expr, canonical)
    # Gêmeo do #617 (review 2026-07-15): literal acentuado contra var *_norm
    # seria regra sempre-falsa (a var é normalizada no runtime) — normaliza o
    # literal deterministicamente ANTES de validar/mostrar ao operador.
    expr = normalize_norm_literals(expr)
    result = validate_conditional_expression(expr, canonical)
    # Smoke de runtime: a regra válida tem que AVALIAR sem crash (sandbox,
    # contexto vazio) — pega erro de tipo/método que o set-diff não vê.
    if result["valid"]:
        try:
            _eval_conditional(expr, _build_conditional_context())
        except Exception as e:
            result = {**result, "valid": False, "error": f"A regra gerada não avalia: {e}"}
    return {"expr": expr, "description": description, **result}


# ═══════════════════════════════════════════════════════════════════
# Context Scope Wizard (2026-06-01) — endpoints para o frontend
# escolher a política de propagação de contexto (inherit/scoped/isolated)
# entre nós da mesh chain. Complementar ao Conditional Routing — ver
# `_resolve_context_scope` em app/agents/engine.py.
# ═══════════════════════════════════════════════════════════════════


# Metadata de ajuda dos TIPOS DE CONEXÃO — fonte ÚNICA para o popover `?` ao
# lado de cada card no wizard (mesh.html). No backend (não hardcoded no template)
# pra não dar drift. Mesma filosofia de CONTEXT_SCOPE_VARS_META / os `desc` dos
# modos de escopo. `what` = "o que é", `when` = "quando usar".
MESH_CONNECTION_TYPES_HELP: list[dict] = [
    {
        "id": "sequential",
        "label": "Sequencial",
        "what": "O destino roda SEMPRE depois da origem e recebe o output dela como contexto.",
        "when": "Encadear etapas onde uma alimenta a outra — A produz, B consome (ex.: Busca endereço → Tavily busca a partir do endereço resolvido).",
    },
    {
        "id": "parallel",
        "label": "Sempre dispara",
        "what": "Vários destinos da mesma origem rodam TODOS, com o mesmo input, um após o outro em sequência (sem o roteador escolher um; a execução não é simultânea).",
        "when": "Quando você quer respostas independentes de cada destino para combinar depois — não é roteamento 1-de-N.",
    },
    {
        "id": "conditional",
        "label": "Condicional",
        "what": "O destino roda só se a regra (expressão) casar contra a entrada/output do upstream.",
        "when": "Roteamento 1-de-N: o roteador escolhe UM destino conforme a mensagem. Combine com um destino 'default' como else (se nenhum casar).",
    },
]


@router.get("/connection-types")
async def connection_types():
    """Metadata de ajuda dos tipos de conexão (o que é / quando usar) — fonte
    única para o popover `?` ao lado de cada card no wizard de conexão. Estática;
    espelha o padrão de `/context-scope-vars`.
    """
    return {"types": MESH_CONNECTION_TYPES_HELP}


@router.get("/context-scope-vars")
async def context_scope_vars():
    """Lista as variáveis disponíveis em templates Jinja do modo `scoped`
    + os modos suportados. Usado pelo wizard de Edição de Conexão (vars
    panel + seletor de modo).

    As vars são as MESMAS do conditional routing — operador aprende uma
    vez, usa nos dois lugares. Ver `_build_conditional_context`.
    """
    from app.agents.engine import (
        CONTEXT_SCOPE_VARS_META, CONTEXT_SCOPE_MODES,
    )
    return {
        "vars": CONTEXT_SCOPE_VARS_META,
        "modes": [
            {
                "id": "inherit",
                "label": "Herdar (padrão)",
                "desc": "Output completo do agente anterior vira contexto do próximo — comportamento histórico, retrocompatível.",
            },
            {
                "id": "scoped",
                "label": "Filtrar (scoped)",
                "desc": "Output passa por transformação Jinja antes de virar contexto. Economiza tokens e permite extrair só a parte relevante.",
            },
            {
                "id": "isolated",
                "label": "Isolar",
                "desc": "Próximo agente NÃO recebe contexto do anterior — apenas a solicitação original. Útil para subagentes atômicos.",
            },
        ],
        "_modes_canonical": list(CONTEXT_SCOPE_MODES),
    }


@router.post("/connections/test-context-scope")
async def test_context_scope(payload: dict):
    """Aplica a política de scope contra um output de exemplo e devolve
    o resultado. Usado pelo simulador do wizard antes do operador salvar.

    Payload:
      {
        "mode": "inherit" | "scoped" | "isolated",
        "template": str (opcional, só p/ mode=scoped),
        "max_chars": int (opcional, só p/ mode=scoped — atalho),
        "output": str (simulação do output do agente anterior),
        "final_state": str (opcional)
      }

    Returns (sucesso):
      {
        "mode": str,
        "output": str (resultado da aplicação do scope),
        "skip_prefix": bool,
        "chars_before": int,
        "chars_after": int,
        "reduction_pct": float,
        "effective_template": str (template realmente avaliado — útil
            quando max_chars expandiu pra output[:N]),
        "context": dict (vars disponíveis no Jinja, pra debugging)
      }

    Returns (erro):
      {"error": "<descrição>", "context": dict}

    Política: **fail-CLOSED** — erro vira mensagem para o operador
    corrigir o template antes de salvar. Em runtime
    (`_resolve_context_scope`) o erro é fail-OPEN; aqui o operador QUER
    ver o erro para corrigir.
    """
    from app.agents.engine import (
        CONTEXT_SCOPE_MODES, _apply_context_scope_template,
        _build_conditional_context,
    )

    mode = (payload.get("mode") or "inherit").strip().lower()
    if mode not in CONTEXT_SCOPE_MODES:
        return {
            "error": f"Modo inválido: {mode!r}. Use um de: {', '.join(CONTEXT_SCOPE_MODES)}.",
        }

    last_output = payload.get("output", "") or ""
    final_state = payload.get("final_state", "") or ""
    chars_before = len(last_output)
    ctx = _build_conditional_context(output=last_output, final_state=final_state)

    if mode == "inherit":
        return {
            "mode": "inherit",
            "output": last_output,
            "skip_prefix": False,
            "chars_before": chars_before,
            "chars_after": chars_before,
            "reduction_pct": 0.0,
            "effective_template": "",
            "context": ctx,
        }

    if mode == "isolated":
        return {
            "mode": "isolated",
            "output": "",
            "skip_prefix": True,
            "chars_before": chars_before,
            "chars_after": 0,
            "reduction_pct": 100.0 if chars_before > 0 else 0.0,
            "effective_template": "",
            "context": ctx,
        }

    # mode == "scoped"
    template = (payload.get("template") or "").strip()
    max_chars = payload.get("max_chars")
    if not template and isinstance(max_chars, int) and max_chars > 0:
        template = f"output[:{max_chars}]"
    if not template:
        return {
            "error": "Modo 'scoped' requer 'template' (expressão Jinja) ou 'max_chars' (atalho).",
            "context": ctx,
        }

    try:
        scoped_output = _apply_context_scope_template(template, ctx)
    except Exception as e:
        return {
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "effective_template": template,
            "context": ctx,
        }

    chars_after = len(scoped_output)
    return {
        "mode": "scoped",
        "output": scoped_output,
        "skip_prefix": False,
        "chars_before": chars_before,
        "chars_after": chars_after,
        "reduction_pct": (
            round((1 - chars_after / chars_before) * 100, 1)
            if chars_before > 0 else 0.0
        ),
        "effective_template": template,
        "context": ctx,
    }


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