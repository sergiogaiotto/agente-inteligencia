"""Módulo IA Responsável (56.0.0) — governança, confiança e conformidade.

Painel único: postura computada das flags REAIS, trilha de auditoria e eventos
de segurança do `audit_log`, model cards, registro de risco, crosswalk de
frameworks, guarda/DLP e cockpit OPA — todas as fases entregues (nada de
número inventado aqui).

Todas as rotas são gated por require_role("root","admin"), que — desde 56.0.0 —
inclui o perfil "governanca" (herda os poderes de Admin).
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.config import get_settings, apply_settings_to_env
from app.core import opa_policies as opa_pol
from app.core.database import (
    audit_repo, agents_repo, skills_repo, governance_risk_repo, settings_store,
    users_repo, governance_officer_repo, governance_attestation_repo,
)
from app.skill_parser.parser import parse_skill_md

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])

_gate = require_role("root", "admin")


def _flag(name: str, default: bool = False) -> bool:
    return bool(getattr(get_settings(), name, default))


def _posture() -> tuple[int, list[dict]]:
    """Postura de governança computada do estado REAL das flags da plataforma.

    Cada pilar é a fração de checagens satisfeitas; o score é a média dos
    pilares. Honesto de propósito: se OPA está desligado, Segurança não é 100%.
    """
    retention_on = int(getattr(get_settings(), "interactions_retention_days", 0) or 0) > 0
    pillars = {
        "Privacidade": [
            ("Retenção por idade configurada", retention_on),
            ("DLP / redação de PII ligada", _flag("dlp_enabled", True)),
            ("Direito ao esquecimento disponível", True),
        ],
        "Segurança": [
            ("Guarda de injeção (LLM01) ligada", _flag("prompt_guard_enabled", True)),
            ("Grounding estrito ligado", _flag("grounding_strict", True)),
            ("Circuit breaker ligado", _flag("circuit_breaker_enabled", True)),
            ("Policy-as-code (OPA) ligado", _flag("opa_enabled", False)),
        ],
        "Transparência": [
            ("Verifier/juiz multidimensional ligado", _flag("verifier_v2_enabled", False)),
            ("Fichas de transparência (model cards) disponíveis", True),
        ],
        "Robustez": [
            ("Circuit breaker ligado", _flag("circuit_breaker_enabled", True)),
            ("Verifier v2 ligado", _flag("verifier_v2_enabled", False)),
        ],
        "Auditabilidade": [
            ("Trilha de auditoria ativa", True),
            ("Retenção por idade configurada", retention_on),
        ],
    }
    out = []
    for name, checks in pillars.items():
        ok = sum(1 for _, v in checks if v)
        out.append({
            "pillar": name,
            "pct": round(ok / len(checks) * 100),
            "checks": [{"label": lbl, "ok": v} for lbl, v in checks],
        })
    score = round(sum(p["pct"] for p in out) / len(out)) if out else 0
    return score, out


@router.get("/summary")
async def governance_summary(user=Depends(_gate)):
    score, pillars = _posture()
    return {
        "posture_score": score,
        "pillars": pillars,
        # estado REAL das capacidades — alimenta os chips da Visão geral.
        "capabilities": {
            "opa_enabled": _flag("opa_enabled", False),
            "prompt_guard_enabled": _flag("prompt_guard_enabled", True),
            "dlp_enabled": _flag("dlp_enabled", True),
            "grounding_strict": _flag("grounding_strict", True),
            "circuit_breaker_enabled": _flag("circuit_breaker_enabled", True),
            "verifier_v2_enabled": _flag("verifier_v2_enabled", False),
            "interactions_retention_days": int(getattr(get_settings(), "interactions_retention_days", 0) or 0),
        },
    }


def _audit_row(r: dict, actor_names: dict | None = None) -> dict:
    actor = r.get("actor")
    return {
        "id": r.get("id"),
        "action": r.get("action"),
        "entity_type": r.get("entity_type"),
        "entity_id": r.get("entity_id"),
        "actor": actor,
        "actor_name": (actor_names or {}).get(actor, actor),
        "ip": r.get("ip"),
        "created_at": str(r.get("created_at") or ""),
        # 66.0.0: painel de detalhe da linha (UI) — details já é redigido na
        # escrita quando vem de input de usuário (redact_for_log).
        "details": r.get("details") or "",
    }


async def _actor_names(rows: list[dict]) -> dict:
    """actor no audit_log é às vezes o USERNAME (call sites explícitos) e às
    vezes o USER_ID cru (fallback do AuditRepository via user_id_var) — a UI
    acabava exibindo UUID. Resolve os actors distintos em 1 query batelada
    (reusa _resolve_user_names do dashboard; N+1 aqui foi achado de revisão);
    actor que não é id conhecido fica de fora do mapa e o consumidor cai no
    valor cru. Best-effort: a trilha nunca quebra por falha de lookup."""
    actors = {(r.get("actor") or "").strip() for r in rows} - {""}
    if not actors:
        return {}
    try:
        from app.routes.dashboard import _resolve_user_names
        return await _resolve_user_names(list(actors))
    except Exception:
        return {}


@router.get("/audit")
async def governance_audit(limit: int = 50, user=Depends(_gate)):
    """Trilha de auditoria (audit_log) — mais recentes primeiro."""
    lim = min(max(limit, 1), 200)
    rows = await audit_repo.find_all(limit=lim)
    rows = sorted(rows, key=lambda r: r.get("id") or 0, reverse=True)[:lim]
    names = await _actor_names(rows)
    return {"events": [_audit_row(r, names) for r in rows]}


def _is_security_action(action: str | None) -> bool:
    a = (action or "").lower()
    return ("injection" in a) or ("->refuse" in a) or ("->escalate" in a) or ("policy" in a)


@router.get("/security-events")
async def governance_security_events(limit: int = 50, user=Depends(_gate)):
    """Eventos de segurança derivados do audit_log: injeções bloqueadas,
    recusas e escalonamentos (sinais da defesa-em-profundidade em ação)."""
    # clamp igual ao /audit — sem ele, limit=100000 na query string viraria até
    # 1000 linhas com resolução de actor (achado de revisão adversarial).
    lim = min(max(limit, 1), 200)
    rows = await audit_repo.find_all(limit=1000)
    counts: Counter = Counter()
    for r in rows:
        a = (r.get("action") or "").lower()
        # 66.0.0: warn ganhou evento próprio (prompt_injection_warned) — antes
        # a zona cinza da guarda morria em memória; separar evita inflar o
        # contador de bloqueios com avisos.
        if "injection_warned" in a:
            counts["injecoes_avisadas"] += 1
        elif "injection" in a:
            counts["injecoes_bloqueadas"] += 1
        if "->refuse" in a:
            counts["recusas"] += 1
        if "->escalate" in a:
            counts["escalonamentos"] += 1
    sec = [r for r in rows if _is_security_action(r.get("action"))]
    sec = sorted(sec, key=lambda r: r.get("id") or 0, reverse=True)[:lim]
    names = await _actor_names(sec)
    return {
        "counts": dict(counts),
        "scanned": len(rows),
        "events": [_audit_row(r, names) for r in sec],
    }


# ── Exportação p/ Excel (66.0.0) — CSV com BOM, padrão da plataforma ─────────
def _audit_csv_response(rows: list[dict], names: dict, filename: str):
    """CSV UTF-8 com BOM (Excel abre acentos pt-BR sem mojibake) + neutralização
    de CSV-injection (células iniciadas em =+-@ viram texto) — mesmo padrão do
    export de verifications do dashboard."""
    import csv
    import io

    def _safe(v):
        v = "" if v is None else str(v)
        if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    cols = ["id", "created_at", "action", "entity_type", "entity_id",
            "actor", "actor_name", "ip", "details"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        row = _audit_row(r, names)
        w.writerow([_safe(row.get(c)) for c in cols])
    from fastapi import Response
    return Response(
        "\N{ZERO WIDTH NO-BREAK SPACE}" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/audit/export")
async def governance_audit_export(user=Depends(_gate)):
    """Trilha de auditoria completa (até 1000 mais recentes) para Excel."""
    rows = await audit_repo.find_all(limit=1000)
    rows = sorted(rows, key=lambda r: r.get("id") or 0, reverse=True)
    names = await _actor_names(rows)
    return _audit_csv_response(rows, names, "auditoria-ia-responsavel.csv")


@router.get("/security-events/export")
async def governance_security_events_export(user=Depends(_gate)):
    """Eventos de segurança (subset do audit_log) para Excel."""
    rows = await audit_repo.find_all(limit=1000)
    sec = [r for r in rows if _is_security_action(r.get("action"))]
    sec = sorted(sec, key=lambda r: r.get("id") or 0, reverse=True)
    names = await _actor_names(sec)
    return _audit_csv_response(sec, names, "eventos-seguranca.csv")


# ── Cockpit Guardrails (66.0.0 — Fase 1: visibilidade, zero enforcement) ─────
# Mapa entrada→modelo→saída com o estado REAL de cada guarda (lido das flags
# vivas) + cobertura honesta (inclusive gaps "não implementado"). Nada aqui
# altera decisão de runtime: é leitura + simulação dry-run.
@router.get("/guardrails")
async def guardrails_map(user=Depends(_gate)):
    s = get_settings()
    _b = float(getattr(s, "prompt_guard_block_threshold", 0.7))
    _w = float(getattr(s, "prompt_guard_warn_threshold", 0.4))
    dlp_on = bool(getattr(s, "dlp_enabled", True))
    stages = [
        {"stage": "entrada", "label": "Entrada — antes do LLM", "guards": [
            {"key": "prompt_guard", "label": "Guarda de injeção (OWASP LLM01)",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "prompt_guard_enabled", True)),
             "detail": f"regex/heurística em 5 camadas (en/pt/es) · bloqueia ≥ {_b:.2f} · avisa ≥ {_w:.2f}",
             "coverage": "caminho FSM (agentes LLM); o caminho declarativo NÃO passa pela guarda",
             "config_tab": "security"},
            {"key": "dlp_pre_llm", "label": "DLP pré-LLM (PII não sai ao provedor)",
             "deterministic": True, "implemented": True,
             "on": dlp_on and bool(getattr(s, "dlp_redact_before_llm", False)),
             "detail": "redige mensagem, anexos, evidências (inclusive rerank), histórico e pergunta do juiz",
             "coverage": "exceções: embed_query cru (recall) e imagens não redigidas",
             "config_tab": "security"},
            {"key": "opa_interaction", "label": "PolicyCheck via OPA (interaction.rego)",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "opa_enabled", False)),
             "detail": "status do usuário + score de injeção (o sinal rate_limit é enviado fixo exceeded=false — o middleware barra antes); guarda local segue autoritativa (AND)",
             "coverage": "decisões auditadas no log da aba Políticas",
             "config_tab": "policies"},
            {"key": "evidence_acl", "label": "Evidence ACL — no read up",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "evidence_acl_enabled", False)),
             "detail": "clearance do usuário × confidencialidade da fonte no retrieval",
             "coverage": "todos os call sites do retriever passam clearance (teste-guarda)",
             "config_tab": "policies"},
            {"key": "rate_limit", "label": "Rate limit por rota",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "rate_limit_enabled", True)),
             "detail": (
                 f"API {int(getattr(s, 'rate_limit_default_per_min', 300))}/janela · "
                 f"rotas LLM {int(getattr(s, 'rate_limit_workspace_per_min', 20))}/janela · "
                 f"login {int(getattr(s, 'rate_limit_auth_per_min', 10))}/janela "
                 f"(janela {int(getattr(s, 'rate_limit_window_seconds', 60))}s) — excedente recebe 429"
             ),
             "coverage": "3 baldes independentes por prefixo de rota", "config_tab": ""},
        ]},
        {"stage": "modelo", "label": "Na LLM — durante a geração", "guards": [
            {"key": "grounding", "label": "Grounding estrito",
             "deterministic": False, "implemented": True,
             "on": bool(getattr(s, "grounding_strict", True)),
             "detail": "responde SÓ com base em evidência; escape hatch por agente (allow_general_knowledge); router isento",
             "coverage": "diretiva + guarda de recusa no engine", "config_tab": ""},
            {"key": "opa_tools", "label": "Gate de tools via OPA (tool_invocation.rego)",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "opa_enabled", False)),
             "detail": "sensibilidade da tool × papel do dono da sessão; rótulo não-reconhecido = fail-closed; tool SEM classificação = low (liberada, retrocompat)",
             "coverage": "toda chamada de tool no harness", "config_tab": "policies"},
            {"key": "skill_guardrails", "label": "Guardrails declarativos da skill",
             "deterministic": False, "implemented": True, "on": True,
             "detail": "seção ## Guardrails do SKILL.md entra no system prompt do agente",
             "coverage": "por agente; texto livre (não é política executável — candidata à Fase 3)",
             "config_tab": ""},
            {"key": "circuit_breaker", "label": "Circuit breaker por provedor",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "circuit_breaker_enabled", True)),
             "detail": "abre por provedor após falhas consecutivas (Redis, cross-worker)",
             "coverage": "geração dos agentes (cadeia de resiliência), juiz e wizard; chamadas diretas get_provider().generate() (ex.: reranker) ficam FORA",
             "config_tab": ""},
        ]},
        {"stage": "saida", "label": "Saída — depois da geração", "guards": [
            {"key": "verifier", "label": "Verifier / juiz multidimensional",
             "deterministic": False, "implemented": True,
             "on": bool(getattr(s, "verifier_v2_enabled", False)),
             "detail": "grounding, contrato e risco; posturas sync/async por pipeline (fraude: reservado — só existe no verificador legado)",
             "coverage": "persiste em verifications (auditável em /quality)", "config_tab": ""},
            {"key": "contract", "label": "Contrato de saída + retry",
             "deterministic": True, "implemented": True,
             "on": bool(getattr(s, "verifier_v2_enabled", False)),
             "detail": "valida ## Output Contract da skill via Verifier v2; retry corretivo gateado por verifier_contract_retry_enabled",
             "coverage": "quando a skill declara contrato E o Verifier v2 está ligado; steps standard de pipeline auto-passam sem juiz",
             "config_tab": ""},
            {"key": "dlp_persist", "label": "DLP na persistência",
             "deterministic": True, "implemented": True, "on": dlp_on,
             "detail": "PII redigida nas colunas *_redacted (histórico e UI)",
             "coverage": "todos os writers de turnos (teste-guarda repo-wide)",
             "config_tab": "security"},
            {"key": "output_redaction", "label": "Redação de PII na RESPOSTA ao usuário",
             "deterministic": True, "implemented": False, "on": False,
             "detail": "hoje a resposta entregue sai crua — só a persistência é redigida",
             "coverage": "gap conhecido — Fase 2 do módulo de Guardrails", "config_tab": ""},
            {"key": "leak_detector", "label": "Detector de vazamento (prompt/confidencial)",
             "deterministic": True, "implemented": False, "on": False,
             "detail": "checaria a resposta contra system prompt e evidência confidencial",
             "coverage": "gap conhecido — Fase 2", "config_tab": ""},
            {"key": "denied_topics", "label": "Tópicos negados por agente",
             "deterministic": False, "implemented": False, "on": False,
             "detail": "lista de temas proibidos avaliada na saída",
             "coverage": "gap conhecido — Fase 2", "config_tab": ""},
        ]},
    ]
    # contadores REAIS do audit_log (mesma varredura do security-events)
    rows = await audit_repo.find_all(limit=1000)
    counts: Counter = Counter()
    for r in rows:
        a = (r.get("action") or "").lower()
        if "injection_warned" in a:
            counts["injecoes_avisadas"] += 1
        elif "injection" in a:
            counts["injecoes_bloqueadas"] += 1
        if r.get("entity_type") == "policy_decision" and (r.get("action") or "") == "deny":
            counts["policy_denies"] += 1
    return {"stages": stages, "counters": dict(counts), "scanned": len(rows)}


class GuardrailSimulate(BaseModel):
    text: str


@router.post("/guardrails/simulate")
async def guardrails_simulate(data: GuardrailSimulate, user=Depends(_gate)):
    """What-if dos guardrails determinísticos de ENTRADA — dry-run puro: roda o
    detector de injeção com os limiares VIGENTES e o DLP sobre o texto, sem
    auditar, sem persistir e sem chamar LLM nenhum."""
    text = (data.text or "")[:20000]
    if not text.strip():
        raise HTTPException(422, "texto vazio")
    from app.core.prompt_guard import detect as _detect
    from app.core.dlp import count_pii, redact
    s = get_settings()
    g = _detect(
        text,
        block_threshold=float(getattr(s, "prompt_guard_block_threshold", 0.7)),
        warn_threshold=float(getattr(s, "prompt_guard_warn_threshold", 0.4)),
    )
    pii = count_pii(text)
    return {
        "prompt_guard": {
            "enabled": bool(getattr(s, "prompt_guard_enabled", True)),
            "score": round(float(g.score), 3),
            "blocked": bool(g.blocked),
            "warn": bool(g.warn),
            "matched": list(g.matched_patterns)[:12],
            "block_threshold": float(getattr(s, "prompt_guard_block_threshold", 0.7)),
            "warn_threshold": float(getattr(s, "prompt_guard_warn_threshold", 0.4)),
        },
        "dlp": {
            "enabled": bool(getattr(s, "dlp_enabled", True)),
            "pre_llm_enabled": bool(getattr(s, "dlp_enabled", True)) and bool(getattr(s, "dlp_redact_before_llm", False)),
            "counts": {"cpf": pii.cpf, "cnpj": pii.cnpj, "email": pii.email,
                       "phone": pii.phone, "card": pii.card, "cep": pii.cep,
                       "total": pii.total},
            "redacted_preview": redact(text)[:2000],
        },
    }


# ── Model / System cards (transparência) ─────────────────────────────────────
# Ficha automática por agente, derivada 100% da definição REAL (campos do agente
# + seções da SKILL.md). Artefato de transparência para auditores. Zero invenção.
def _truthy(v) -> bool:
    return v is True or v == 1 or str(v).strip().lower() in ("1", "true")


async def _skill_sections(agent: dict) -> dict | None:
    sid = agent.get("skill_id")
    if not sid:
        return None
    row = await skills_repo.find_by_id(sid)
    if not row or not row.get("raw_content"):
        return None
    p = parse_skill_md(row["raw_content"])
    return {
        "id": sid,
        "purpose": p.purpose, "guardrails": p.guardrails,
        "evidence_policy": p.evidence_policy, "output_contract": p.output_contract,
        "inputs": p.inputs, "tool_bindings": p.tool_bindings,
        "failure_modes": p.failure_modes, "execution_mode": p.execution_mode,
    }


def _risk_signals(agent: dict, skill: dict | None) -> list[dict]:
    sig = []
    if _truthy(agent.get("allow_general_knowledge")):
        sig.append({"label": "Pode usar conhecimento geral (fora das evidências)", "level": "warn"})
    if not _truthy(agent.get("require_evidence")):
        sig.append({"label": "Não exige evidência para responder", "level": "warn"})
    if not skill:
        sig.append({"label": "Sem SKILL.md vinculada — comportamento não declarado", "level": "warn"})
    else:
        if (skill.get("guardrails") or "").strip():
            sig.append({"label": "Guardrails declarados na skill", "level": "ok"})
        if (skill.get("evidence_policy") or "").strip():
            sig.append({"label": "Política de evidência declarada", "level": "ok"})
    if _truthy(agent.get("accepts_documents")) or _truthy(agent.get("accepts_images")):
        sig.append({"label": "Recebe anexos (documentos/imagens)", "level": "info"})
    if (agent.get("kind") or "") == "aobd":
        sig.append({"label": "Orquestrador — coordena outros agentes", "level": "info"})
    return sig


@router.get("/model-cards")
async def model_cards_list(user=Depends(_gate)):
    agents = await agents_repo.find_all(limit=500)
    cards = []
    for a in agents:
        has_skill = bool(a.get("skill_id"))
        warns = 0
        if _truthy(a.get("allow_general_knowledge")):
            warns += 1
        if not _truthy(a.get("require_evidence")):
            warns += 1
        if not has_skill:
            warns += 1
        cards.append({
            "agent_id": a.get("id"), "name": a.get("name"), "kind": a.get("kind"),
            "domain": a.get("domain") or "", "status": a.get("status"),
            "has_skill": has_skill, "warn_count": warns,
        })
    cards.sort(key=lambda c: (-c["warn_count"], (c["name"] or "").lower()))
    return {"cards": cards}


@router.get("/model-cards/{agent_id}")
async def model_card_detail(agent_id: str, user=Depends(_gate)):
    a = await agents_repo.find_by_id(agent_id)
    if not a:
        raise HTTPException(404)
    skill = await _skill_sections(a)
    return {
        "agent_id": a.get("id"), "name": a.get("name"), "kind": a.get("kind"),
        "domain": a.get("domain") or "", "status": a.get("status"),
        "version": a.get("version"), "description": a.get("description") or "",
        "model": {
            "provider": a.get("llm_provider"), "model": a.get("model"),
            "temperature": a.get("temperature"),
        },
        "data_handling": {
            "accepts_images": _truthy(a.get("accepts_images")),
            "accepts_documents": _truthy(a.get("accepts_documents")),
            "require_evidence": _truthy(a.get("require_evidence")),
            "allow_general_knowledge": _truthy(a.get("allow_general_knowledge")),
            "response_language": a.get("response_language") or "",
        },
        "skill": skill,
        "risk_signals": _risk_signals(a, skill),
    }


# ── Registro de risco (classificação estilo EU AI Act) ───────────────────────
_TIERS = ("unacceptable", "high", "limited", "minimal")


class RiskClassify(BaseModel):
    tier: str
    rationale: str = ""
    mitigations: str = ""


def _suggested_signals(agent: dict) -> list[dict]:
    """Os sinais que a heurística de sugestão considera, item a item — a UI
    (painel de detalhe do Risco, 66.1.0) mostra exatamente ESTAS regras, para o
    humano entender de onde veio o tier sugerido antes de decidir."""
    return [
        {"label": "Pode usar conhecimento geral (fora das evidências)",
         "warn": _truthy(agent.get("allow_general_knowledge"))},
        {"label": "Não exige evidência para responder",
         "warn": not _truthy(agent.get("require_evidence"))},
        {"label": "Sem SKILL.md vinculada — comportamento não declarado",
         "warn": not agent.get("skill_id")},
    ]


def _suggested_tier(agent: dict) -> str:
    """Sugestão (humano decide) a partir dos sinais de risco derivados dos campos
    reais do agente — sem parsear a skill (os sinais de ATENÇÃO não dependem dela).
    Regra: 3 sinais = high; 1–2 = limited; 0 = minimal."""
    warns = sum(1 for s in _suggested_signals(agent) if s["warn"])
    if warns >= 3:
        return "high"
    if warns >= 1:
        return "limited"
    return "minimal"


@router.get("/risk-register")
async def risk_register(user=Depends(_gate)):
    agents = await agents_repo.find_all(limit=500)
    classifications = {
        c.get("entity_id"): c
        for c in await governance_risk_repo.find_all(limit=1000, entity_type="agent")
    }
    items = []
    counts: Counter = Counter()
    for a in agents:
        cls = classifications.get(a.get("id"))
        tier = cls.get("tier") if cls else None
        counts[tier or "unclassified"] += 1
        items.append({
            "entity_type": "agent", "entity_id": a.get("id"), "name": a.get("name"),
            "kind": a.get("kind"), "domain": a.get("domain") or "",
            "suggested_tier": _suggested_tier(a),
            "signals": _suggested_signals(a),  # 66.1.0: regras do painel de detalhe
            "tier": tier,
            "rationale": cls.get("rationale") if cls else "",
            "mitigations": cls.get("mitigations") if cls else "",
            "classified_by": cls.get("classified_by") if cls else "",
            "classified_at": str(cls.get("classified_at")) if cls else "",
        })
    # não-classificados primeiro (pendência), depois por risco (alto→mínimo)
    order = {None: 0, "unacceptable": 1, "high": 2, "limited": 3, "minimal": 4}
    items.sort(key=lambda x: (order.get(x["tier"], 9), (x["name"] or "").lower()))
    return {"items": items, "counts": dict(counts), "tiers": list(_TIERS)}


@router.put("/risk/{entity_type}/{entity_id}")
async def classify_risk(entity_type: str, entity_id: str, data: RiskClassify, user=Depends(_gate)):
    if entity_type not in ("agent", "pipeline"):
        raise HTTPException(422, "entity_type inválido (agent|pipeline)")
    if data.tier not in _TIERS:
        raise HTTPException(422, "tier inválido")
    who = (user or {}).get("username") or (user or {}).get("display_name") or "?"
    payload = {
        "entity_type": entity_type, "entity_id": entity_id, "tier": data.tier,
        "rationale": data.rationale or "", "mitigations": data.mitigations or "",
        "classified_by": who,
    }
    existing = await governance_risk_repo.find_all(limit=1, entity_type=entity_type, entity_id=entity_id)
    if existing:
        await governance_risk_repo.update(existing[0]["id"], {**payload, "classified_at": datetime.utcnow()})
    else:
        payload["id"] = str(uuid.uuid4())
        await governance_risk_repo.create(payload)
    await audit_repo.create({
        "entity_type": "governance_risk", "entity_id": entity_id,
        "action": f"risk_classified:{data.tier}", "actor": who,
    })
    return {"message": "Classificação de risco salva"}


# ── Guarda de injeção & DLP — configuração (traz o headless à UI) ─────────────
class GuardConfig(BaseModel):
    prompt_guard_enabled: bool | None = None
    prompt_guard_block_threshold: float | None = None
    prompt_guard_warn_threshold: float | None = None
    dlp_enabled: bool | None = None
    dlp_redact_before_llm: bool | None = None


@router.get("/guard-config")
async def guard_config_get(user=Depends(_gate)):
    s = get_settings()
    return {
        "prompt_guard_enabled": bool(getattr(s, "prompt_guard_enabled", True)),
        "prompt_guard_block_threshold": float(getattr(s, "prompt_guard_block_threshold", 0.7)),
        "prompt_guard_warn_threshold": float(getattr(s, "prompt_guard_warn_threshold", 0.4)),
        "dlp_enabled": bool(getattr(s, "dlp_enabled", True)),
        "dlp_redact_before_llm": bool(getattr(s, "dlp_redact_before_llm", False)),
    }


@router.put("/guard-config")
async def guard_config_put(data: GuardConfig, user=Depends(_gate)):
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    for k in ("prompt_guard_block_threshold", "prompt_guard_warn_threshold"):
        if k in payload and not (0.0 <= float(payload[k]) <= 1.0):
            raise HTTPException(422, f"{k} deve estar entre 0 e 1")
    if ("prompt_guard_block_threshold" in payload and "prompt_guard_warn_threshold" in payload
            and float(payload["prompt_guard_warn_threshold"]) > float(payload["prompt_guard_block_threshold"])):
        raise HTTPException(422, "o limiar de aviso não pode ser maior que o de bloqueio")
    if not payload:
        return {"message": "Nada a alterar", "env_applied": 0}
    # Persiste só estas chaves (sem tocar segredos de outras abas) + aplica em runtime.
    await settings_store.set_many({k: str(v) for k, v in payload.items()})
    try:
        applied = await apply_settings_to_env()
    except Exception:
        applied = 0
    who = (user or {}).get("username") or (user or {}).get("display_name") or "?"
    await audit_repo.create({
        "entity_type": "settings", "entity_id": "guard_dlp",
        "action": "guard_dlp_updated", "actor": who,
    })
    return {"message": "Configuração de guarda/DLP salva", "keys": list(payload.keys()), "env_applied": applied}


# ── Policy-as-code (OPA) — cockpit (62.0.0 read/simulate · 63.0.0 edição) ─────
# Status + toggle + visualização/EDIÇÃO das políticas + simulador what-if + log.
# Constantes e helpers de política vivem em app.core.opa_policies (compartilhados
# com o re-push no boot). Aliases locais por legibilidade das rotas.
_OPA_PACKAGES = opa_pol.PACKAGES
_OPA_WIRED = opa_pol.WIRED


class OpaConfig(BaseModel):
    opa_enabled: bool | None = None
    opa_failsafe_open: bool | None = None
    opa_timeout_seconds: float | None = None
    evidence_acl_enabled: bool | None = None  # 64.0.0: "no read up" de evidência


@router.get("/opa/status")
async def opa_status(user=Depends(_gate)):
    """Estado do OPA: flags (do settings) + saúde do servidor. Alimenta o form."""
    from app.core import opa_client
    s = get_settings()
    return {
        "enabled": bool(getattr(s, "opa_enabled", False)),
        "url": getattr(s, "opa_url", ""),
        "failsafe_open": bool(getattr(s, "opa_failsafe_open", True)),
        "timeout_seconds": float(getattr(s, "opa_timeout_seconds", 2.0)),
        "evidence_acl_enabled": bool(getattr(s, "evidence_acl_enabled", False)),
        "server_ok": await opa_client.server_health(),
        # 64.0.0: erros do último re-push no boot — se não-vazio, o OPA pode estar
        # servindo o baked enquanto o DB mostra a versão editada (drift silencioso).
        "policy_repush_errors": opa_pol.last_repush().get("errors", []),
    }


@router.get("/opa/policies")
async def opa_policies(user=Depends(_gate)):
    """Políticas Rego vigentes (via OPA); fallback DB→disco se o OPA estiver fora."""
    from app.core import opa_client
    result = await opa_client.list_policies()
    policies = []
    if result is not None:
        source = "opa"
        for p in result:
            pkg = opa_pol.pkg_from_id(p.get("id", ""))
            policies.append({
                "id": p.get("id", ""), "package": pkg,
                "raw": p.get("raw", ""), "wired": pkg in _OPA_WIRED,
            })
    else:
        # OPA fora do ar → prefere a vigente do DB (o que DEVERIA estar no OPA),
        # senão o baked do disco. DB indisponível (unit sem pool) → cai p/ disco.
        any_db = False
        for package in opa_pol.PACKAGES:
            raw = None
            try:
                cur = await opa_pol.current_version(package)
                raw = cur["rego"] if cur and cur.get("rego") else None
            except Exception:
                raw = None
            if raw is not None:
                any_db = True
            else:
                raw = opa_pol.read_baked(package) or ""
            policies.append({"id": opa_pol.policy_id_for(package), "package": package,
                             "raw": raw, "wired": package in _OPA_WIRED})
        source = "db" if any_db else "disk"  # rótulo honesto da origem
    # evidence só está "em uso" quando o Evidence ACL está ligado (64.0.0).
    _ev_on = bool(getattr(get_settings(), "evidence_acl_enabled", False))
    for pol in policies:
        if pol["package"] == "evidence":
            pol["wired"] = _ev_on
    # wired primeiro, depois alfabético.
    policies.sort(key=lambda x: (not x["wired"], x["package"]))
    return {"source": source, "policies": policies}


class OpaSimulate(BaseModel):
    package: str
    rule: str = "allow"
    input: dict = {}


@router.post("/opa/simulate")
async def opa_simulate(data: OpaSimulate, user=Depends(_gate)):
    """What-if: avalia um input contra uma política SEM efeito colateral (não
    audita, ignora o toggle opa_enabled → funciona mesmo com o OPA desligado)."""
    if data.package not in _OPA_PACKAGES:
        raise HTTPException(422, "pacote inválido (interaction|tool_invocation|evidence)")
    from app.core import opa_client
    decision = await opa_client.simulate(data.package, (data.rule or "allow"), data.input or {})
    reasons = None
    # Cada política expõe seus motivos numa rule própria (interaction=reasons set;
    # tool_invocation=reason string; evidence não tem).
    if data.package == "interaction":
        reasons = (await opa_client.simulate("interaction", "reasons", data.input or {})).get("result")
    elif data.package == "tool_invocation":
        reasons = (await opa_client.simulate("tool_invocation", "reason", data.input or {})).get("result")
    return {
        "allow": decision.get("allow"),
        "result": decision.get("result"),
        "reasons": reasons,
        "source": decision.get("source"),
        "duration_ms": decision.get("duration_ms"),
        "error": decision.get("error"),
    }


@router.get("/opa/decisions")
async def opa_decisions(limit: int = 50, user=Depends(_gate)):
    """Log de decisões de política (audit_log entity_type=policy_decision)."""
    lim = min(max(limit, 1), 200)
    rows = await audit_repo.find_all(entity_type="policy_decision", limit=1000)
    rows = sorted(rows, key=lambda r: r.get("id") or 0, reverse=True)
    out = []
    for r in rows[:lim]:
        pkg = rule = src = ""
        dur = None
        try:
            d = json.loads(r.get("details") or "{}")
            pkg, rule = d.get("package", ""), d.get("rule", "")
            dec = d.get("decision", {})
            src, dur = dec.get("source", ""), dec.get("duration_ms")
        except Exception:
            pass
        out.append({
            "id": r.get("id"), "action": r.get("action"), "entity_id": r.get("entity_id"),
            "package": pkg, "rule": rule, "source": src, "duration_ms": dur,
            "created_at": str(r.get("created_at") or ""),
        })
    return {"decisions": out}


@router.put("/opa/config")
async def opa_config_put(data: OpaConfig, user=Depends(_gate)):
    """Liga/desliga o OPA + failsafe + timeout. Persiste em platform_settings e
    aplica ao runtime (sem restart). Requer as 3 chaves no _UI_TO_ENV_MAP."""
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "opa_timeout_seconds" in payload and not (0.1 <= float(payload["opa_timeout_seconds"]) <= 30.0):
        raise HTTPException(422, "timeout deve estar entre 0.1 e 30 segundos")
    if not payload:
        return {"message": "Nada a alterar", "env_applied": 0}
    await settings_store.set_many({k: str(v) for k, v in payload.items()})
    try:
        applied = await apply_settings_to_env()
    except Exception:
        applied = 0
    who = (user or {}).get("username") or (user or {}).get("display_name") or "?"
    await audit_repo.create({
        "entity_type": "settings", "entity_id": "opa_policy",
        "action": "opa_config_updated", "actor": who,
    })
    return {"message": "Configuração do OPA salva", "keys": list(payload.keys()), "env_applied": applied}


# ── Edição persistente de políticas Rego (63.0.0 — cockpit Fase B) ────────────
# Editar uma política é reescrever uma regra de segurança: validado no OPA (que
# COMPILA), auditado e VERSIONADO (append-only) com rollback. DB = fonte viva; o
# .rego baked é o seed. Mesmo gate root/admin/governanca das demais rotas.
class OpaPolicyEdit(BaseModel):
    rego: str
    note: str = ""


class OpaRollback(BaseModel):
    version: int


def _who(user) -> str:
    return (user or {}).get("username") or (user or {}).get("display_name") or "?"


async def _apply_and_save(package: str, rego: str, note: str, who: str, action: str) -> int:
    """Valida+empurra no OPA, grava versão nova e audita — atômico do ponto de
    vista da governança: se a persistência falhar DEPOIS do push, COMPENSA
    re-empurrando o estado anterior, para o OPA nunca aplicar uma mudança que não
    ficou registrada/auditada. 422 = Rego inválido/pacote errado; 503 = OPA fora."""
    prev_raw = await opa_pol.opa_current_raw(package)  # snapshot ANTES (via OPA, não DB)
    res = await opa_pol.validate_and_push(package, rego)
    if not res.get("ok"):
        if res.get("kind") == "unreachable":
            raise HTTPException(503, f"OPA indisponível — política não aplicada ({res.get('error')})")
        raise HTTPException(422, f"política rejeitada pelo OPA: {res.get('error')}")
    try:
        ver = await opa_pol.save_version(package, rego, note, who)
        await audit_repo.create({
            "entity_type": "governance_policy", "entity_id": package,
            "action": action.format(v=ver), "actor": who,
        })
    except Exception:
        await opa_pol.revert_opa(package, prev_raw)  # OPA não fica com mudança não registrada
        raise HTTPException(500, "falha ao registrar a política; alteração revertida no OPA")
    return ver


@router.put("/opa/policies/{package}")
async def opa_policy_edit(package: str, data: OpaPolicyEdit, user=Depends(_gate)):
    if package not in opa_pol.PACKAGES:
        raise HTTPException(422, "pacote inválido (interaction|tool_invocation|evidence)")
    rego = (data.rego or "").strip()
    if not rego:
        raise HTTPException(422, "a política não pode ser vazia")
    ver = await _apply_and_save(package, rego, data.note or "editado pela UI", _who(user), "policy_edited:v{v}")
    return {"message": "Política salva e aplicada no OPA", "package": package, "version": ver}


@router.get("/opa/policies/{package}/versions")
async def opa_policy_versions(package: str, user=Depends(_gate)):
    if package not in opa_pol.PACKAGES:
        raise HTTPException(422, "pacote inválido")
    rows = await opa_pol.list_versions(package)
    return {
        "package": package,
        "current": rows[0]["version"] if rows else None,
        "versions": [{
            "version": r.get("version"), "note": r.get("note") or "",
            "created_by": r.get("created_by") or "", "created_at": str(r.get("created_at") or ""),
        } for r in rows],
    }


@router.post("/opa/policies/{package}/rollback")
async def opa_policy_rollback(package: str, data: OpaRollback, user=Depends(_gate)):
    if package not in opa_pol.PACKAGES:
        raise HTTPException(422, "pacote inválido")
    target = next((r for r in await opa_pol.list_versions(package) if r.get("version") == data.version), None)
    if not target:
        raise HTTPException(404, "versão não encontrada")
    ver = await _apply_and_save(package, target["rego"], f"rollback de v{data.version}",
                                _who(user), f"policy_rollback:v{data.version}->v{{v}}")
    return {"message": f"Revertido para a v{data.version}", "package": package, "version": ver}


@router.post("/opa/policies/{package}/restore-default")
async def opa_policy_restore(package: str, user=Depends(_gate)):
    if package not in opa_pol.PACKAGES:
        raise HTTPException(422, "pacote inválido")
    baked = opa_pol.read_baked(package)
    if not baked:
        raise HTTPException(404, "política baked não encontrada na imagem")
    ver = await _apply_and_save(package, baked, "restaurado padrão baked", _who(user), "policy_restore_default:v{v}")
    return {"message": "Padrão baked restaurado e aplicado", "package": package, "version": ver}


# ── Crosswalk de conformidade (EU AI Act / NIST / ISO 42001 / LGPD / OWASP) ───
# Cobertura HONESTA: mapeia os controles REAIS da plataforma (flags + capacidades
# entregues) contra os requisitos de cada framework. Um requisito é "coberto" se
# ALGUM dos controles que o atendem está ativo. Zero % inventado.
_CONTROL_LABELS = {
    "grounding": "Grounding estrito",
    "prompt_guard": "Guarda de injeção (LLM01)",
    "dlp": "DLP / redação de PII",
    "verifier": "Verifier / juiz multidim",
    "circuit_breaker": "Circuit breaker",
    "opa": "Policy-as-code (OPA)",
    "retention": "Retenção por idade",
    "forget": "Direito ao esquecimento",
    "audit": "Trilha de auditoria",
    "rbac": "RBAC / papéis",
    "model_cards": "Model cards",
    "risk_register": "Registro de risco",
    "federation_guard": "Federação (SSRF/HMAC)",
    "evidence_policy": "Política de evidência",
}

_FRAMEWORKS = {
    "EU AI Act": [
        ("Classificação de risco do sistema", ["risk_register"]),
        ("Transparência e documentação técnica", ["model_cards"]),
        ("Supervisão humana", ["risk_register", "audit"]),
        ("Governança de dados", ["dlp", "retention", "evidence_policy"]),
        ("Registro / manutenção de logs", ["audit"]),
        ("Precisão e robustez", ["grounding", "verifier"]),
        ("Cibersegurança", ["prompt_guard", "federation_guard"]),
    ],
    "NIST AI RMF": [
        ("Govern — políticas e papéis", ["rbac", "opa"]),
        ("Map — contexto e risco", ["risk_register", "model_cards"]),
        ("Measure — avaliação e métricas", ["verifier", "audit"]),
        ("Manage — mitigação e monitoramento", ["circuit_breaker", "prompt_guard", "dlp"]),
    ],
    "ISO/IEC 42001": [
        ("Política de IA", ["opa", "rbac"]),
        ("Papéis e responsabilidades", ["rbac"]),
        ("Avaliação de risco de IA", ["risk_register"]),
        ("Controles operacionais", ["grounding", "prompt_guard", "dlp"]),
        ("Monitoramento e melhoria", ["audit", "verifier"]),
    ],
    "LGPD": [
        ("Direito ao esquecimento (Art. 18)", ["forget"]),
        ("Retenção e minimização", ["retention", "dlp"]),
        ("Segurança do tratamento", ["prompt_guard", "federation_guard"]),
        ("Registro das operações de tratamento", ["audit"]),
    ],
    "OWASP LLM Top 10": [
        ("LLM01 — Prompt injection", ["prompt_guard"]),
        ("LLM02/05 — Saída/output insegura", ["verifier", "grounding"]),
        ("LLM06 — Vazamento de dados sensíveis", ["dlp"]),
        ("LLM08 — Excesso de agência", ["risk_register", "rbac"]),
        ("LLM09 — Dependência excessiva / alucinação", ["grounding", "verifier"]),
    ],
}


def _controls() -> dict:
    s = get_settings()
    return {
        "grounding": _flag("grounding_strict", True),
        "prompt_guard": _flag("prompt_guard_enabled", True),
        "dlp": _flag("dlp_enabled", True),
        "verifier": _flag("verifier_v2_enabled", False),
        "circuit_breaker": _flag("circuit_breaker_enabled", True),
        "opa": _flag("opa_enabled", False),
        "retention": int(getattr(s, "interactions_retention_days", 0) or 0) > 0,
        "forget": True,
        "audit": True,
        "rbac": True,
        "model_cards": True,
        "risk_register": True,
        "federation_guard": True,
        "evidence_policy": True,
    }


@router.get("/crosswalk")
async def compliance_crosswalk(user=Depends(_gate)):
    controls = _controls()
    frameworks = []
    for name, reqs in _FRAMEWORKS.items():
        rows, covered = [], 0
        for req, keys in reqs:
            satisfied = [k for k in keys if controls.get(k)]
            is_cov = bool(satisfied)
            covered += 1 if is_cov else 0
            rows.append({"requirement": req, "controls": keys, "covered": is_cov, "satisfied_by": satisfied})
        frameworks.append({
            "framework": name, "covered": covered, "total": len(reqs),
            "pct": round(covered / len(reqs) * 100) if reqs else 0,
            "requirements": rows,
        })
    return {"frameworks": frameworks, "controls": controls, "control_labels": _CONTROL_LABELS}


# ── Attestation (sign-off) + papéis DPO/AI Officer + relatório exportável ─────
_OFFICES = {"dpo": "DPO — Encarregado de Dados (LGPD)", "ai_officer": "AI Officer"}


class OfficerAssign(BaseModel):
    office: str
    user_id: str


class AttestationCreate(BaseModel):
    scope: str = "platform"      # platform | agent | pipeline
    entity_id: str = ""
    office: str = ""
    statement: str


async def _user_name(uid: str | None) -> str:
    if not uid:
        return "—"
    u = await users_repo.find_by_id(uid)
    return (u.get("display_name") or u.get("username") or uid) if u else uid


@router.get("/officers")
async def officers_list(user=Depends(_gate)):
    rows = await governance_officer_repo.find_all(limit=200)
    assigned = [{
        "id": r.get("id"), "office": r.get("office"), "user_id": r.get("user_id"),
        "name": await _user_name(r.get("user_id")),
        "assigned_by": r.get("assigned_by"), "assigned_at": str(r.get("assigned_at") or ""),
    } for r in rows]
    return {"offices": [{"office": k, "label": v} for k, v in _OFFICES.items()], "assigned": assigned}


@router.post("/officers", status_code=201)
async def officer_assign(data: OfficerAssign, user=Depends(_gate)):
    if data.office not in _OFFICES:
        raise HTTPException(422, "papel inválido (dpo|ai_officer)")
    if not data.user_id:
        raise HTTPException(422, "usuário obrigatório")
    if await governance_officer_repo.find_all(limit=1, office=data.office, user_id=data.user_id):
        raise HTTPException(409, "usuário já designado para este papel")
    who = (user or {}).get("username") or "?"
    oid = str(uuid.uuid4())
    await governance_officer_repo.create({"id": oid, "office": data.office, "user_id": data.user_id, "assigned_by": who})
    await audit_repo.create({
        "entity_type": "governance_officer", "entity_id": data.user_id,
        "action": f"officer_assigned:{data.office}", "actor": who,
    })
    return {"id": oid, "message": "Papel designado"}


@router.delete("/officers/{officer_id}")
async def officer_remove(officer_id: str, user=Depends(_gate)):
    if not await governance_officer_repo.delete(officer_id):
        raise HTTPException(404)
    return {"message": "Designação removida"}


@router.get("/attestations")
async def attestations_list(user=Depends(_gate)):
    rows = await governance_attestation_repo.find_all(limit=100)
    rows.sort(key=lambda r: str(r.get("signed_at") or ""), reverse=True)
    return {"attestations": [{
        "id": r.get("id"), "scope": r.get("scope"), "entity_id": r.get("entity_id") or "",
        "office": r.get("office") or "", "statement": r.get("statement") or "",
        "signed_by": r.get("signed_by"), "signed_at": str(r.get("signed_at") or ""),
    } for r in rows]}


@router.post("/attestations", status_code=201)
async def attestation_sign(data: AttestationCreate, user=Depends(_gate)):
    if data.scope not in ("platform", "agent", "pipeline"):
        raise HTTPException(422, "escopo inválido (platform|agent|pipeline)")
    if not (data.statement or "").strip():
        raise HTTPException(422, "a declaração é obrigatória")
    who = (user or {}).get("username") or (user or {}).get("display_name") or "?"
    aid = str(uuid.uuid4())
    await governance_attestation_repo.create({
        "id": aid, "scope": data.scope, "entity_id": data.entity_id or None,
        "office": data.office or None, "statement": data.statement.strip(), "signed_by": who,
    })
    await audit_repo.create({
        "entity_type": "governance_attestation", "entity_id": data.entity_id or data.scope,
        "action": "attestation_signed", "actor": who,
    })
    return {"id": aid, "message": "Prontidão assinada"}


@router.get("/report")
async def compliance_report(user=Depends(_gate)):
    """Relatório de conformidade consolidado (para exportar ao auditor)."""
    score, pillars = _posture()
    controls = _controls()
    crosswalk = []
    for name, reqs in _FRAMEWORKS.items():
        covered = sum(1 for _, keys in reqs if any(controls.get(k) for k in keys))
        crosswalk.append({"framework": name, "covered": covered, "total": len(reqs),
                          "pct": round(covered / len(reqs) * 100) if reqs else 0})
    agents = await agents_repo.find_all(limit=500)
    classifications = {c.get("entity_id"): c for c in await governance_risk_repo.find_all(limit=1000, entity_type="agent")}
    risk = Counter()
    for a in agents:
        cls = classifications.get(a.get("id"))
        risk[(cls.get("tier") if cls else None) or "unclassified"] += 1
    officers = await governance_officer_repo.find_all(limit=200)
    attests = await governance_attestation_repo.find_all(limit=20)
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "posture_score": score,
        "pillars": pillars,
        "capabilities": controls,
        "frameworks": crosswalk,
        "risk": dict(risk),
        "officers": [{"office": o.get("office"), "user_id": o.get("user_id"),
                      "name": await _user_name(o.get("user_id"))} for o in officers],
        "attestations": [{"scope": a.get("scope"), "office": a.get("office"),
                          "statement": a.get("statement"), "signed_by": a.get("signed_by"),
                          "signed_at": str(a.get("signed_at") or "")} for a in attests],
    }
