"""Cliente Python para o OPA (Onda 4a — Policy as Code).

Usa httpx.AsyncClient (já instrumentado pelo OTel da Onda 2). Cada chamada
gera span `policy.evaluate` com atributos da decisão e entry no audit_log.

Failsafe configurável:
- `opa_enabled=False`: retorna sempre `allow=True` (no-op). Comportamento de
  hoje (pré-Onda 4a).
- `opa_enabled=True` E OPA respondendo: decisão real.
- `opa_enabled=True` E OPA offline E `opa_failsafe_open=True`: retorna `allow=True`
  com warning + audit. Default em dev.
- `opa_enabled=True` E OPA offline E `opa_failsafe_open=False`: retorna `allow=False`.
  Recomendado em produção com dados sensíveis.

Backward compat: módulo é totalmente independente — se nenhum chamador o usar,
nada muda.
"""
from __future__ import annotations

import asyncio
import logging
import json
import time
from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)

# Singleton lazy do cliente HTTP. Reusa conexões — OPA é endpoint local frequente.
_client: Optional[httpx.AsyncClient] = None
_client_timeout: Optional[float] = None  # timeout com que o _client foi construído
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Cria/retorna o AsyncClient singleton.

    O timeout é fixado na construção do httpx.AsyncClient. Como o cockpit pode
    alterar `opa_timeout_seconds` em runtime (PUT /opa/config → apply_settings_to_env),
    reconstrói o cliente quando o timeout efetivo muda — senão o valor novo só
    valeria após restart do processo.
    """
    global _client, _client_timeout
    settings = get_settings()
    timeout = settings.opa_timeout_seconds
    if _client is not None and _client_timeout == timeout:
        return _client
    async with _client_lock:
        if _client is not None and _client_timeout == timeout:
            return _client
        if _client is not None:  # timeout mudou → fecha o antigo antes de recriar
            old, _client = _client, None
            try:
                await old.aclose()
            except Exception:
                pass
        _client = httpx.AsyncClient(
            base_url=settings.opa_url,
            timeout=httpx.Timeout(timeout),
        )
        _client_timeout = timeout
        return _client


async def evaluate(
    package: str,
    rule: str = "allow",
    input_doc: Optional[dict] = None,
    *,
    audit: bool = True,
) -> dict[str, Any]:
    """Avalia uma rule contra OPA.

    Args:
        package: nome do pacote Rego (ex: "interaction", "tool_invocation").
        rule: rule a avaliar (default "allow").
        input_doc: documento que o OPA recebe como `input`. Pode ser None.
        audit: se True, registra a decisão no audit_log (entity_type=policy_decision).

    Returns:
        {
          "allow":       bool,
          "result":      <valor cru retornado pelo OPA, podendo ser bool|list|dict>,
          "duration_ms": int,
          "source":      "opa" | "failsafe_open" | "failsafe_closed" | "disabled",
        }

    Nunca propaga exceção. OPA offline + failsafe vira `source` específico
    para o caller saber distinguir decisão real de fallback.
    """
    settings = get_settings()
    started = time.perf_counter()

    with _tracer.start_as_current_span("policy.evaluate") as span:
        span.set_attribute("policy.package", package)
        span.set_attribute("policy.rule", rule)

        # ── Toggle global: OPA desabilitado é no-op (allow=True) ─────
        if not settings.opa_enabled:
            span.set_attribute("policy.source", "disabled")
            return {
                "allow": True,
                "result": True,
                "duration_ms": 0,
                "source": "disabled",
            }

        body = {"input": input_doc or {}}
        url = f"/v1/data/{package}/{rule}"

        try:
            client = await _get_client()
            r = await client.post(url, json=body)
            r.raise_for_status()
            payload = r.json()
            # OPA retorna {"result": ...} — pode ser bool, list, dict, etc.
            result = payload.get("result")
            allow = bool(result) if isinstance(result, bool) else result is not None and result is not False
            duration_ms = int((time.perf_counter() - started) * 1000)
            span.set_attribute("policy.allow", allow)
            span.set_attribute("policy.duration_ms", duration_ms)
            span.set_attribute("policy.source", "opa")

            decision = {
                "allow": allow,
                "result": result,
                "duration_ms": duration_ms,
                "source": "opa",
            }
            if audit:
                await _audit(package, rule, input_doc, decision)
            return decision

        except (httpx.HTTPError, httpx.HTTPStatusError, json.JSONDecodeError) as e:
            duration_ms = int((time.perf_counter() - started) * 1000)
            failsafe_allow = settings.opa_failsafe_open
            source = "failsafe_open" if failsafe_allow else "failsafe_closed"
            logger.warning(
                f"OPA falhou ({type(e).__name__}: {str(e)[:120]}); "
                f"failsafe-{'open' if failsafe_allow else 'closed'} → allow={failsafe_allow}"
            )
            span.set_attribute("policy.allow", failsafe_allow)
            span.set_attribute("policy.duration_ms", duration_ms)
            span.set_attribute("policy.source", source)
            span.set_attribute("policy.error", str(e)[:200])

            decision = {
                "allow": failsafe_allow,
                "result": None,
                "duration_ms": duration_ms,
                "source": source,
                "error": str(e)[:200],
            }
            if audit:
                await _audit(package, rule, input_doc, decision)
            return decision


async def evaluate_value(
    package: str,
    rule: str,
    input_doc: Optional[dict] = None,
) -> Any:
    """Atalho: retorna apenas o valor cru do `result` (sem wrapper de decision).

    Útil para rules que retornam coleções/strings (ex: `interaction.reasons`).
    OPA offline → None. OPA enabled=False → None.
    """
    settings = get_settings()
    if not settings.opa_enabled:
        return None
    body = {"input": input_doc or {}}
    url = f"/v1/data/{package}/{rule}"
    try:
        client = await _get_client()
        r = await client.post(url, json=body)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        logger.warning(f"OPA evaluate_value({package}.{rule}) falhou: {e}")
        return None


async def _audit(package: str, rule: str, input_doc: Optional[dict], decision: dict) -> None:
    """Registra a decisão em audit_log. Nunca propaga erro (audit best-effort)."""
    try:
        # Import lazy para evitar ciclo (database importa otel; opa_client importa otel).
        from app.core.database import audit_repo
        await audit_repo.create({
            "entity_type": "policy_decision",
            "entity_id": f"{package}.{rule}",
            "action": "allow" if decision["allow"] else "deny",
            "details": json.dumps({
                "package": package,
                "rule": rule,
                "input": input_doc or {},
                "decision": {
                    "allow": decision["allow"],
                    "source": decision["source"],
                    "duration_ms": decision["duration_ms"],
                },
            })[:8000],  # limita payload — audit_log.details é TEXT
        })
    except Exception as e:
        # Audit é nice-to-have. Não bloqueia decisão por causa de DB problema.
        logger.warning(f"audit policy_decision falhou: {e}")


# ════════════════════════════════════════════════════════════════════
# Resolução do usuário atuante para os PEPs do engine (62.0.0).
#
# Substitui os valores HARDCODED que o engine passava ao OPA (user.status
# "active" e user.role "operator"), que faziam tools sensitivity:high serem
# filtradas em silêncio ao ligar o OPA. O vocabulário RBAC da plataforma
# (root/admin/governanca/comum) é um namespace DIFERENTE do vocabulário OPA
# (viewer/operator/admin) → mapeamento explícito abaixo.
# ════════════════════════════════════════════════════════════════════

# Papel RBAC da plataforma → papel do OPA (política tool_invocation.rego).
# root/admin/governanca herdam poderes de admin (auth.require_role) → "admin".
# comum é usuário interno autenticado → "operator" (libera tools low+medium).
_ROLE_TO_OPA = {
    "root": "admin",
    "admin": "admin",
    "governanca": "admin",
    "comum": "operator",
}


# Vocabulário de sensibilidade das tools (UI: public/internal/confidential/restricted,
# igual à confidencialidade das fontes) → tiers da tool_invocation.rego (low/medium/high).
# Sem este mapa, ligar o OPA nega TODA tool classificada (a rego só conhece low/medium/high).
# Valores já-tier (low/medium/high) passam direto (retrocompat).
_SENSITIVITY_TO_TIER = {
    "public": "low", "internal": "low", "confidential": "medium",
    "restricted": "high", "secret": "high",  # 'secret' = alias de 'restricted' (topo, = evidence.rego)
    "low": "low", "medium": "medium", "high": "high",
}


def map_tool_sensitivity_to_tier(sensitivity: Optional[str]) -> str:
    """Sensibilidade da tool → tier da tool_invocation.rego.

    AUSENTE/vazio → 'low' (tool não-classificada = usável; retrocompat). Rótulo
    PRESENTE mas desconhecido (typo, 'sigiloso', 'critical') → 'high' = FAIL-CLOSED:
    o curador quis classificar; um label não-reconhecido NÃO pode virar 'liberado
    p/ todos' (red-team: rotular de 'secret' abria a tool ao piso). Ver [[project_opa_cockpit_handoff]]."""
    s = (sensitivity or "").strip().lower()
    if not s:
        return "low"                            # não classificada → usável
    return _SENSITIVITY_TO_TIER.get(s, "high")   # presente-mas-desconhecido → fail-closed


def map_platform_role_to_opa(role: Optional[str]) -> str:
    """Mapeia o papel RBAC da plataforma para o vocabulário do OPA.

    Papel desconhecido/ausente → "operator" — preserva o comportamento que o
    engine tinha hardcoded (nenhuma regressão para chamadas sem platform-user
    real); só usuários privilegiados sobem para "admin" e destravam tools high.
    """
    return _ROLE_TO_OPA.get((role or "").strip().lower(), "operator")


async def resolve_opa_user(owner_user_id: Optional[str]) -> dict[str, str]:
    """Contexto REAL do usuário atuante (status + papel OPA + clearance) para os PEPs.

    Faz no máximo 1 lookup por PK (`users_repo.find_by_id`) e reusa o mesmo row para
    status, papel e clearance (o "no read up" de evidência — 64.0.0). Sem platform-user
    atuante (owner None — ex.: invoke via API key ou chat de cliente-final externo) OU
    lookup falho → default seguro e NÃO-regressivo: status "active", papel "operator",
    clearance "internal" (= nível default das fontes). Nunca propaga exceção.
    """
    uid = (owner_user_id or "").strip()
    if not uid:
        return {"status": "active", "role": "operator", "clearance": "internal"}
    row = None
    try:
        from app.core.database import users_repo  # import tardio: evita ciclo
        row = await users_repo.find_by_id(uid)
    except Exception as e:
        logger.warning(f"resolve_opa_user({uid[:12]}) lookup falhou: {e}")
    if not row:
        return {"status": "active", "role": "operator", "clearance": "internal"}
    status = (str(row.get("status") or "active")).strip().lower() or "active"
    clearance = (str(row.get("clearance") or "internal")).strip().lower() or "internal"
    return {"status": status, "role": map_platform_role_to_opa(row.get("role")), "clearance": clearance}


# ════════════════════════════════════════════════════════════════════
# Helpers do cockpit OPA (62.0.0) — read/simulate. Todos IGNORAM o toggle
# opa_enabled (o servidor OPA roda no perfil default do compose, sempre de pé)
# e NUNCA propagam exceção. `simulate` NÃO audita (é what-if, efeito zero).
# ════════════════════════════════════════════════════════════════════
async def server_health() -> bool:
    """True se o servidor OPA responde em /health. Independe de opa_enabled."""
    try:
        client = await _get_client()
        r = await client.get("/health")
        return r.status_code == 200
    except Exception:
        return False


async def list_policies() -> Optional[list[dict]]:
    """Políticas carregadas no OPA (GET /v1/policies → result[].id/.raw).

    Retorna a lista crua do OPA, ou None em falha (caller cai no fallback de
    disco). Independe de opa_enabled.
    """
    try:
        client = await _get_client()
        r = await client.get("/v1/policies")
        r.raise_for_status()
        return r.json().get("result") or []
    except Exception as e:
        logger.warning(f"OPA list_policies falhou: {e}")
        return None


async def simulate(package: str, rule: str = "allow", input_doc: Optional[dict] = None) -> dict:
    """What-if: avalia uma rule DIRETO no OPA, ignorando opa_enabled e SEM audit.

    Retorna {allow, result, duration_ms, source}. `allow` é None e source="error"
    quando o OPA não responde (o cockpit distingue erro de deny real).
    """
    started = time.perf_counter()
    body = {"input": input_doc or {}}
    url = f"/v1/data/{package}/{rule}"
    try:
        client = await _get_client()
        r = await client.post(url, json=body)
        r.raise_for_status()
        result = r.json().get("result")
        allow = result is not None and result is not False
        return {
            "allow": allow,
            "result": result,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "source": "opa",
        }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
        return {
            "allow": None,
            "result": None,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "source": "error",
            "error": str(e)[:200],
        }


async def push_policy(policy_id: str, rego: str) -> dict:
    """Cria/substitui uma política no OPA: PUT /v1/policies/{id} (63.0.0 Fase B).

    O OPA COMPILA no PUT — Rego inválido → 400 com {code, message, errors}. Usar
    o MESMO id do baked substitui a política (não duplica). Independe de opa_enabled
    (é gestão, não avaliação) e nunca propaga exceção. Retorna {ok, kind, error}
    com kind ∈ {"ok","rejected","unreachable"} — o caller distingue Rego inválido
    (rejected) de OPA fora do ar (unreachable), que exigem mensagens diferentes.
    """
    try:
        client = await _get_client()
        r = await client.put(
            f"/v1/policies/{policy_id}",
            content=rego.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        if r.status_code == 200:
            return {"ok": True, "kind": "ok", "error": None}
        try:
            body = r.json()
            msg = body.get("message") or json.dumps(body.get("errors") or body)
        except Exception:
            msg = r.text[:400]
        return {"ok": False, "kind": "rejected", "error": str(msg)[:400]}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "kind": "unreachable", "error": f"{type(e).__name__}: {str(e)[:200]}"}


async def get_policy(policy_id: str) -> Optional[str]:
    """Raw da política atualmente no OPA (GET /v1/policies/{id}.result.raw).

    Snapshot para compensar uma persistência falha — o OPA nunca deve ficar com
    uma mudança não registrada. None se ausente/fora do ar. Independe de opa_enabled.
    """
    try:
        client = await _get_client()
        r = await client.get(f"/v1/policies/{policy_id}")
        if r.status_code == 200:
            return (r.json().get("result") or {}).get("raw")
        return None
    except Exception:
        return None


async def close():
    """Fecha o cliente HTTP. Chamado no shutdown do app."""
    global _client, _client_timeout
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
            _client_timeout = None
