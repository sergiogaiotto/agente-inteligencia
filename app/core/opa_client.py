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
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Cria/retorna o AsyncClient singleton."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            settings = get_settings()
            _client = httpx.AsyncClient(
                base_url=settings.opa_url,
                timeout=httpx.Timeout(settings.opa_timeout_seconds),
            )
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


async def close():
    """Fecha o cliente HTTP. Chamado no shutdown do app."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
