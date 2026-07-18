"""Módulo IA Responsável (56.0.0) — governança, confiança e conformidade.

Fase 1: consolida num painel único o que hoje está espalhado ou headless —
postura computada das flags REAIS, trilha de auditoria e eventos de segurança
do `audit_log`. Model cards, registro de risco e crosswalk de frameworks são
Fase 2/3 (marcados como roadmap na UI; nada de número inventado aqui).

Todas as rotas são gated por require_role("root","admin"), que — desde 56.0.0 —
inclui o perfil "governanca" (herda os poderes de Admin).
"""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends

from app.core.auth import require_role
from app.core.config import get_settings
from app.core.database import audit_repo

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
            ("Model cards publicados (Fase 2)", False),
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
