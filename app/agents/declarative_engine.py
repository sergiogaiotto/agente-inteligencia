"""Engine declarativo — executa agents com execution_mode=declarative
sem LLM. Lê ## API Bindings do SKILL.md, resolve templates Jinja2
sandboxed, chama APIs via conectores, aplica output_mapping (jsonpath-ng)
e emite ContextDelta append-only.

Fase 2: single binding sem DAG.
Fase 3: DAG via depends_on, paralelismo por nível, deep-merge de context.
Fase 4: circuit breaker, on_failure=continue|compensate, dry_run.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
from jinja2 import ChainableUndefined, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from jsonpath_ng.ext import parse as jsonpath_parse

from app.a2a.protocol import ContextDelta, apply_context_delta
from app.core.database import (
    api_connectors_repo, api_call_logs_repo,
    interactions_repo, binding_executions_repo,
)
from app.core.http_auth import (
    build_auth_headers,
    prepare_request_body,
    redact_headers,
)

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

# RFC 6570 / OpenAPI path templates usam {name} (brace único). O engine
# de templating é Jinja2 que só interpreta {{ }}. Sem pre-substituição,
# `/api/cep/v1/{cep}` chega literal ao httpx, é URL-encodado como
# `%7Bcep%7D`, e a API externa rejeita (Bug fix 2026-06-01: BrasilAPI
# respondia "CEP possui menos do que 8 caracteres" para um CEP válido
# de 8 chars — recebia literal `{cep}` (5 chars) na URL).
_PATH_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

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


def _resolve_path_placeholders(path: str, scope: dict) -> str:
    """Substitui placeholders `{name}` em path por valor de `scope['inputs'][name]`.

    Convenção: o parser persiste path em estilo brace-único (RFC 6570 /
    OpenAPI path templates — igual ao path do connector real). Esta função
    traduz para o valor concreto ANTES do Jinja `_render`, sem afetar
    expressões Jinja `{{ ... }}` já presentes (Jinja segue funcionando para
    casos avançados).

    Placeholders não resolvidos (nome não existe em `scope.inputs` nem em
    `scope`) ficam literais — facilita debug em vez de virar string vazia
    silenciosa. Aplicado apenas em path (não em headers/query/body para
    não interferir com `{}` legítimos em JSON; usuários que precisam de
    interpolação nesses campos usam Jinja `{{ ... }}` explícito).
    """
    if not isinstance(path, str) or "{" not in path:
        return path
    inputs = scope.get("inputs") or {}

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if isinstance(inputs, dict) and name in inputs:
            return str(inputs[name])
        if name in scope:
            return str(scope[name])
        return m.group(0)

    return _PATH_PLACEHOLDER_RE.sub(_sub, path)


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
            overflow = size - max_bytes
            errors.append(
                f"Valor em '{dst}' ({size}B) excede max_bytes={max_bytes} (+{overflow}B). "
                "Refine o JSONPath em 'from' ou aumente 'max_bytes' no output_mapping."
            )
            continue
        used += size
        if used > bytes_budget:
            errors.append("Orçamento de contexto esgotado — mapeamento truncado")
            break
        _set_dotted(additions, dst, val)
    return additions, errors


# ═══════════════════════════════════════════════════════
# Data Tables (Onda Tabular) — fase pré-DAG
# Roda ANTES dos API Bindings, sequencialmente. Resultados ficam
# no context.scope e podem ser referenciados por bindings via
# `{{ context.<chave> }}`. Cada item invoca o service tabular
# (que já valida coluna/operador/limit e audita em data_table_query_logs).
# ═══════════════════════════════════════════════════════


async def _execute_data_tables_phase(
    data_tables: list[dict],
    agent: dict,
    scope: dict,
    context: dict,
    skill_parsed: Any,
    interaction_id: str,
) -> tuple[list[dict], list[str]]:
    """Executa a fase de Data Tables (Onda Tabular). Mutates `context`/`scope`.

    Para cada item em `data_tables`:
    - Resolve templates Jinja2 dos filters/inputs (com scope: inputs, context).
    - Chama tabular.execute_query() — bind vars seguras, read-only, audited.
    - Aplica output_mapping (jsonpath sobre `{rows, row_count, columns}`).
    - Merge das additions no context (deep-merge, paridade com bindings).
    - Erros respeitam on_error: fail (interrompe) | continue (segue).
    - Registra em binding_executions com binding_id="table:<id>".

    Returns:
        (executed, errors): lista de execuções (igual aos bindings, com
        kind='table') + lista de erros agregados.
    """
    # Import lazy: feature pode estar inativa em deploys sem duckdb
    try:
        from app.evidence.tabular import execute_query as tabular_execute_query, TabularError
    except ImportError:
        return [], ["Engine tabular indisponível (DuckDB não instalado)"]

    executed: list[dict] = []
    errors: list[str] = []

    for item in data_tables or []:
        item_id = str(item.get("id") or "")
        table_ref = str(item.get("table_ref") or "")
        if not item_id or not table_ref:
            errors.append(f"data_table inválido (id/table_ref ausente): {item}")
            continue

        on_error = (item.get("on_error") or "fail").strip().lower()
        query_spec = item.get("query") or {}

        # Resolve table_id a partir de URN ou aceita id direto
        try:
            from app.data_tables.queries import find_by_urn_with_ks, find_by_id_with_ks
        except ImportError:
            errors.append("Engine tabular indisponível (módulo data_tables)")
            return executed, errors

        try:
            table_row = await find_by_urn_with_ks(table_ref) if table_ref.startswith("urn:table:") else await find_by_id_with_ks(table_ref)
        except Exception as e:
            errors.append(f"[table {item_id}] erro ao resolver '{table_ref}': {e}")
            if on_error == "fail":
                return executed, errors
            continue

        if not table_row:
            errors.append(f"[table {item_id}] tabela '{table_ref}' não encontrada")
            if on_error == "fail":
                return executed, errors
            continue

        # Render Jinja2 dos filters (value pode ter "{{ inputs.X }}")
        try:
            filters_raw = query_spec.get("filters") or []
            rendered_filters = _render_deep(filters_raw, scope, lenient=False)
            select = query_spec.get("select") or []
            order_by = query_spec.get("order_by") or []
            limit = int(query_spec.get("limit") or 100)
        except Exception as e:
            errors.append(f"[table {item_id}] falha ao renderizar templates: {e}")
            if on_error == "fail":
                return executed, errors
            continue

        # Inputs do scope.inputs viram inputs do service (para if_present
        # e templates simples internos)
        service_inputs = dict(scope.get("inputs") or {})

        t0 = time.time()
        status_int = 0
        error_str: str | None = None
        result: dict | None = None
        try:
            result = await tabular_execute_query(
                table_id=table_row["id"],
                inputs=service_inputs,
                select=select,
                filters=rendered_filters,
                order_by=order_by,
                limit=limit,
                executed_by=agent.get("id", ""),
                interaction_id=interaction_id,
                agent_id=agent.get("id", ""),
            )
            status_int = 200
        except TabularError as e:
            error_str = f"[table {item_id}] {e}"
            status_int = int(e.status_code)
        except Exception as e:
            error_str = f"[table {item_id}] {e}"
            status_int = 500

        latency_ms = round((time.time() - t0) * 1000, 2)

        # Registra execução (paridade com binding_executions, kind='table')
        call_id = str(uuid.uuid4())
        try:
            await binding_executions_repo.create({
                "id": str(uuid.uuid4()),
                "interaction_id": interaction_id,
                "agent_id": agent.get("id", ""),
                "binding_id": f"table:{item_id}",
                "call_id": call_id,
                "status_code": status_int,
                "latency_ms": latency_ms,
                "attempts": 1,
                "error": error_str,
                "skipped_by_breaker": False,
                "is_compensation": False,
            })
        except Exception as e:
            logger.warning("Declarativo[table]: audit failed for %s: %s", item_id, e)

        exec_record = {
            "binding_id": f"table:{item_id}",
            "call_id": call_id,
            "status": status_int,
            "latency_ms": latency_ms,
            "attempts": 1,
            "level": -1,  # pré-DAG
            "kind": "table",
        }

        if error_str:
            errors.append(error_str)
            executed.append(exec_record)
            if on_error == "fail":
                return executed, errors
            continue

        # Aplica output_mapping (paridade com bindings: jsonpath sobre payload)
        # Payload exposto: dict result completo (rows, row_count, columns).
        # Default se output_mapping ausente: salva tudo em context["tables"][<id>]
        output_mapping = item.get("output_mapping")
        if output_mapping:
            # Aceita 2 formatos:
            #  (a) list [{from, to, max_bytes?}]  — igual aos bindings
            #  (b) dict {alias: "$.jsonpath"}     — açúcar sintático
            if isinstance(output_mapping, dict):
                mapping_list = [
                    {"to": k, "from": v if isinstance(v, str) else "$"}
                    for k, v in output_mapping.items()
                ]
            else:
                mapping_list = output_mapping
            additions, map_errors = _apply_output_mapping(
                result, mapping_list, bytes_budget=_MAX_CONTEXT_BYTES
            )
            for me in map_errors:
                errors.append(f"[table {item_id}] {me}")
        else:
            additions = {"tables": {item_id: result}}

        if additions:
            merged = {k: _deep_merge(context.get(k), v) for k, v in additions.items()}
            delta = ContextDelta(
                agent_id=agent.get("id", ""),
                skill_ref=getattr(skill_parsed.frontmatter, "id", "") if skill_parsed else "",
                additions=merged,
            )
            new_ctx = apply_context_delta(context, delta)
            context.clear()
            context.update(new_ctx)
            scope["context"] = context

        exec_record["response_data"] = result
        executed.append(exec_record)

    return executed, errors


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


# ═══════════════════════════════════════════════════════
# HTTP execution
# ═══════════════════════════════════════════════════════

_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


async def _execute_http_call(
    connector: dict,
    method: str,
    path: str,
    headers: dict,
    auth_headers: dict,
    query: dict,
    body: Any,
    body_type: str,
    timeout_s: float,
) -> httpx.Response:
    base = connector.get("base_url", "").rstrip("/")
    url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    method_u = method.upper()
    if method_u not in _VALID_METHODS:
        raise ValueError(f"Método HTTP não suportado: {method}")

    body_kwargs = prepare_request_body(body_type or "json", body, extra_headers=dict(auth_headers or {}))
    body_headers = body_kwargs.pop("headers", {})
    final_headers = {**body_headers, **(headers or {})}

    verify = bool(connector.get("verify_ssl", 1))
    async with httpx.AsyncClient(
        timeout=timeout_s,
        headers=final_headers,
        verify=verify,
        follow_redirects=True,
    ) as client:
        return await client.request(
            method_u, url,
            params=query or None,
            **body_kwargs,
        )


async def _call_with_retry(
    connector: dict,
    method: str,
    path: str,
    headers: dict,
    auth_headers: dict,
    query: dict,
    body: Any,
    body_type: str,
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
            resp = await _execute_http_call(connector, method, path, headers, auth_headers, query, body, body_type, timeout_s)
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
        # Resolve {name} placeholders (brace-único) ANTES do Jinja, porque
        # o parser persiste path em estilo OpenAPI/RFC 6570 e o _render só
        # interpreta {{ }}. Sem isso, /api/cep/v1/{cep} chega ao httpx
        # literal e a API externa rejeita o request.
        raw_path = _resolve_path_placeholders(binding.get("path", "/"), scope)
        path = _render(raw_path, scope, lenient=lenient)
        headers = _render_deep(binding.get("headers") or {}, scope, lenient=lenient)
        query = _render_deep(binding.get("query") or {}, scope, lenient=lenient)
        body = _render_deep(binding.get("body"), scope, lenient=lenient) if binding.get("body") is not None else None
        idemp_tpl = binding.get("idempotency_key") or ""
        idempotency_key = _render(idemp_tpl, scope, lenient=lenient) if idemp_tpl else ""
    except Exception as e:
        # Antes só retornava (None, "erro ao renderizar template: ..."). O
        # erro ia parar em `errors` do execute_declarative mas não em
        # errors.log — troubleshooting de Jinja quebrado em produção ficava
        # cego. Agora vai estruturado com binding_id + connector + path.
        logger.warning(
            "declarative_engine.plan_binding_template_failed",
            extra={
                "event": "declarative.template_failed",
                "binding_id": binding_id,
                "connector": connector_ref,
                "path_template": binding.get("path", "/"),
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return None, f"[{binding_id}] erro ao renderizar template: {e}"

    if method in _IDEMPOTENT_REQUIRED_METHODS and not idempotency_key and not lenient:
        return None, f"[{binding_id}] idempotency_key é obrigatório para {method}"

    if idempotency_key:
        headers = dict(headers or {})
        headers.setdefault("Idempotency-Key", idempotency_key)

    body_type = (binding.get("body_type") or "json").strip().lower()
    auth_headers = build_auth_headers(connector)

    plan = {
        "binding_id": binding_id,
        "connector": connector,
        "method": method,
        "path": path,
        "headers": headers,
        "auth_headers": auth_headers,
        "query": query,
        "body": body,
        "body_type": body_type,
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

async def _persist_binding_execution(
    result: dict,
    interaction_id: str,
    agent_id: str,
    is_compensation: bool,
) -> None:
    """Persiste um row em binding_executions a partir do result dict. Best-effort:
    falha aqui só loga (não interfere no fluxo do agente)."""
    if not interaction_id:
        return  # Sem contexto de interaction, não vale persistir órfão.
    try:
        await binding_executions_repo.create({
            "id": str(uuid.uuid4()),
            "interaction_id": interaction_id,
            "agent_id": agent_id or "",
            "binding_id": result.get("binding_id") or "",
            "call_id": result.get("call_id") or "",
            "status_code": int(result.get("status") or 0),
            "latency_ms": float(result.get("latency_ms") or 0),
            "attempts": int(result.get("attempts") or 0),
            "error": result.get("error"),
            "skipped_by_breaker": bool(result.get("skipped_by_breaker", False)),
            "is_compensation": bool(is_compensation),
        })
    except Exception as e:
        logger.warning("Falha ao persistir binding_execution: %s", e)


async def _execute_planned_binding(
    plan: dict,
    agent: dict,
    skill_parsed: Any,
    interaction_id: str = "",
    is_compensation: bool = False,
) -> dict:
    """Executa um binding previamente resolvido. Retorna dict com
    status, call_id, latency_ms, attempts, additions, error, etc.
    Não muta contexto — caller decide como aplicar.

    Persiste 1 row em binding_executions ao final (best-effort, ignora interaction_id
    vazio para evitar lixo de proxy manual).
    """
    binding_id = plan["binding_id"]
    connector = plan["connector"]
    breaker_cfg = plan["breaker"] or {}
    breaker = _get_breaker(binding_id, breaker_cfg) if breaker_cfg else None

    if breaker and not breaker.allow():
        result = {
            "binding_id": binding_id,
            "call_id": "",
            "status": 0,
            "latency_ms": 0.0,
            "attempts": 0,
            "additions": {},
            "error": f"[{binding_id}] circuit breaker aberto — chamada suprimida",
            "skipped_by_breaker": True,
        }
        await _persist_binding_execution(result, interaction_id, agent.get("id", ""), is_compensation)
        return result

    req_start = time.time()
    resp, last_error, attempts = await _call_with_retry(
        connector, plan["method"], plan["path"],
        plan["headers"], plan.get("auth_headers", {}),
        plan["query"], plan["body"],
        plan.get("body_type", "json"),
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
    persisted_headers = {**plan.get("auth_headers", {}), **(plan["headers"] or {})}
    try:
        await api_call_logs_repo.create({
            "id": call_id,
            "connector_id": connector["id"],
            "endpoint_id": "",
            "agent_id": agent.get("id", ""),
            "interaction_id": interaction_id or "",
            "method": plan["method"],
            "url": connector.get("base_url", "").rstrip("/") + plan["path"],
            "request_headers": json.dumps(redact_headers(persisted_headers), ensure_ascii=False),
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
        "response_data": resp_json if resp_json is not None else (resp.text if resp is not None else None),
    }

    if not success:
        result["error"] = (
            f"[{binding_id}] falha de rede: {last_error}"
            if resp is None
            else f"[{binding_id}] HTTP {status_code} — resposta não-2xx"
        )
    elif not plan["output_mapping"]:
        result["error"] = f"[{binding_id}] output_mapping é obrigatório"
    elif resp_json is None:
        result["error"] = f"[{binding_id}] response não é JSON — output_mapping não pôde ser aplicado"
    else:
        additions, map_errors = _apply_output_mapping(resp_json, plan["output_mapping"], _MAX_CONTEXT_BYTES)
        result["additions"] = additions
        if map_errors:
            result["error"] = f"[{binding_id}] output_mapping: " + "; ".join(map_errors)

    await _persist_binding_execution(result, interaction_id, agent.get("id", ""), is_compensation)
    return result


# ═══════════════════════════════════════════════════════
# Orchestration — DAG + paralelismo + deep-merge
# ═══════════════════════════════════════════════════════

async def _finalize_declarative_interaction(trace_id: str, final_state: str) -> None:
    """Marca a interaction como terminada com o state final do declarativo
    (completed/partial/failed/dry_run). Best-effort: log e segue."""
    try:
        await interactions_repo.update(trace_id, {
            "state": final_state,
            "ended_at": datetime.utcnow(),
        })
    except Exception as e:
        logger.warning("Declarativo: falha ao finalizar interaction %s: %s", trace_id, e)


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

    # Registra a invocação como uma interaction pra que (a) apareça em
    # /agents/{id}/invocations e (b) api_call_logs.interaction_id tenha FK
    # válida. Best-effort: erro aqui não bloqueia a execução.
    try:
        existing_itx = await interactions_repo.find_by_id(trace_id)
        if not existing_itx:
            await interactions_repo.create({
                "id": trace_id,
                "title": ((agent.get("name") or "agent") + " (declarativo)")[:80],
                "agent_id": agent.get("id", ""),
                "channel": "api",
                "journey_id": "",
                "state": "Intake",
            })
    except Exception as e:
        logger.warning("Declarativo: falha ao persistir interaction %s: %s", trace_id, e)

    scope = {
        "inputs": inputs,
        "context": context,
        "session_id": trace_id,
    }

    bindings = list(getattr(skill_parsed, "api_bindings_parsed", []) or [])
    data_tables = list(getattr(skill_parsed, "data_tables_parsed", []) or [])

    # Skill declarativa pode usar SÓ Data Tables, SÓ API Bindings, ou ambos.
    if not bindings and not data_tables:
        await _finalize_declarative_interaction(trace_id, "failed")
        return _build_empty_result(trace_id, agent, context, start,
                                    ["Nenhum API Binding ou Data Table encontrado no SKILL.md"])

    executed: list[dict] = []
    errors: list[str] = []
    compensations_fired: list[str] = []
    dry_run_plans: list[dict] = []
    first_success_response_data: Any = None
    fatal = False

    # ── Fase pré-DAG: Data Tables (Onda Tabular) ─────────────────
    # Executa sequencialmente antes dos API Bindings. Resultados ficam
    # no context — bindings subsequentes podem consumir via {{ context.X }}.
    if data_tables and not dry_run:
        table_executed, table_errors = await _execute_data_tables_phase(
            data_tables=data_tables,
            agent=agent,
            scope=scope,
            context=context,
            skill_parsed=skill_parsed,
            interaction_id=trace_id,
        )
        executed.extend(table_executed)
        errors.extend(table_errors)
        # Se alguma table com on_error=fail estourou, table_errors tem entries
        # mas execute_data_tables_phase já retornou cedo. Não bloqueia loop
        # de bindings — eles podem rodar mesmo sem tables (decisão do user).
        # Para skill SÓ com tables, não há bindings → loop simplesmente skip.

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
        await _finalize_declarative_interaction(trace_id, "failed")
        return _build_empty_result(trace_id, agent, context, start, dag_errors)

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
                        "headers": redact_headers({**plan.get("auth_headers", {}), **(plan["headers"] or {})}),
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
            *[_execute_planned_binding(p, agent, skill_parsed, interaction_id=trace_id) for p in runnable_plans],
            return_exceptions=False,
        )

        # Agrega additions com deep-merge (sequencial dentro do nível por
        # determinismo: ordem do SKILL.md → last-write-wins em colisão)
        level_additions: dict = {}
        level_had_success = False
        compensations_to_run: list[str] = []

        for r, p in zip(exec_results, runnable_plans):
            executed.append({**{k: r[k] for k in ("binding_id","call_id","status","latency_ms","attempts")},
                             "level": level_idx,
                             "response_data": r.get("response_data")})
            if 200 <= r.get("status", 0) < 300 and first_success_response_data is None:
                first_success_response_data = r.get("response_data")
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
            res = await _execute_planned_binding(plan, agent, skill_parsed, interaction_id=trace_id, is_compensation=True)
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

    await _finalize_declarative_interaction(trace_id, final_state)

    return {
        "interaction_id": trace_id,
        "agent_id": agent.get("id", ""),
        "output": json.dumps(answer_payload, ensure_ascii=False, indent=2),
        "final_state": final_state,
        "context": context,
        "api_response": first_success_response_data,
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
