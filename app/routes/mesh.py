"""Mesh + CAR — topologia e catálogo de roteadores §6."""
import uuid, json
from fastapi import APIRouter, HTTPException
from app.models.schemas import MeshConnectionCreate, CAREntryCreate
from app.core.database import mesh_repo, agents_repo, car_repo

router = APIRouter(prefix="/api/v1/mesh", tags=["mesh"])

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
    return {"nodes": nodes, "edges": edges, "fanout_roots": _fanout_roots(edges)}

@router.post("/connections", status_code=201)
async def create_connection(data: MeshConnectionCreate):
    if not await agents_repo.find_by_id(data.source_agent_id) or not await agents_repo.find_by_id(data.target_agent_id):
        raise HTTPException(404, "Agente não encontrado")
    cid = str(uuid.uuid4())
    await mesh_repo.create({"id":cid,"source_agent_id":data.source_agent_id,"target_agent_id":data.target_agent_id,"connection_type":data.connection_type,"config":data.config or "{}"})
    return {"id": cid, "message": "Conexão criada"}

@router.put("/connections/{conn_id}")
async def update_connection(conn_id: str, data: MeshConnectionCreate):
    existing = await mesh_repo.find_by_id(conn_id)
    if not existing: raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    return await mesh_repo.update(conn_id, upd)

@router.delete("/connections/{conn_id}")
async def delete_connection(conn_id: str):
    if not await mesh_repo.delete(conn_id): raise HTTPException(404)
    return {"message": "Conexão removida"}


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


@router.post("/connections/test-conditional")
async def test_conditional(payload: dict):
    """Avalia uma expressão Jinja boolean contra um output/final_state
    de exemplo. Usado pelo simulador do wizard antes do operador salvar.

    Payload: {"expr": str, "output": str (opcional), "final_state": str (opcional)}
    Returns: {"result": bool, "context": dict} OU {"error": str}

    Política: fail-CLOSED — erro vira mensagem para o operador corrigir
    a expressão antes de salvar. Em runtime (`_should_skip_conditional`)
    o erro é fail-OPEN porque é melhor executar do que perder dado; aqui
    o operador QUER ver o erro para corrigir.
    """
    expr = (payload.get("expr") or "").strip()
    if not expr:
        return {"error": "Expressão vazia — sem regra para avaliar."}

    from app.agents.engine import _build_conditional_context, _eval_conditional

    ctx = _build_conditional_context(
        output=payload.get("output", ""),
        final_state=payload.get("final_state", ""),
    )
    try:
        result = _eval_conditional(expr, ctx)
    except Exception as e:
        return {
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "context": ctx,
        }
    return {"result": bool(result), "context": ctx}


# ═══════════════════════════════════════════════════════════════════
# Context Scope Wizard (2026-06-01) — endpoints para o frontend
# escolher a política de propagação de contexto (inherit/scoped/isolated)
# entre nós da mesh chain. Complementar ao Conditional Routing — ver
# `_resolve_context_scope` em app/agents/engine.py.
# ═══════════════════════════════════════════════════════════════════


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