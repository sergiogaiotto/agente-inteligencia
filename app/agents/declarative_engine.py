"""Engine declarativo — executa agents com execution_mode=declarative
sem LLM. Lê ## API Bindings do SKILL.md, resolve templates Jinja2
sandboxed, chama APIs via conectores, aplica output_mapping (jsonpath-ng)
e emite ContextDelta append-only.

Fase 2: single binding sem DAG.
Fase 3: DAG via depends_on, paralelismo por nível, deep-merge de context.
Fase 4: circuit breaker, on_failure=continue|compensate, dry_run.
"""

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from typing import Any

import httpx
from jinja2 import ChainableUndefined, StrictUndefined
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

_jinja_env_strict = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
# dry_run: vars ausentes viram string vazia em vez de erro — permite
# resolver o plan mesmo quando contexto de níveis anteriores não foi
# populado (já que dry_run pula a chamada HTTP).
_jinja_env_lenient = SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False)
_PURE_JINJA_EXPR_RE = re.compile(r"^\s*{{\s*(.+?)\s*}}\s*$", re.DOTALL)

# ═══════════════════════════════════════════════════════
# Templating — Jinja2 sandboxed
# ═══════════════════════════════════════════════════════

def _render(template: Any, scope: dict, lenient: bool = False) -> Any:
    if not isinstance(template, str):
        return template
    if "{{" not in template and "{%" not in template:
        return template
    env = _jinja_env_lenient if lenient else _jinja_env_strict
    pure_expr = _PURE_JINJA_EXPR_RE.match(template)
    if pure_expr:
        # Quando o template é só uma expressão Jinja (ex: "{{ inputs.datamart_ids }}"),
        # preserva o tipo original (lista, inteiro, dict, etc) em vez de converter para string.
        expr = pure_expr.group(1)
        return env.compile_expression(expr)(**scope)
    return env.from_string(template).render(**scope)


def _render_deep(value: Any, scope: dict, lenient: bool = False) -> Any:
    if isinstance(value, str):
        return _render(value, scope, lenient=lenient)
    if isinstance(value, dict):
        return {k: _render_deep(v, scope, lenient=lenient) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_deep(v, scope, lenient=lenient) for v in value]
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
    elif auth_type == "cookie":
        cookie_name = (header_name or "").strip()
        value = api_key.strip()
        if "=" in value and (not cookie_name or cookie_name.lower() in ("cookie", "")):
            headers["Cookie"] = value
        else:
            headers["Cookie"] = f"{cookie_name or 'session'}={value}"
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
# DAG helpers
# ═══════════════════════════════════════════════════════

def _topological_levels(bindings: list[dict]) -> tuple[list[list[dict]], list[str]]:
    """Agrupa bindings em níveis topológicos.

    Retorna (levels, errors). Cada nível é uma lista de bindings que podem
    rodar em paralelo. Em caso de ciclo ou depends_on inexistente, retorna
    levels=[] e errors populado.
    """
    errors: list[str] = []
    by_id: dict[str, dict] = {}
    for b in bindings:
        bid = b.get("id")
        if not bid:
            errors.append("binding sem 'id'")
            continue
        if bid in by_id:
            errors.append(f"binding_id duplicado: '{bid}'")
            continue
        by_id[bid] = b

    if errors:
        return [], errors

    in_deg: dict[str, int] = {bid: 0 for bid in by_id}
    deps_map: dict[str, list[str]] = {}
    for bid, b in by_id.items():
        deps = b.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        deps_map[bid] = list(deps)
        for d in deps:
            if d not in by_id:
                errors.append(f"[{bid}] depends_on '{d}' não existe")
            else:
                in_deg[bid] += 1

    if errors:
        return [], errors

    levels: list[list[dict]] = []
    remaining = dict(in_deg)
    while remaining:
        ready_ids = [bid for bid, deg in remaining.items() if deg == 0]
        if not ready_ids:
            errors.append(f"ciclo detectado nos bindings: {sorted(remaining)}")
            return [], errors
        levels.append([by_id[bid] for bid in ready_ids])
        for bid in ready_ids:
            del remaining[bid]
        for rem_bid in list(remaining):
            deps = deps_map[rem_bid]
            remaining[rem_bid] = sum(1 for d in deps if d in remaining)
    return levels, errors


def _deep_merge(dst: Any, src: Any) -> Any:
    """Merge src em dst recursivamente — dicts mesclam campo a campo;
    listas e escalares: src vence."""
    if isinstance(dst, dict) and isinstance(src, dict):
        out = dict(dst)
        for k, v in src.items():
            out[k] = _deep_merge(dst.get(k), v) if k in dst else v
        return out
    return src


# ═══════════════════════════════════════════════════════
# Circuit breaker (in-memory, per binding_id)
# ═══════════════════════════════════════════════════════

class _Breaker:
    __slots__ = ("threshold", "cooldown_s", "fails", "opened_at")

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.fails = 0
        self.opened_at = 0.0

    def allow(self) -> bool:
        if self.fails < self.threshold:
            return True
        return (time.time() - self.opened_at) >= self.cooldown_s

    def record(self, success: bool) -> None:
        if success:
            self.fails = 0
            self.opened_at = 0.0
        else:
            self.fails += 1
            if self.fails >= self.threshold:
                self.opened_at = time.time()


_BREAKERS: dict[str, _Breaker] = {}


def _get_breaker(binding_id: str, cfg: dict) -> _Breaker:
    br = _BREAKERS.get(binding_id)
    if br is None:
        br = _Breaker(
            threshold=int(cfg.get("threshold", 5)),
            cooldown_s=float(cfg.get("cooldown_s", 30.0)),
        )
        _BREAKERS[binding_id] = br
    return br


# ═══════════════════════════════════════════════════════
# Planning — resolve connector + templates (sem I/O)
# ═══════════════════════════════════════════════════════

async def _plan_binding(binding: dict, scope: dict, lenient: bool = False) -> tuple[dict | None, str | None]:
    """Resolve tudo que não requer I/O. Retorna (plan_dict, error).

    plan_dict contém: connector, method, path, headers, query, body,
    idempotency_key, resilience (resolvido), binding_id, raw.

    lenient=True (dry_run): vars ausentes viram string vazia em vez de
    erro — permite resolver plan de níveis que dependeriam de context
    que só seria populado após chamadas HTTP reais.
    """
    binding_id = binding.get("id", "?")
    connector_ref = binding.get("connector")
    if not connector_ref:
        return None, f"[{binding_id}] campo 'connector' ausente"

    connector = await _resolve_connector(connector_ref)
    if not connector:
        return None, f"[{binding_id}] connector '{connector_ref}' não encontrado no registry"

    resilience = binding.get("resilience") or {}
    timeout_ms = int(resilience.get("timeout_ms") or connector.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)
    retry_cfg = resilience.get("retry") or {}
    breaker_cfg = resilience.get("circuit_breaker") or {}

    method = (binding.get("method") or "GET").upper()

    try:
        path = _render(binding.get("path", "/"), scope, lenient=lenient)
        headers = _render_deep(binding.get("headers") or {}, scope, lenient=lenient)
        query = _render_deep(binding.get("query") or {}, scope, lenient=lenient)
        body = _render_deep(binding.get("body"), scope, lenient=lenient) if binding.get("body") is not None else None
        idemp_tpl = binding.get("idempotency_key") or ""
        idempotency_key = _render(idemp_tpl, scope, lenient=lenient) if idemp_tpl else ""
    except Exception as e:
        return None, f"[{binding_id}] erro ao renderizar template: {e}"

    if method in _IDEMPOTENT_REQUIRED_METHODS and not idempotency_key and not lenient:
        return None, f"[{binding_id}] idempotency_key é obrigatório para {method}"

    if idempotency_key:
        headers = dict(headers or {})
        headers.setdefault("Idempotency-Key", idempotency_key)

    plan = {
        "binding_id": binding_id,
        "connector": connector,
        "method": method,
        "path": path,
        "headers": headers,
        "query": query,
        "body": body,
        "idempotency_key": idempotency_key,
        "timeout_ms": timeout_ms,
        "retry": {
            "max": int(retry_cfg.get("max", 0)),
            "on": set(retry_cfg.get("on") or []),
            "backoff": retry_cfg.get("backoff", "fixed"),
        },
        "breaker": breaker_cfg,
        "on_failure": binding.get("on_failure", "fail"),
        "output_mapping": binding.get("output_mapping") or [],
    }
    return plan, None


# ═══════════════════════════════════════════════════════
# Binding execution (post-plan)
# ═══════════════════════════════════════════════════════

async def _execute_planned_binding(
    plan: dict,
    agent: dict,
    skill_parsed: Any,
) -> dict:
    """Executa um binding previamente resolvido. Retorna dict com
    status, call_id, latency_ms, attempts, additions, error, etc.
    Não muta contexto — caller decide como aplicar.
    """
    binding_id = plan["binding_id"]
    connector = plan["connector"]
    breaker_cfg = plan["breaker"] or {}
    breaker = _get_breaker(binding_id, breaker_cfg) if breaker_cfg else None

    if breaker and not breaker.allow():
        return {
            "binding_id": binding_id,
            "call_id": "",
            "status": 0,
            "latency_ms": 0.0,
            "attempts": 0,
            "additions": {},
            "error": f"[{binding_id}] circuit breaker aberto — chamada suprimida",
            "skipped_by_breaker": True,
        }

    req_start = time.time()
    resp, last_error, attempts = await _call_with_retry(
        connector, plan["method"], plan["path"],
        plan["headers"], plan["query"], plan["body"],
        plan["timeout_ms"] / 1000.0,
        plan["retry"]["max"], plan["retry"]["on"], plan["retry"]["backoff"],
    )
    latency_ms = round((time.time() - req_start) * 1000, 2)

    status_code = resp.status_code if resp is not None else 0
    resp_json: Any = None
    if resp is not None:
        try:
            resp_json = resp.json()
        except Exception:
            resp_json = None

    success = resp is not None and 200 <= status_code < 300
    if breaker:
        breaker.record(success)

    call_id = str(uuid.uuid4())
    try:
        await api_call_logs_repo.create({
            "id": call_id,
            "connector_id": connector["id"],
            "endpoint_id": "",
            "agent_id": agent.get("id", ""),
            "method": plan["method"],
            "url": connector.get("base_url", "").rstrip("/") + plan["path"],
            "request_headers": json.dumps(_redact_headers(plan["headers"]), ensure_ascii=False),
            "request_body": json.dumps(plan["body"], ensure_ascii=False, default=str)[:5000]
                             if plan["body"] is not None else "{}",
            "response_body": (json.dumps(resp_json, ensure_ascii=False, default=str)[:5000]
                              if resp_json is not None
                              else (resp.text[:5000] if resp is not None else "")),
            "status_code": status_code,
            "latency_ms": latency_ms,
        })
    except Exception as e:
        logger.warning("Falha ao persistir api_call_log: %s", e)

    result = {
        "binding_id": binding_id,
        "call_id": call_id,
        "status": status_code,
        "latency_ms": latency_ms,
        "attempts": attempts,
        "additions": {},
        "error": None,
    }

    if not success:
        result["error"] = (
            f"[{binding_id}] falha de rede: {last_error}"
            if resp is None
            else f"[{binding_id}] HTTP {status_code} — resposta não-2xx"
        )
        return result

    mapping = plan["output_mapping"]
    if not mapping:
        result["error"] = f"[{binding_id}] output_mapping é obrigatório"
        return result
    if resp_json is None:
        result["error"] = f"[{binding_id}] response não é JSON — output_mapping não pôde ser aplicado"
        return result

    additions, map_errors = _apply_output_mapping(resp_json, mapping, _MAX_CONTEXT_BYTES)
    result["additions"] = additions
    if map_errors:
        result["error"] = f"[{binding_id}] output_mapping: " + "; ".join(map_errors)
    return result


# ═══════════════════════════════════════════════════════
# Orchestration — DAG + paralelismo + deep-merge
# ═══════════════════════════════════════════════════════

async def execute_declarative(
    agent: dict,
    skill_parsed: Any,
    inputs: dict | None = None,
    context: dict | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Executa um agente no modo declarativo com DAG.

    - depends_on → níveis topológicos.
    - Bindings de um mesmo nível rodam em paralelo (asyncio.gather).
    - Additions deep-merged no context.
    - on_failure: fail | continue | compensate: <binding_id>.
    - dry_run: resolve plan sem tocar na rede.
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
        return _build_empty_result(trace_id, agent, context, start,
                                    ["Nenhum API Binding encontrado no SKILL.md"])

    bindings_by_id = {b["id"]: b for b in bindings}

    # Auto-exclui do DAG bindings que são apenas alvos de compensação
    # (não devem rodar no fluxo normal, só quando alguém falha).
    compensation_targets: set[str] = set()
    for b in bindings:
        of = b.get("on_failure")
        if isinstance(of, dict) and "compensate" in of:
            compensation_targets.add(of["compensate"])
        elif isinstance(of, str) and of.startswith("compensate:"):
            compensation_targets.add(of.split(":", 1)[1].strip())

    dag_bindings = [b for b in bindings if b.get("id") not in compensation_targets]
    levels, dag_errors = _topological_levels(dag_bindings)
    if dag_errors:
        return _build_empty_result(trace_id, agent, context, start, dag_errors)
    executed: list[dict] = []
    errors: list[str] = []
    compensations_fired: list[str] = []
    dry_run_plans: list[dict] = []
    fatal = False

    for level_idx, level in enumerate(levels):
        if fatal:
            break

        # Planning: resolve connector + templates para cada binding do nível
        plan_results = await asyncio.gather(
            *[_plan_binding(b, scope, lenient=dry_run) for b in level],
            return_exceptions=False,
        )

        runnable_plans: list[dict] = []
        for (plan, perr), binding in zip(plan_results, level):
            if perr:
                errors.append(perr)
                on_failure = binding.get("on_failure", "fail")
                if on_failure == "fail":
                    fatal = True
            elif plan is not None:
                runnable_plans.append(plan)
                if dry_run:
                    dry_run_plans.append({
                        "binding_id": plan["binding_id"],
                        "level": level_idx,
                        "method": plan["method"],
                        "url": plan["connector"].get("base_url", "").rstrip("/") + plan["path"],
                        "headers": _redact_headers(plan["headers"]),
                        "query": plan["query"],
                        "body": plan["body"],
                        "timeout_ms": plan["timeout_ms"],
                        "output_mapping_keys": [m.get("to") for m in plan["output_mapping"]],
                    })

        if fatal:
            break

        if dry_run:
            # Registra execução simulada sem chamar HTTP
            for p in runnable_plans:
                executed.append({
                    "binding_id": p["binding_id"],
                    "call_id": "",
                    "status": 0,
                    "latency_ms": 0.0,
                    "attempts": 0,
                    "level": level_idx,
                    "dry_run": True,
                })
            continue

        # Execução paralela do nível
        exec_results = await asyncio.gather(
            *[_execute_planned_binding(p, agent, skill_parsed) for p in runnable_plans],
            return_exceptions=False,
        )

        # Agrega additions com deep-merge (sequencial dentro do nível por
        # determinismo: ordem do SKILL.md → last-write-wins em colisão)
        level_additions: dict = {}
        level_had_success = False
        compensations_to_run: list[str] = []

        for r, p in zip(exec_results, runnable_plans):
            executed.append({**{k: r[k] for k in ("binding_id","call_id","status","latency_ms","attempts")},
                             "level": level_idx})
            if r.get("skipped_by_breaker"):
                errors.append(r["error"])
            elif r["error"]:
                errors.append(r["error"])
                bspec = bindings_by_id.get(p["binding_id"], {})
                on_failure = bspec.get("on_failure", "fail")
                if isinstance(on_failure, dict) and "compensate" in on_failure:
                    compensations_to_run.append(on_failure["compensate"])
                elif isinstance(on_failure, str) and on_failure.startswith("compensate:"):
                    compensations_to_run.append(on_failure.split(":", 1)[1].strip())
                elif on_failure == "fail":
                    fatal = True
            else:
                level_had_success = True
                level_additions = _deep_merge(level_additions, r["additions"])

        if level_additions:
            merged_additions = {
                k: _deep_merge(context.get(k), v) for k, v in level_additions.items()
            }
            delta = ContextDelta(
                agent_id=agent.get("id", ""),
                skill_ref=getattr(skill_parsed.frontmatter, "id", "") if skill_parsed else "",
                additions=merged_additions,
            )
            new_ctx = apply_context_delta(context, delta)
            context.clear()
            context.update(new_ctx)
            scope["context"] = context

        # Disparo de compensações — rodam imediatamente, sequencialmente,
        # NÃO propagam fail adicional (já estamos compensando).
        for comp_id in compensations_to_run:
            comp_binding = bindings_by_id.get(comp_id)
            if not comp_binding:
                errors.append(f"compensação '{comp_id}' não encontrada")
                continue
            plan, perr = await _plan_binding(comp_binding, scope)
            if perr:
                errors.append(f"[compensate] {perr}")
                continue
            res = await _execute_planned_binding(plan, agent, skill_parsed)
            compensations_fired.append(comp_id)
            executed.append({
                "binding_id": comp_id,
                "call_id": res["call_id"],
                "status": res["status"],
                "latency_ms": res["latency_ms"],
                "attempts": res["attempts"],
                "level": level_idx,
                "compensation_for": plan["binding_id"],
            })
            if res["error"]:
                errors.append(f"[compensate] {res['error']}")
            elif res["additions"]:
                merged = {k: _deep_merge(context.get(k), v) for k, v in res["additions"].items()}
                delta = ContextDelta(
                    agent_id=agent.get("id", ""),
                    skill_ref=getattr(skill_parsed.frontmatter, "id", "") if skill_parsed else "",
                    additions=merged,
                )
                new_ctx = apply_context_delta(context, delta)
                context.clear()
                context.update(new_ctx)
                scope["context"] = context

    duration_ms = round((time.time() - start) * 1000, 2)
    any_success = any(200 <= e.get("status", 0) < 300 for e in executed)

    if dry_run:
        final_state = "dry_run"
    elif errors and not any_success:
        final_state = "failed"
    elif errors:
        final_state = "partial"
    else:
        final_state = "completed"

    answer_payload = {
        "bindings_executed": executed,
        "errors": errors,
        "context_keys": [k for k in context.keys() if not k.startswith("_")],
        "compensations_fired": compensations_fired,
        "dry_run": dry_run,
        "levels": len(levels),
    }
    if dry_run:
        answer_payload["plans"] = dry_run_plans

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
        "dry_run": dry_run,
        "dry_run_plans": dry_run_plans if dry_run else None,
        "compensations_fired": compensations_fired,
    }


def _build_empty_result(
    trace_id: str,
    agent: dict,
    context: dict,
    start: float,
    errors: list[str],
) -> dict:
    return {
        "interaction_id": trace_id,
        "agent_id": agent.get("id", ""),
        "output": "",
        "final_state": "failed",
        "context": context,
        "bindings_executed": [],
        "errors": errors,
        "duration_ms": round((time.time() - start) * 1000, 2),
        "mode": "declarative",
    }
