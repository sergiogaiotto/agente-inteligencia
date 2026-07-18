"""Módulo IA Responsável (56.0.0) — governança, confiança e conformidade.

Fase 1: consolida num painel único o que hoje está espalhado ou headless —
postura computada das flags REAIS, trilha de auditoria e eventos de segurança
do `audit_log`. Model cards, registro de risco e crosswalk de frameworks são
Fase 2/3 (marcados como roadmap na UI; nada de número inventado aqui).

Todas as rotas são gated por require_role("root","admin"), que — desde 56.0.0 —
inclui o perfil "governanca" (herda os poderes de Admin).
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.config import get_settings, apply_settings_to_env
from app.core.database import (
    audit_repo, agents_repo, skills_repo, governance_risk_repo, settings_store,
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
        # status das capacidades (algumas hoje só env/DB — o painel as revela).
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


def _audit_row(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "action": r.get("action"),
        "entity_type": r.get("entity_type"),
        "entity_id": r.get("entity_id"),
        "actor": r.get("actor"),
        "ip": r.get("ip"),
        "created_at": str(r.get("created_at") or ""),
    }


@router.get("/audit")
async def governance_audit(limit: int = 50, user=Depends(_gate)):
    """Trilha de auditoria (audit_log) — mais recentes primeiro."""
    rows = await audit_repo.find_all(limit=min(max(limit, 1), 200))
    rows = sorted(rows, key=lambda r: r.get("id") or 0, reverse=True)
    return {"events": [_audit_row(r) for r in rows[:limit]]}


def _is_security_action(action: str | None) -> bool:
    a = (action or "").lower()
    return ("injection" in a) or ("->refuse" in a) or ("->escalate" in a) or ("policy" in a)


@router.get("/security-events")
async def governance_security_events(limit: int = 50, user=Depends(_gate)):
    """Eventos de segurança derivados do audit_log: injeções bloqueadas,
    recusas e escalonamentos (sinais da defesa-em-profundidade em ação)."""
    rows = await audit_repo.find_all(limit=1000)
    counts: Counter = Counter()
    for r in rows:
        a = (r.get("action") or "").lower()
        if "injection" in a:
            counts["injecoes_bloqueadas"] += 1
        if "->refuse" in a:
            counts["recusas"] += 1
        if "->escalate" in a:
            counts["escalonamentos"] += 1
    sec = [r for r in rows if _is_security_action(r.get("action"))]
    sec = sorted(sec, key=lambda r: r.get("id") or 0, reverse=True)
    return {
        "counts": dict(counts),
        "scanned": len(rows),
        "events": [_audit_row(r) for r in sec[:limit]],
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


def _suggested_tier(agent: dict) -> str:
    """Sugestão (humano decide) a partir dos sinais de risco derivados dos campos
    reais do agente — sem parsear a skill (os sinais de ATENÇÃO não dependem dela)."""
    warns = 0
    if _truthy(agent.get("allow_general_knowledge")):
        warns += 1
    if not _truthy(agent.get("require_evidence")):
        warns += 1
    if not agent.get("skill_id"):
        warns += 1
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
