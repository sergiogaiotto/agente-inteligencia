"""Engine declarativo — executa agents com execution_mode=declarative
sem LLM. Lê ## API Bindings do SKILL.md, resolve templates Jinja2
sandboxed, chama APIs via conectores, aplica output_mapping (jsonpath-ng)
e emite ContextDelta append-only.

Fase 2 — single binding sem DAG. Paralelismo e depends_on ficam para Fase 3.
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any

import httpx
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from jsonpath_ng.ext import parse as jsonpath_parse

from app.a2a.protocol import ContextDelta, apply_context_delta
from app.core.database import api_connectors_repo, api_call_logs_repo

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS = 30000
_DEFAULT_MAX_OUTPUT_BYTES = 4096
_MAX_CONTEXT_BYTES = 65536
_RETRYABLE_METHODS = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
_IDEMPOTENT_REQUIRED_METHODS = {"POST", "PATCH", "DELETE"}

_jinja_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


# ═══════════════════════════════════════════════════════
# Templating — Jinja2 sandboxed
# ═══════════════════════════════════════════════════════

def _render(template: Any, scope: dict) -> Any:
    if not isinstance(template, str):
        return template
    if "{{" not in template and "{%" not in template:
        return template
    return _jinja_env.from_string(template).render(**scope)


def _render_deep(value: Any, scope: dict) -> Any:
    if isinstance(value, str):
        return _render(value, scope)
    if isinstance(value, dict):
        return {k: _render_deep(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_deep(v, scope) for v in value]
    return value


# ═══════════════════════════════════════════════════════
# Context path helpers
# ═══════════════════════════════════════════════════════

def _set_dotted(target: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = target
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _apply_output_mapping(
    response_data: Any,
    mapping: list,
    bytes_budget: int,
) -> tuple[dict, list[str]]:
    additions: dict = {}
    errors: list[str] = []
    used = 0
    for m in mapping or []:
        if not isinstance(m, dict):
            errors.append(f"output_mapping item não é objeto: {m}")
            continue
        src = m.get("from")
        dst = m.get("to")
        max_bytes = int(m.get("max_bytes", _DEFAULT_MAX_OUTPUT_BYTES))
        if not src or not dst:
            errors.append(f"output_mapping inválido (from/to ausente): {m}")
            continue
        try:
            expr = jsonpath_parse(src)
            matches = [x.value for x in expr.find(response_data)]
        except Exception as e:
            errors.append(f"JSONPath inválido '{src}': {e}")
            continue
        if not matches:
            errors.append(f"JSONPath '{src}' não encontrou valor")
            continue
        val = matches[0] if len(matches) == 1 else matches
        serialized = json.dumps(val, ensure_ascii=False, default=str)
        size = len(serialized.encode("utf-8"))
        if size > max_bytes:
            errors.append(f"Valor em '{dst}' ({size}B) excede max_bytes={max_bytes}")
            continue
        used += size
        if used > bytes_budget:
            errors.append("Orçamento de contexto esgotado — mapeamento truncado")
            break
        _set_dotted(additions, dst, val)
    return additions, errors


# ═══════════════════════════════════════════════════════
# Connector & auth
# ═══════════════════════════════════════════════════════

async def _resolve_connector(ref: str) -> dict | None:
    """Resolve por name primeiro, depois por id."""
    all_conns = await api_connectors_repo.find_all(limit=500)
    for c in all_conns:
        if c.get("name") == ref or c.get("id") == ref:
            return c
    return None


def _build_auth_headers(connector: dict) -> dict:
    """Monta headers de autenticação a partir do connector registry.

    Secrets NUNCA são expostos ao templating — só entram no header
    da request aqui. Replicado de app/routes/api_connectors.py para
    evitar import circular.
    """
    headers = {"Content-Type": "application/json"}
    auth_type = connector.get("auth_type", "none")
    api_key = connector.get("api_key", "") or ""
    if not api_key:
        return headers
    header_name = connector.get("auth_header", "X-API-Key")
    if auth_type == "api_key":
        headers[header_name] = api_key
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "basic":
        headers["Authorization"] = f"Basic {base64.b64encode(api_key.encode()).decode()}"
    return headers


def _redact_headers(headers: dict) -> dict:
    sensitive = {"authorization", "x-api-key", "cookie", "set-cookie", "idempotency-key"}
    return {
        k: ("<redacted>" if k.lower() in sensitive else v)
        for k, v in (headers or {}).items()
    }


# ═══════════════════════════════════════════════════════
# HTTP execution
# ═══════════════════════════════════════════════════════

async def _execute_http_call(
    connector: dict,
    method: str,
    path: str,
    headers: dict,
    query: dict,
    body: Any,
    timeout_s: float,
) -> httpx.Response:
    base = connector.get("base_url", "").rstrip("/")
    url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    auth = _build_auth_headers(connector)
    final_headers = {**auth, **(headers or {})}
    method_u = method.upper()
    async with httpx.AsyncClient(timeout=timeout_s, headers=final_headers) as client:
        if method_u == "GET":
            return await client.get(url, params=query or None)
        if method_u == "POST":
            return await client.post(url, json=body, params=query or None)
        if method_u == "PUT":
            return await client.put(url, json=body, params=query or None)
        if method_u == "PATCH":
            return await client.patch(url, json=body, params=query or None)
        if method_u == "DELETE":
            return await client.delete(url, params=query or None)
        if method_u == "HEAD":
            return await client.head(url, params=query or None)
        raise ValueError(f"Método HTTP não suportado: {method}")


async def _call_with_retry(
    connector: dict,
    method: str,
    path: str,
    headers: dict,
    query: dict,
    body: Any,
    timeout_s: float,
    max_retries: int,
    retry_on: set,
    backoff: str,
) -> tuple[httpx.Response | None, str | None, int]:
    """Retorna (response|None, last_error|None, attempts)."""
    attempts = 0
    last_error: str | None = None
    while attempts <= max_retries:
        attempts += 1
        try:
            resp = await _execute_http_call(connector, method, path, headers, query, body, timeout_s)
            retry_needed = (
                500 <= resp.status_code < 600 and "5xx" in retry_on
                and attempts <= max_retries
            )
            if retry_needed:
                await _sleep_backoff(backoff, attempts)
                continue
            return resp, None, attempts
        except httpx.TimeoutException as e:
            last_error = f"timeout: {e}"
            if "timeout" in retry_on and attempts <= max_retries:
                await _sleep_backoff(backoff, attempts)
                continue
            return None, last_error, attempts
        except httpx.NetworkError as e:
            last_error = f"network: {e}"
            if "network" in retry_on and attempts <= max_retries:
                await _sleep_backoff(backoff, attempts)
                continue
            return None, last_error, attempts
        except Exception as e:
            return None, f"unexpected: {e}", attempts
    return None, last_error, attempts


async def _sleep_backoff(mode: str, attempt: int) -> None:
    if mode == "exponential":
        await asyncio.sleep(min(0.2 * (2 ** attempt), 5.0))
    else:
        await asyncio.sleep(0.2)


# ═══════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════

async def execute_declarative(
    agent: dict,
    skill_parsed: Any,
    inputs: dict | None = None,
    context: dict | None = None,
    session_id: str | None = None,
) -> dict:
    """Executa um agente no modo declarativo.

    Fase 2: bindings rodam em ordem sequencial (sem DAG, sem paralelismo).
    Cada binding pode ler variáveis de contextos já escritos por bindings
    anteriores via {{ context.* }} em templates.
    """
    start = time.time()
    inputs = inputs or {}
    context = dict(context or {})
    trace_id = session_id or str(uuid.uuid4())

    scope = {
        "inputs": inputs,
        "context": context,
        "session_id": trace_id,
    }

    bindings = list(getattr(skill_parsed, "api_bindings_parsed", []) or [])
    if not bindings:
        return {
            "interaction_id": trace_id,
            "agent_id": agent.get("id", ""),
            "output": "",
            "final_state": "failed",
            "context": context,
            "bindings_executed": [],
            "errors": ["Nenhum API Binding encontrado no SKILL.md"],
            "duration_ms": round((time.time() - start) * 1000, 2),
            "mode": "declarative",
        }

    executed: list[dict] = []
    errors: list[str] = []

    for binding in bindings:
        binding_id = binding.get("id", "?")
        on_failure = binding.get("on_failure", "fail")

        step_error = await _run_binding(
            binding=binding,
            scope=scope,
            context=context,
            agent=agent,
            skill_parsed=skill_parsed,
            executed=executed,
        )
        if step_error:
            errors.append(step_error)
            if on_failure == "fail":
                break
        # scope['context'] já é o mesmo dict de context, mutado em _run_binding

    duration_ms = round((time.time() - start) * 1000, 2)

    if errors and not any(e.get("status", 0) >= 200 and e.get("status", 0) < 300 for e in executed):
        final_state = "failed"
    elif errors:
        final_state = "partial"
    else:
        final_state = "completed"

    answer_payload = {
        "bindings_executed": executed,
        "errors": errors,
        "context_keys": [k for k in context.keys() if not k.startswith("_")],
    }

    return {
        "interaction_id": trace_id,
        "agent_id": agent.get("id", ""),
        "output": json.dumps(answer_payload, ensure_ascii=False, indent=2),
        "final_state": final_state,
        "context": context,
        "bindings_executed": executed,
        "errors": errors,
        "duration_ms": duration_ms,
        "mode": "declarative",
    }


async def _run_binding(
    binding: dict,
    scope: dict,
    context: dict,
    agent: dict,
    skill_parsed: Any,
    executed: list[dict],
) -> str | None:
    binding_id = binding.get("id", "?")
    connector_ref = binding.get("connector")
    if not connector_ref:
        return f"[{binding_id}] campo 'connector' ausente"

    connector = await _resolve_connector(connector_ref)
    if not connector:
        return f"[{binding_id}] connector '{connector_ref}' não encontrado no registry"

    resilience = binding.get("resilience") or {}
    timeout_ms = int(resilience.get("timeout_ms") or connector.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)
    timeout_s = timeout_ms / 1000.0
    retry_cfg = resilience.get("retry") or {}
    max_retries = int(retry_cfg.get("max", 0))
    retry_on = set(retry_cfg.get("on") or [])
    backoff = retry_cfg.get("backoff", "fixed")

    method = (binding.get("method") or "GET").upper()

    try:
        path = _render(binding.get("path", "/"), scope)
        headers = _render_deep(binding.get("headers") or {}, scope)
        query = _render_deep(binding.get("query") or {}, scope)
        body = _render_deep(binding.get("body"), scope) if binding.get("body") is not None else None
        idemp_tpl = binding.get("idempotency_key") or ""
        idempotency_key = _render(idemp_tpl, scope) if idemp_tpl else ""
    except Exception as e:
        return f"[{binding_id}] erro ao renderizar template: {e}"

    if method in _IDEMPOTENT_REQUIRED_METHODS and not idempotency_key:
        return f"[{binding_id}] idempotency_key é obrigatório para {method}"

    if idempotency_key:
        headers = dict(headers or {})
        headers.setdefault("Idempotency-Key", idempotency_key)

    req_start = time.time()
    resp, last_error, attempts = await _call_with_retry(
        connector, method, path, headers, query, body,
        timeout_s, max_retries, retry_on, backoff,
    )
    latency_ms = round((time.time() - req_start) * 1000, 2)

    status_code = resp.status_code if resp is not None else 0
    resp_json: Any = None
    if resp is not None:
        try:
            resp_json = resp.json()
        except Exception:
            resp_json = None

    call_id = str(uuid.uuid4())
    try:
        await api_call_logs_repo.create({
            "id": call_id,
            "connector_id": connector["id"],
            "endpoint_id": "",
            "agent_id": agent.get("id", ""),
            "method": method,
            "url": connector.get("base_url", "").rstrip("/") + path,
            "request_headers": json.dumps(_redact_headers(headers), ensure_ascii=False),
            "request_body": json.dumps(body, ensure_ascii=False, default=str)[:5000] if body is not None else "{}",
            "response_body": (json.dumps(resp_json, ensure_ascii=False, default=str)[:5000]
                              if resp_json is not None
                              else (resp.text[:5000] if resp is not None else "")),
            "status_code": status_code,
            "latency_ms": latency_ms,
        })
    except Exception as e:
        logger.warning("Falha ao persistir api_call_log: %s", e)

    executed.append({
        "binding_id": binding_id,
        "call_id": call_id,
        "status": status_code,
        "latency_ms": latency_ms,
        "attempts": attempts,
    })

    if resp is None:
        return f"[{binding_id}] falha de rede: {last_error}"
    if not (200 <= status_code < 300):
        return f"[{binding_id}] HTTP {status_code} — resposta não-2xx"

    mapping = binding.get("output_mapping") or []
    if not mapping:
        return f"[{binding_id}] output_mapping é obrigatório"

    if resp_json is None:
        return f"[{binding_id}] response não é JSON — output_mapping não pôde ser aplicado"

    current_size = len(json.dumps(context, ensure_ascii=False, default=str).encode("utf-8"))
    budget = max(0, _MAX_CONTEXT_BYTES - current_size)
    additions, map_errors = _apply_output_mapping(resp_json, mapping, budget)

    delta = ContextDelta(
        agent_id=agent.get("id", ""),
        skill_ref=getattr(skill_parsed.frontmatter, "id", "") if skill_parsed else "",
        additions=additions,
        span_id=call_id,
    )
    merged = apply_context_delta(context, delta)
    context.clear()
    context.update(merged)

    if map_errors:
        return f"[{binding_id}] output_mapping: " + "; ".join(map_errors)
    return None
