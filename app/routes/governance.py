"""Módulo IA Responsável (56.0.0) — governança, confiança e conformidade.

Fase 1: consolida num painel único o que hoje está espalhado ou headless —
postura computada das flags REAIS, trilha de auditoria e eventos de segurança
do `audit_log`. Model cards, registro de risco e crosswalk de frameworks são
Fase 2/3 (marcados como roadmap na UI; nada de número inventado aqui).

Todas as rotas são gated por require_role("root","admin"), que — desde 56.0.0 —
inclui o perfil "governanca" (herda os poderes de Admin).
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.config import get_settings, apply_settings_to_env
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


# ── Policy-as-code (OPA) — cockpit read/simulate (62.0.0) ─────────────────────
# Traz o motor de políticas Rego (Onda 4a, até aqui headless) à UI: status +
# toggle (enabled/failsafe/timeout) + visualização das políticas + simulador
# what-if + log de decisões. Edição persistente das regras é Fase B (roadmap).
_OPA_PACKAGES = ("interaction", "tool_invocation", "evidence")
# `evidence` existe mas está dormente (sem call site; users.clearance não é
# coluna) — o cockpit a marca como não-wired.
_OPA_WIRED = ("interaction", "tool_invocation")
_OPA_POLICIES_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "opa" / "policies"


def _opa_pkg_from_id(pid: str) -> str:
    """OPA devolve id tipo "policies/interaction.rego" → extrai "interaction"."""
    base = (pid or "").rsplit("/", 1)[-1]
    return base[:-5] if base.endswith(".rego") else base


class OpaConfig(BaseModel):
    opa_enabled: bool | None = None
    opa_failsafe_open: bool | None = None
    opa_timeout_seconds: float | None = None


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
        "server_ok": await opa_client.server_health(),
    }


@router.get("/opa/policies")
async def opa_policies(user=Depends(_gate)):
    """Políticas Rego carregadas (via OPA); fallback lendo o disco se OPA off/down."""
    from app.core import opa_client
    result = await opa_client.list_policies()
    policies = []
    if result is not None:
        source = "opa"
        for p in result:
            pkg = _opa_pkg_from_id(p.get("id", ""))
            policies.append({
                "id": p.get("id", ""), "package": pkg,
                "raw": p.get("raw", ""), "wired": pkg in _OPA_WIRED,
            })
    else:
        source = "disk"
        for f in sorted(_OPA_POLICIES_DIR.glob("*.rego")):
            try:
                raw = f.read_text(encoding="utf-8")
            except Exception:
                raw = ""
            policies.append({"id": f"policies/{f.name}", "package": f.stem,
                             "raw": raw, "wired": f.stem in _OPA_WIRED})
    # wired primeiro, depois alfabético.
    policies.sort(key=lambda x: (x["package"] not in _OPA_WIRED, x["package"]))
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
