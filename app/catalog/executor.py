"""Executor de recipes (Onda 4).

Roda em background task (asyncio.create_task) — não bloqueia o endpoint POST.
Atualiza catalog_recipe_executions linearmente: 1 step por vez, mais recente
primeiro a chegar. UI faz polling em GET /executions/{id}.

Modelo de chain (decidido na fase de design):
- Step 1 recebe input original do consumer.
- Step N+1 recebe `output` do step N.
- Se step N falha, demais ficam status='skipped' e execution finaliza
  como 'partial' (chain quebrou, mas não é fatal).
- Crash do executor finaliza como 'failed' com error_message.

Cost auto-wire:
- Cada step success grava 1 row em catalog_costs com tokens/latency reais.
- cost_usd persiste como 0 nesta onda — pricing table fica para PR de
  cost auto-wire pleno no engine (#69 da Onda 4).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.catalog.queries import (
    append_step_result,
    finalize_execution,
    recompute_entry_trust,
    recompute_external_trust,
    record_invocation_cost,
)
from app.core.database import catalog_entries_repo
from app.core.llm_pricing import compute_cost

logger = logging.getLogger(__name__)

# Limita o tamanho do output persistido no steps_results para não inchar
# o JSONB. Output completo fica em interactions (caso target = agent).
_MAX_OUTPUT_CHARS = 5000

# Kinds invocáveis como STEP de recipe. 'agent' roda no engine interno;
# 'external_platform' (openai_chat) roda via run_probe (orquestração híbrida).
# Recipe → recipe é anti-ciclo intencional; skill/application não têm executor.
_EXECUTABLE_KIND = "agent"
_RECIPE_STEP_KINDS = ("agent", "external_platform")


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


def _truncate(s: str) -> str:
    if not s:
        return ""
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s
    return s[:_MAX_OUTPUT_CHARS] + f"… [+{len(s) - _MAX_OUTPUT_CHARS} chars]"


async def _resolve_target(target_entry_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Lookup target entry. Retorna (entry, reason_if_unrunnable).
    reason=None significa OK. Caso contrário, é msg humana de por que falhou."""
    entry = await catalog_entries_repo.find_by_id(target_entry_id)
    if not entry:
        return None, f"target_entry_id '{target_entry_id}' não existe"
    if entry.get("status") != "published":
        return entry, (
            f"target '{entry.get('name')}' está em status "
            f"'{entry.get('status')}' (só published é executável)"
        )
    kind = entry.get("kind")
    if kind not in _RECIPE_STEP_KINDS:
        return entry, (
            f"target '{entry.get('name')}' é kind='{kind}'; "
            f"recipe só roda steps kind='agent' ou 'external_platform'"
        )
    if kind == "agent":
        if not entry.get("artifact_id"):
            return entry, f"target '{entry.get('name')}' não tem artifact_id"
        return entry, None

    # external_platform — precisa de conexão configurada (openai_chat, p/ produzir
    # saída encadeável). http_ping não gera texto para o próximo step.
    from app.catalog.queries import get_entry_adapter_raw
    cfg = await get_entry_adapter_raw(target_entry_id)
    probe = cfg.get("probe") if isinstance(cfg, dict) else None
    if not isinstance(probe, dict) or not probe.get("base_url"):
        return entry, (
            f"target '{entry.get('name')}' (plataforma externa) não tem conexão "
            f"configurada — configure o adapter antes de usar como step"
        )
    if (probe.get("mode") or "openai_chat") != "openai_chat":
        return entry, (
            f"target '{entry.get('name')}' está em modo '{probe.get('mode')}' "
            f"(sem saída para encadear) — use openai_chat para usar como step de recipe"
        )
    return entry, None


async def _invoke_external_step(target_entry: dict, current_input: str) -> dict:
    """Invoca uma Plataforma Externa (openai_chat) como step de recipe, via
    run_probe. current_input vira o prompt; a saída alimenta o próximo step.
    Levanta em falha (não-2xx/erro) para o caller quebrar a chain."""
    from app.catalog.external_probe import run_probe
    from app.catalog.queries import get_entry_adapter_raw

    cfg = await get_entry_adapter_raw(target_entry["id"])
    probe = (cfg.get("probe") if isinstance(cfg, dict) else None) or {}
    secret = probe.get("secret_cipher") or ""
    res = await run_probe(probe, secret=secret, input_text=current_input)
    if not res.get("ok"):
        raise RuntimeError(
            res.get("error") or f"plataforma externa respondeu HTTP {res.get('status')}"
        )
    tok_in = int(res.get("tokens_input") or 0)
    tok_out = int(res.get("tokens_output") or 0)
    return {
        "output": res.get("output") or "",
        "duration_ms": int(res.get("latency_ms") or 0),
        "tokens_input": tok_in,
        "tokens_output": tok_out,
        "tokens_total": tok_in + tok_out,
        "provider": probe.get("base_url"),
        "model": probe.get("model"),
        "interaction_id": None,
        "final_state": "external",
    }


async def _invoke_step(
    target_entry: dict, current_input: str, consumer_user_id: str
) -> dict:
    """Invoca o target (agent ou external_platform) e retorna dict normalizado:
    {output, duration_ms, tokens_input, tokens_output, tokens_total,
     provider, model, interaction_id, final_state}.
    Levanta exception para erros técnicos (caller trata)."""
    if target_entry.get("kind") == "external_platform":
        return await _invoke_external_step(target_entry, current_input)

    # agent — import lazy (engine carrega LLM clients pesados na importação).
    from app.agents.engine import execute_interaction

    result = await execute_interaction(
        agent_id=target_entry["artifact_id"],
        user_input=current_input,
        channel="recipe",
        journey=f"recipe:{target_entry.get('id')}",
        # Passo de recipe = stateless (a saída de um passo alimenta o próximo via
        # current_input, não via memória de sessão). 'none' mantém determinismo.
        context_mode="none",
        # Grounded-by-default (2026-06-06): replay de recipe é determinístico e
        # a saída de um passo alimenta o próximo via current_input (não via
        # evidência RAG). strict=True recusaria passos sem fonte e quebraria o
        # encadeamento. Fixamos False — a guarda é runtime de produção.
        grounding_strict=False,
    )
    trace = result.get("trace") or {}
    tokens = trace.get("tokens") or {}
    return {
        "output": result.get("output") or "",
        "duration_ms": int(result.get("duration_ms") or 0),
        # input_billed_sum (soma entre chamadas) p/ o custo não subcontar turnos
        # multi-chamada; fallback a 'input' (última) p/ single-call/traces antigos.
        "tokens_input": int(tokens.get("input_billed_sum") or tokens.get("input") or 0),
        "tokens_output": int(tokens.get("output") or 0),
        "tokens_total": int(tokens.get("total_billed") or tokens.get("total") or 0),
        "provider": trace.get("agent_provider"),
        "model": trace.get("agent_model"),
        "interaction_id": result.get("interaction_id"),
        "final_state": result.get("final_state"),
    }


async def execute_recipe(
    execution_id: str,
    recipe_entry_id: str,
    steps: list[dict],
    consumer_user: dict,
    user_input: str,
    *,
    is_sandbox: bool = False,
) -> None:
    """Roda o recipe inteiro. Pensado para ser invocado via asyncio.create_task
    (não bloqueia o caller). Erros internos são capturados; status final
    sempre persiste em catalog_recipe_executions.

    is_sandbox=True marca run de teste:
    - NÃO grava em catalog_costs (não polui dashboards de chargeback)
    - step_results ainda contêm cost_usd calculado (para drill-down)
    - LLM ainda é chamado real — testa qualidade, latência e comportamento
    """
    start = time.time()
    total_cost_usd = 0.0
    total_latency_ms = 0
    any_failure = False
    consumer_user_id = consumer_user.get("id")

    if is_sandbox:
        logger.info(
            f"sandbox run start: execution={execution_id} "
            f"recipe={recipe_entry_id} consumer={consumer_user_id}"
        )

    try:
        # Steps em ordem crescente — defensivo caso o repo retorne fora de ordem.
        ordered = sorted(steps, key=lambda s: s.get("order", 0))
        current_input = user_input
        chain_broken = False

        for step in ordered:
            order = step.get("order")
            target_entry_id = step.get("target_entry_id")
            notes = step.get("notes") or ""

            # Step já marcado como skipped se chain quebrou em step anterior.
            if chain_broken:
                await append_step_result(execution_id, {
                    "order": order,
                    "target_entry_id": target_entry_id,
                    "target_name": None,
                    "notes": notes,
                    "status": "skipped",
                    "output": "",
                    "error": "step anterior falhou — chain interrompida",
                    "cost_usd": 0,
                    "tokens_used": 0,
                    "latency_ms": 0,
                    "interaction_id": None,
                    "started_at": _iso(),
                    "finished_at": _iso(),
                })
                continue

            step_start = time.time()
            step_iso_start = _iso()
            target_entry, reason = await _resolve_target(target_entry_id)
            target_name = (target_entry or {}).get("name")

            if reason:
                any_failure = True
                chain_broken = True
                await append_step_result(execution_id, {
                    "order": order,
                    "target_entry_id": target_entry_id,
                    "target_name": target_name,
                    "notes": notes,
                    "status": "error",
                    "output": "",
                    "error": reason,
                    "cost_usd": 0,
                    "tokens_used": 0,
                    "latency_ms": int((time.time() - step_start) * 1000),
                    "interaction_id": None,
                    "started_at": step_iso_start,
                    "finished_at": _iso(),
                })
                continue

            try:
                inv = await _invoke_step(target_entry, current_input, consumer_user_id)
            except Exception as e:
                logger.exception(
                    f"recipe step falhou: execution={execution_id} "
                    f"step={order} target={target_entry_id}"
                )
                any_failure = True
                chain_broken = True
                await append_step_result(execution_id, {
                    "order": order,
                    "target_entry_id": target_entry_id,
                    "target_name": target_name,
                    "notes": notes,
                    "status": "error",
                    "output": "",
                    "error": f"{type(e).__name__}: {e}",
                    "cost_usd": 0,
                    "tokens_used": 0,
                    "latency_ms": int((time.time() - step_start) * 1000),
                    "interaction_id": None,
                    "started_at": step_iso_start,
                    "finished_at": _iso(),
                })
                continue

            # Success do step — calcula cost real (PR #69) e grava best-effort
            step_latency_ms = inv["duration_ms"] or int((time.time() - step_start) * 1000)
            step_tokens_in = inv["tokens_input"]
            step_tokens_out = inv["tokens_output"]
            step_tokens_total = inv["tokens_total"] or (step_tokens_in + step_tokens_out)
            step_cost_usd = compute_cost(
                inv.get("provider"), inv.get("model"),
                step_tokens_in, step_tokens_out,
            )

            # Sandbox NÃO grava em catalog_costs — runs de teste não devem
            # poluir dashboards de chargeback. step_results ainda contém
            # cost_usd calculado para drill-down.
            if not is_sandbox:
                try:
                    await record_invocation_cost(
                        target_entry_id,
                        consumer_user_id=consumer_user_id,
                        consumer_department=(consumer_user.get("domains") or [None])[0]
                            if isinstance(consumer_user.get("domains"), list) else None,
                        interaction_id=inv.get("interaction_id"),
                        cost_usd=step_cost_usd,
                        tokens_used=step_tokens_total,
                        latency_ms=step_latency_ms,
                    )
                except Exception as e:
                    # Cost grava best-effort — falha aqui não derruba o step
                    logger.warning(
                        f"record_invocation_cost falhou: execution={execution_id} "
                        f"step={order}: {type(e).__name__}: {e}"
                    )

            total_cost_usd += step_cost_usd
            total_latency_ms += step_latency_ms

            await append_step_result(execution_id, {
                "order": order,
                "target_entry_id": target_entry_id,
                "target_name": target_name,
                "notes": notes,
                "status": "success",
                "output": _truncate(inv["output"]),
                "error": None,
                "cost_usd": step_cost_usd,
                "tokens_used": step_tokens_total,
                "tokens_input": step_tokens_in,
                "tokens_output": step_tokens_out,
                "latency_ms": step_latency_ms,
                "provider": inv.get("provider"),
                "model": inv.get("model"),
                "interaction_id": inv.get("interaction_id"),
                "final_state": inv.get("final_state"),
                "started_at": step_iso_start,
                "finished_at": _iso(),
            })

            # Chain: output do step N vira input do step N+1
            current_input = inv["output"] or ""

        # Status final agregado
        elapsed_total_ms = int((time.time() - start) * 1000)
        # Prefere total_latency_ms (soma dos steps) ao elapsed wall-clock — soma é
        # mais fiel ao custo computacional; wall-clock é fallback se nada rodou.
        final_latency = total_latency_ms or elapsed_total_ms
        final_status = "partial" if any_failure else "completed"
        await finalize_execution(
            execution_id,
            status=final_status,
            total_cost_usd=total_cost_usd,
            total_latency_ms=final_latency,
        )

    except Exception as e:
        # Catch-all: row não pode ficar 'running' forever.
        logger.exception(f"execute_recipe crashed: execution={execution_id}")
        try:
            await finalize_execution(
                execution_id,
                status="failed",
                total_cost_usd=total_cost_usd,
                total_latency_ms=total_latency_ms or int((time.time() - start) * 1000),
                error_message=f"{type(e).__name__}: {e}",
            )
        except Exception:
            logger.exception(
                f"finalize_execution também falhou: execution={execution_id}"
            )


async def _finalize_failed(execution_id: str, start: float, err: Exception) -> None:
    """Best-effort: sela a execução como 'failed' (nunca deixa 'running' forever)."""
    try:
        await finalize_execution(
            execution_id,
            status="failed",
            total_cost_usd=0.0,
            total_latency_ms=int((time.time() - start) * 1000),
            error_message=f"{type(err).__name__}: {err}"[:500],
        )
    except Exception:
        logger.exception(f"finalize_execution(failed) também falhou: execution={execution_id}")


async def execute_pipeline_entry(
    *,
    execution_id: str,
    pipeline_entry_id: str,
    root_agent_id: str,
    consumer_user: dict,
    user_input: str,
    is_sandbox: bool = False,
    allowed_agent_ids: set | None = None,
) -> None:
    """Executa um pipeline publicado (kind='pipeline') REUSANDO o motor do mesh
    (engine.execute_pipeline) a partir da raiz. Grava na MESMA tabela das runs de
    recipe (catalog_recipe_executions; recipe_entry_id guarda o id da entry do
    pipeline) e reusa trust/custo (record_invocation_cost). Background task —
    cliente faz polling em GET /executions/{id}.

    Mapeia o resultado do mesh (pipeline_steps) em steps_results. Status final:
    'completed' (tudo ok) | 'partial' (algum step com erro, mas houve execução) |
    'failed' (crash ou nada executou).

    CUSTO (PR7): os steps do mesh agora EXPÕEM cost_usd/tokens_used (cost auto-wire
    no engine), então a soma de custo/tokens é REAL. Após finalizar, recalcula o
    trust da entry (reliability/latency_p95/avg_cost) a partir das execuções reais.
    """
    start = time.time()
    try:
        from app.agents.engine import execute_pipeline
        result = await execute_pipeline(
            entry_agent_id=root_agent_id,
            user_input=user_input,
            channel="catalog",
            allowed_agent_ids=allowed_agent_ids,  # selado ao subgrafo do pipeline (PR-A1)
        )
    except Exception as e:
        logger.exception(f"execute_pipeline_entry crashed (engine): execution={execution_id}")
        await _finalize_failed(execution_id, start, e)
        return

    # Gravação guardada: um erro de DB aqui NÃO pode deixar a row 'running' forever
    # (espelha o catch-all de execute_recipe). Sela como 'failed' no except.
    try:
        steps = result.get("pipeline_steps") or []
        total_cost_usd = 0.0
        for i, s in enumerate(steps):
            cost = float(s.get("cost_usd") or 0)  # real desde o PR7 (cost auto-wire no engine)
            total_cost_usd += cost
            await append_step_result(execution_id, {
                "order": i + 1,
                "agent_id": s.get("agent_id"),
                "agent_name": s.get("agent_name"),
                "status": s.get("status"),
                "final_state": s.get("final_state"),
                "output": _truncate(s.get("output", "") or ""),
                "error": s.get("error"),
                "cost_usd": cost,
                "tokens_used": int(s.get("tokens_used") or 0),
                "latency_ms": float(s.get("duration_ms") or s.get("latency_ms") or 0),
            })

        total_latency_ms = int(result.get("duration_ms") or (time.time() - start) * 1000)
        executed = int(result.get("completed_agents") or 0)
        had_error = any(str(s.get("status", "")).startswith("error") for s in steps)
        if had_error and executed == 0:
            final_status = "failed"
        elif had_error:
            final_status = "partial"
        else:
            final_status = "completed"

        await finalize_execution(
            execution_id,
            status=final_status,
            total_cost_usd=total_cost_usd,
            total_latency_ms=total_latency_ms,
            error_message=None,
        )
    except Exception as e:
        logger.exception(f"execute_pipeline_entry crashed (recording): execution={execution_id}")
        await _finalize_failed(execution_id, start, e)
        return

    # Trust/custo: bump na entry do pipeline (não em sandbox). Com o cost auto-wire
    # do PR7, total_cost_usd/tokens já são REAIS (somados dos steps do mesh).
    if not is_sandbox:
        total_tokens = sum(int(s.get("tokens_used") or 0) for s in steps)
        try:
            await record_invocation_cost(
                pipeline_entry_id,
                consumer_user_id=consumer_user["id"],
                interaction_id=result.get("interaction_id"),
                cost_usd=total_cost_usd,
                tokens_used=total_tokens,
                latency_ms=float(total_latency_ms),
            )
        except Exception:
            logger.exception(f"record_invocation_cost falhou: execution={execution_id}")
        # PR7 — recalcula trust_reliability/latency_p95/avg_cost da entry a partir
        # das execuções reais (reliability/latência já valem; custo agora também).
        try:
            await recompute_entry_trust(pipeline_entry_id)
        except Exception:
            logger.exception(f"recompute_entry_trust falhou: execution={execution_id}")

    # PR8b3: devolve o result do engine (output não-truncado) p/ chamadores SÍNCRONOS
    # (ingress de federação). Os chamadores via asyncio.create_task ignoram o retorno
    # (comportamento inalterado). None nos caminhos de falha acima.
    return result


async def probe_external_platform(
    *,
    execution_id: str,
    entry_id: str,
    entry_name: str,
    config: dict,
    secret: str,
    user_input: str,
) -> None:
    """PR2 — 'Provar Capacidade': roda UM probe contra a plataforma externa e
    grava como execução de 1 step em catalog_recipe_executions (reusa o trilho
    de polling de recipes/pipelines). Pensado para asyncio.create_task.

    is_sandbox é selado pelo endpoint (probe é sempre teste — custo não vai p/
    chargeback). run_probe é fail-soft; o try/except é cinto-e-suspensório p/
    nunca deixar a row em 'running'."""
    from app.catalog.external_probe import run_probe  # lazy: httpx/ssrf

    started = _iso()
    try:
        res = await run_probe(config, secret=secret, input_text=user_input)
        ok = bool(res.get("ok"))
        latency = int(res.get("latency_ms") or 0)
        tok_in = int(res.get("tokens_input") or 0)
        tok_out = int(res.get("tokens_output") or 0)
        step = {
            "order": 1,
            "target_entry_id": entry_id,
            "target_name": entry_name,
            "status": "success" if ok else "error",
            "output": _truncate(res.get("output") or ""),
            "error": None if ok else (res.get("error") or f"HTTP {res.get('status')}"),
            "cost_usd": 0.0,
            "tokens_input": tok_in,
            "tokens_output": tok_out,
            "tokens_total": tok_in + tok_out,
            "provider": config.get("base_url"),
            "model": config.get("model"),
            "interaction_id": None,
            "latency_ms": latency,
            "http_status": res.get("status"),
            "hint": res.get("hint"),
            "started_at": started,
            "finished_at": _iso(),
        }
        await append_step_result(execution_id, step)
        await finalize_execution(
            execution_id,
            status="completed" if ok else "failed",
            total_cost_usd=0.0,
            total_latency_ms=latency,
            error_message=None if ok else step["error"],
        )
        await _recompute_external_trust_safe(entry_id, execution_id)
    except Exception as e:
        logger.exception(
            "probe_external_platform crashed: execution=%s entry=%s", execution_id, entry_id
        )
        try:
            await finalize_execution(
                execution_id,
                status="failed",
                total_cost_usd=0.0,
                total_latency_ms=0,
                error_message=f"{type(e).__name__}: {str(e)[:160]}",
            )
            await _recompute_external_trust_safe(entry_id, execution_id)
        except Exception:
            logger.exception(f"finalize_execution(failed) falhou: execution={execution_id}")


async def _recompute_external_trust_safe(entry_id: str, execution_id: str) -> None:
    """Recalcula trust real da plataforma externa a partir das execuções de probe.
    Best-effort: nunca derruba o fluxo do probe."""
    try:
        await recompute_external_trust(entry_id)
    except Exception:
        logger.exception(f"recompute_external_trust falhou: execution={execution_id}")
