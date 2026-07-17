"""Rotas do módulo Otimização de Prompt/Skill (45.0.0, PR3b).

POST /api/v1/optimizer/propose — propõe variantes GROUNDED do system_prompt
de um agente (K variantes LLM com tips de estilo + 1 variante-controle
determinística). REPORT-ONLY: nada é aplicado; o caller roda cada variante
como experimento (PR3a: run_type='experiment' + config_overrides) e decide
pelo veredito pareado do Comparar Execuções.

Gate root/admin: o mesmo racional do config_overrides — propor→rodar injeta
system_prompt arbitrário numa execução com tools reais.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import require_role
from app.core.database import agents_repo, eval_runs_repo, gold_cases_repo, skills_repo
from app.optimizer.proposer import (
    STYLE_TIPS,
    build_control_variant,
    build_proposer_messages,
    parse_proposer_response,
    summarize_gold,
    summarize_last_run,
    variant_leaks_gold,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/optimizer", tags=["optimizer"])


class ProposeRequest(BaseModel):
    agent_id: str
    gold_version: str = "latest"
    # K pequeno de propósito (PR3a: com gold sets pequenos, K grande dilui o
    # poder estatístico — champion×challenger é o desenho honesto).
    n_variants: int = Field(default=2, ge=1, le=3)


@router.post("/propose")
async def propose_variants(data: ProposeRequest, request: Request):
    """Propõe variantes de system_prompt para experimento A/B (report-only)."""
    caller = await require_role("root", "admin")(request)

    agent = await agents_repo.find_by_id(data.agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{data.agent_id}' não encontrado.")

    # Seções de texto livre da skill (contexto grounded) + recusa declarativa
    # (sem LLM não há prompt a otimizar — mesma regra do config_overrides).
    skill_sections: dict | None = None
    if agent.get("skill_id"):
        skill_row = await skills_repo.find_by_id(agent["skill_id"])
        raw = (skill_row or {}).get("raw_content") or ""
        if raw:
            from app.skill_parser.parser import parse_skill_md
            parsed = parse_skill_md(raw)
            if parsed.execution_mode == "declarative":
                raise HTTPException(
                    422, "A skill deste agente é DECLARATIVA (executa sem "
                         "LLM) — não há prompt a otimizar neste alvo.")
            skill_sections = {
                "purpose": parsed.purpose, "workflow": parsed.workflow,
                "output_contract": parsed.output_contract,
                "guardrails": parsed.guardrails, "inputs": parsed.inputs,
            }

    filters = ({"dataset_version": data.gold_version}
               if data.gold_version != "latest" else {})
    cases = await gold_cases_repo.find_all(limit=500, **filters)
    if not cases:
        raise HTTPException(
            422, "Nenhum caso no Golden Dataset para este gold_version — o "
                 "propositor precisa do resumo do gold para fundamentar "
                 "(e o experimento precisaria dele para medir).")
    gold_summary = summarize_gold(cases)

    # Último run CONCLUÍDO do alvo (feedback grounded). Prefere não-experiment
    # (série real); na ausência, um experiment anterior também informa.
    # Filtro por gold_version quando específico (review PR3b [36]): fundamentar
    # a reescrita em falhas de OUTRO dataset descreveria erros que o
    # experimento não vai medir — mesmo precedente do evaluator.
    _run_f = ({"gold_version": data.gold_version}
              if data.gold_version != "latest" else {})
    runs = await eval_runs_repo.find_all(
        agent_id=data.agent_id, status="completed", limit=5, **_run_f)
    last_run_row = next(
        (r for r in runs if (r.get("run_type") or "") != "experiment"),
        runs[0] if runs else None)
    last_run = summarize_last_run(last_run_row)

    # Aviso anti-Goodhart: optimizer == judge → o propositor otimiza para o
    # próprio gosto do juiz que pontuará as variantes. Aviso, não bloqueio —
    # escolha visível do operador (rotas em Configurações → Roteamento LLM).
    warnings: list[str] = []
    _judge_route: tuple | None = None
    from app.llm_routing import resolve_llm_for_task
    provider, model = await resolve_llm_for_task("optimizer")
    try:
        j_provider, j_model = await resolve_llm_for_task("judge")
        _judge_route = (j_provider, j_model)
        if (provider, model) == (j_provider, j_model):
            warnings.append(
                f"A rota LLM do papel 'optimizer' ({provider}/{model}) é a "
                "MESMA do papel 'judge' — o propositor pode otimizar para o "
                "gosto do próprio juiz (Goodhart). Recomendado: modelos "
                "diferentes em Configurações → Roteamento LLM.")
    except Exception:
        pass

    # K variantes LLM em PARALELO (independentes — só a tip muda), com
    # degradação POR VARIANTE (review PR3b: falha na variante i não pode
    # descartar as já geradas e PAGAS — endpoint é report-only, devolve o
    # que conseguiu + avisos; a controle garante resposta útil sempre).
    from app.routes.wizard import _wizard_llm_complete

    async def _gen(i: int):
        style_key, style_tip = STYLE_TIPS[i % len(STYLE_TIPS)]
        messages = build_proposer_messages(
            agent=agent, skill_sections=skill_sections,
            gold_summary=gold_summary, last_run=last_run,
            style_key=style_key, style_tip=style_tip,
        )
        sink: dict = {}
        content, used_p, used_m = await _wizard_llm_complete(
            messages, provider, model, route="optimizer_propose",
            usage_sink=sink)
        return style_key, content, used_p, used_m, sink

    results = await asyncio.gather(
        *(_gen(i) for i in range(data.n_variants)), return_exceptions=True)

    # Custo do propositor no ledger SSOT (review PR3b: o arco inteiro é sobre
    # custo VISÍVEL — a chamada do próprio otimizador não pode ser a exceção).
    from app.core.cost_ledger import record_invocation_cost
    from app.core.llm_pricing import compute_cost
    variants: list[dict] = []
    rejected_leaks = 0
    # eco dos exemplos que NÓS enviamos no contexto é ilustração legítima —
    # só memorização de material NÃO fornecido (gabaritos etc.) é vazamento.
    allow_frags = tuple(gold_summary.get("exemplos_de_entrada") or ())
    for i, res in enumerate(results):
        label = f"variante {i + 1}"
        if isinstance(res, Exception):
            detail = (getattr(res, "detail", None) or type(res).__name__)
            logger.warning("event=optimizer.propose_variant_failed agent=%s "
                           "variant=%s error=%s", data.agent_id, i + 1,
                           str(detail)[:200])
            warnings.append(f"{label}: falha na geração ({str(detail)[:160]}) "
                            "— as demais variantes seguem.")
            continue
        style_key, content, used_p, used_m, sink = res
        usage = (sink or {}).get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        try:
            await record_invocation_cost(
                agent_id=data.agent_id, user_id=caller.get("id"),
                channel="optimizer", source="optimizer",
                cost_usd=float(compute_cost(used_p, used_m, in_tok, out_tok) or 0.0),
                tokens_used=in_tok + out_tok, latency_ms=0.0,
                final_state="ProposeVariant",
            )
        except Exception:
            logger.warning("event=optimizer.cost_ledger_failed", exc_info=True)
        # Goodhart pós-fallback (review PR3b [25]): o aviso pré-chamada compara
        # a rota RESOLVIDA, mas o fallback hospedado pode aterrissar exatamente
        # no modelo do juiz — re-checa com o modelo REALMENTE usado.
        if _judge_route and (used_p, used_m) == _judge_route and \
                (provider, model) != _judge_route:
            warnings.append(
                f"{label} ({style_key}): gerada pelo FALLBACK "
                f"{used_p}/{used_m}, que é o MESMO modelo do papel 'judge' "
                "(o primário do optimizer estava inacessível) — risco de "
                "Goodhart nesta variante.")
        parsed_v = parse_proposer_response(content)
        if not parsed_v:
            warnings.append(f"{label} ({style_key}): resposta do LLM sem "
                            "JSON utilizável — descartada.")
            continue
        # rationale também é varrido (review [29]): é exibido na UI/JSON e
        # não pode carregar gold case verbatim.
        if variant_leaks_gold(
                parsed_v["system_prompt"] + "\n" + parsed_v["rationale"],
                cases, allow_fragments=allow_frags):
            rejected_leaks += 1
            warnings.append(
                f"{label} ({style_key}): REJEITADA por vazamento — continha "
                "trecho verbatim de caso do gold set (memorizar o gabarito "
                "inflaria a métrica sem melhorar o agente).")
            continue
        variants.append({
            "kind": "llm", "style_tip": style_key,
            "system_prompt": parsed_v["system_prompt"],
            "rationale": parsed_v["rationale"],
            "proposed_by": f"{used_p}/{used_m}",
        })

    variants.append(build_control_variant(agent, skill_sections))

    logger.info(
        "event=optimizer.proposed agent=%s llm_variants=%s leaks_rejected=%s",
        data.agent_id, sum(1 for v in variants if v["kind"] == "llm"),
        rejected_leaks,
    )
    return {
        "agent_id": data.agent_id,
        "gold_version": data.gold_version,
        "variants": variants,
        "warnings": warnings,
        "context_summary": {
            "gold": gold_summary,
            "last_run": last_run,
            "optimizer_route": f"{provider}/{model}",
        },
        "how_to_run": (
            "Para medir: rode DOIS experimentos via POST /api/v1/eval-runs/"
            "execute com run_type='experiment' — o champion sem "
            "config_overrides e o challenger com config_overrides="
            "{'system_prompt': <variante>} — e compare na página do Harness "
            "(veredito pareado McNemar)."
        ),
    }
