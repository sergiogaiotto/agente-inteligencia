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
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import require_role
from app.core.database import (
    agents_repo, eval_runs_repo, gold_cases_repo, releases_repo, skills_repo,
)
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


async def _agent_skill_sections(agent: dict, *,
                                reject_declarative: bool = False) -> dict | None:
    """Seções de TEXTO LIVRE da skill do agente para o contexto grounded do
    propositor (49.0.0: SSOT compartilhado entre a rota /propose e o loop
    reflexivo). Sem skill/conteúdo → None. reject_declarative=True levanta 422
    para skills sem LLM (nada a otimizar)."""
    if not agent.get("skill_id"):
        return None
    skill_row = await skills_repo.find_by_id(agent["skill_id"])
    raw = (skill_row or {}).get("raw_content") or ""
    if not raw:
        return None
    from app.skill_parser.parser import parse_skill_md
    parsed = parse_skill_md(raw)
    if parsed.execution_mode == "declarative":
        if reject_declarative:
            raise HTTPException(
                422, "A skill deste agente é DECLARATIVA (executa sem LLM) — "
                     "não há prompt a otimizar neste alvo.")
        return None
    return {
        "purpose": parsed.purpose, "workflow": parsed.workflow,
        "output_contract": parsed.output_contract,
        "guardrails": parsed.guardrails, "inputs": parsed.inputs,
    }


class PromoteRequest(BaseModel):
    """Promoção da variante vencedora (47.0.0, PR5): aplica o system_prompt
    do CHALLENGER ao agente, com o prompt atual preservado como revisão
    restaurável (PR1). force só pula o VEREDITO (inconclusivo/empate);
    não-comparável e sem_pareamento são bloqueios duros."""
    agent_id: str
    champion_eval_id: str
    challenger_eval_id: str
    force: bool = False
    ack_blast: bool = False
    note: str | None = None


class ProposeRequest(BaseModel):
    agent_id: str
    gold_version: str = "latest"
    # K pequeno de propósito (PR3a: com gold sets pequenos, K grande dilui o
    # poder estatístico — champion×challenger é o desenho honesto).
    n_variants: int = Field(default=2, ge=1, le=3)


class OptimizeRequest(BaseModel):
    """Loop reflexivo do otimizador (49.0.0, PR4b): lança o job GEPA."""
    agent_id: str
    release_id: str
    gold_version: str = "latest"
    max_rounds: Optional[int] = Field(default=None, ge=1, le=10)
    children_per_round: Optional[int] = Field(default=None, ge=1, le=4)
    budget_usd: Optional[float] = Field(default=None, ge=0.0, le=1000.0)


@router.post("/optimize")
async def start_optimization(data: OptimizeRequest, request: Request):
    """Lança o loop reflexivo (GEPA-style) como JOB durável — 202 + id, roda
    em background, acompanha em GET /optimizer/optimize/{id}. Report-only: ao
    fim aponta a melhor variante (revisão restaurável) + veredito no holdout;
    a promoção segue humana (PR5). Gate root/admin (dispara muitos runs de
    LLM injetando prompts arbitrários; mesmo racional do /propose e /promote).
    Recusa skill declarativa e gold sem casos de treino."""
    import uuid as _uuid
    from app.core.config import get_settings

    caller = await require_role("root", "admin")(request)
    if not get_settings().optimizer_loop_enabled:
        raise HTTPException(
            403, "O loop reflexivo do otimizador está desligado — habilite "
                 "'optimizer_loop_enabled' em Configurações → Parâmetros "
                 "(dispara muitos runs de LLM; OFF por default).")
    agent = await agents_repo.find_by_id(data.agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{data.agent_id}' não encontrado.")
    if not await releases_repo.find_by_id(data.release_id):
        raise HTTPException(404, f"Release '{data.release_id}' não encontrada.")
    # Declarativa 422 (reusa o helper compartilhado com o /propose).
    await _agent_skill_sections(agent, reject_declarative=True)

    from app.harness.evaluator import gold_version_filters
    cases = await gold_cases_repo.find_all(
        limit=500, **gold_version_filters(data.gold_version))
    train = [c for c in cases if (c.get("split") or "") != "holdout"]
    if len(train) < 4:
        raise HTTPException(
            422, "Menos de 4 casos de TREINO — o loop não teria sinal. "
                 "Adicione casos ao Golden Dataset e rode 'Dividir "
                 "treino/holdout'.")

    s = get_settings()
    opt_id = f"opt_{_uuid.uuid4().hex[:16]}"
    async with _pool_dep().acquire() as con:
        await con.execute(
            "INSERT INTO optimization_runs (id, agent_id, release_id, "
            "gold_version, status, owner_user_id, max_rounds, "
            "children_per_round, budget_usd) "
            "VALUES ($1,$2,$3,$4,'queued',$5,$6,$7,$8)",
            opt_id, data.agent_id, data.release_id, data.gold_version,
            caller.get("id"),
            int(data.max_rounds or s.optimizer_max_rounds),
            int(data.children_per_round or 2),
            float(data.budget_usd if data.budget_usd is not None
                  else s.optimizer_default_budget_usd),
        )
    from app.optimizer import jobs as opt_jobs
    opt_jobs.dispatch(opt_id)  # sem vaga → carona do reaper despacha
    return JSONResponse(status_code=202, content={
        "optimization_id": opt_id, "status": "queued",
        "poll_url": f"/api/v1/optimizer/optimize/{opt_id}",
        "message": "Loop de otimização enfileirado — acompanhe pelo poll_url. "
                   "Report-only: nada é aplicado ao agente."})


@router.get("/optimize/{opt_id}")
async def get_optimization(opt_id: str, request: Request):
    """Estado do loop + árvore de candidatos (polling e drill-down). Gate
    root/admin (review [14]): os candidatos guardam system_prompts — IP de
    prompt-engineering que não deve vazar a qualquer autenticado/API-key."""
    import json as _json
    await require_role("root", "admin")(request)
    async with _pool_dep().acquire() as con:
        run = await con.fetchrow(
            "SELECT * FROM optimization_runs WHERE id=$1", opt_id)
        if not run:
            raise HTTPException(404, "Otimização não encontrada.")
        cands = await con.fetch(
            "SELECT id, round, parent_candidate_id, kind, eval_id, score, "
            "on_pareto, reflection, length(system_prompt) AS prompt_chars "
            "FROM optimization_candidates WHERE optimization_id=$1 "
            "ORDER BY round, score DESC", opt_id)
    run = dict(run)
    if run.get("result"):
        try:
            run["result"] = _json.loads(run["result"])
        except Exception:
            pass
    return {"run": run, "candidates": [dict(c) for c in cands]}


@router.get("/optimize/{opt_id}/candidates/{cand_id}")
async def get_optimization_candidate(opt_id: str, cand_id: str, request: Request):
    """system_prompt completo de um candidato (para ver/diff na UI). Gate
    root/admin (review [14])."""
    await require_role("root", "admin")(request)
    async with _pool_dep().acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM optimization_candidates WHERE id=$1 AND "
            "optimization_id=$2", cand_id, opt_id)
    if not row:
        raise HTTPException(404, "Candidato não encontrado.")
    return dict(row)


def _pool_dep():
    from app.core.database import _get_pool
    return _get_pool()


class ProbeRequest(BaseModel):
    """Sonda go/no-go (48.0.0, PR4a): avalia a variante num MINIBATCH
    estratificado ANTES de gastar o run completo — 49% dos runs de otimização
    ficam abaixo do baseline na literatura; a sonda barata detecta paisagem
    plana e poupa o gold set inteiro."""
    agent_id: str
    release_id: str
    gold_version: str = "latest"
    champion_eval_id: str
    config_overrides: dict
    n_cases: int = Field(default=8, ge=4, le=20)


@router.post("/probe")
async def probe_variant(data: ProbeRequest, request: Request):
    """Mini-experimento REAL (run_type='experiment' com subset de casos do
    TREINO, visível na lista como qualquer run) + veredito pareado contra o
    champion nos MESMOS casos. go = challenger ganha mais casos do que perde;
    no_go = paisagem plana provável — não gaste o run completo."""
    caller = await require_role("root", "admin")(request)
    agent = await agents_repo.find_by_id(data.agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{data.agent_id}' não encontrado.")
    overrides = data.config_overrides or {}
    if set(overrides) - {"system_prompt"} or not isinstance(
            overrides.get("system_prompt"), str) or \
            not overrides["system_prompt"].strip():
        raise HTTPException(422, "config_overrides: sonda aceita apenas "
                                 "{'system_prompt': <string não-vazia>}.")
    champ = await eval_runs_repo.find_by_id(data.champion_eval_id)
    if not champ or (champ.get("run_type") or "") != "experiment" or \
            (champ.get("status") or "") != "completed" or \
            champ.get("agent_id") != data.agent_id:
        raise HTTPException(422, "champion inválido: precisa ser run "
                                 "'experiment' completed deste agente.")
    # Champion = BASELINE (sem overrides), como no /promote (review [10]):
    # senão o GO significaria 'melhor que outra variante', não 'melhor que a
    # config atual do agente'.
    if champ.get("config_overrides"):
        raise HTTPException(422, "champion não pode ter config_overrides — a "
                                 "sonda compara a variante contra a config "
                                 "ATUAL do agente (champion sem variante).")
    from app.routes.dashboard import _paired_comparison, _parse_json_field
    _parse_json_field(champ, "details", [])
    champ_details = [d for d in (champ.get("details") or [])
                     if isinstance(d, dict) and d.get("case_id")]
    if not champ_details:
        raise HTTPException(422, "champion sem details por caso — re-rode o "
                                 "run do champion (runs antigos/truncados não "
                                 "servem de base à sonda).")

    # HOLDOUT fora da sonda SEMPRE (review [7]): os case_ids vêm dos details
    # do champion, que pode ter rodado 'todos' — sem este filtro a sonda
    # mediria no holdout em silêncio. Busca os ids reservados e os exclui.
    from app.harness.evaluator import gold_version_filters
    _gold = await gold_cases_repo.find_all(
        limit=500, **gold_version_filters(data.gold_version))
    holdout_ids = {c.get("id") for c in _gold
                   if (c.get("split") or "") == "holdout"}
    champ_details = [d for d in champ_details
                     if d.get("case_id") not in holdout_ids]
    if not champ_details:
        raise HTTPException(422, "champion só avaliou casos de holdout — a "
                                 "sonda mede no TREINO; rode o champion no "
                                 "treino primeiro.")

    # Minibatch estratificado POR CATEGORIA (round-robin determinístico) a
    # partir dos casos de TREINO que o champion avaliou.
    by_cat: dict[str, list] = {}
    for d in sorted(champ_details, key=lambda x: str(x.get("case_id"))):
        by_cat.setdefault(d.get("category") or "(sem)", []).append(d)
    case_ids: list[str] = []
    while len(case_ids) < data.n_cases and any(by_cat.values()):
        for cat in sorted(by_cat):
            if by_cat[cat] and len(case_ids) < data.n_cases:
                case_ids.append(by_cat[cat].pop(0)["case_id"])
    if len(case_ids) < 4:
        raise HTTPException(422, "champion com menos de 4 casos de treino "
                                 "pareáveis — sonda não teria sinal algum.")

    # Mini-run REAL do challenger, SÓ NO TREINO (gold_split='train' + case_ids
    # já filtrados de holdout = dupla garantia). Timeout de parede (review
    # [4]): a sonda roda INLINE (precisa do veredito síncrono), então um
    # provider lento não pode pendurar o request — cancela e devolve 504.
    import asyncio
    from app.core.config import get_settings as _gs
    from app.harness.evaluator import run_evaluation
    try:
        _to = min(float(_gs().harness_job_timeout_minutes or 60), 15.0) * 60.0
    except Exception:
        _to = 600.0
    try:
        result = await asyncio.wait_for(run_evaluation(
            data.release_id, agent_id=data.agent_id,
            gold_version=data.gold_version, run_type="experiment",
            owner_user_id=caller.get("id"),
            config_overrides=overrides, gold_split="train",
            case_ids=case_ids,
        ), timeout=_to)
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, "A sonda excedeu o tempo limite (provider "
                                 "lento?) — tente com menos casos ou "
                                 "verifique o gateway de modelos.")
    # Mini-run que não avaliou nada (subset esvaziou, no_cases): NÃO é NO-GO —
    # é ausência de sinal (review [12]).
    if (result or {}).get("status") not in ("completed", "budget_exceeded"):
        return {"go": False, "inconclusive": True,
                "probe_eval_id": result.get("eval_id"),
                "note": "Sonda inconclusiva: o mini-run não avaliou casos "
                        f"({result.get('status')}). Verifique o gold set."}
    probe_eval_id = result.get("eval_id")
    chall_row = await eval_runs_repo.find_by_id(probe_eval_id) or {}
    _parse_json_field(chall_row, "details", [])
    _sub = set(case_ids)
    champ_sub = {"details": [d for d in champ_details
                             if d.get("case_id") in _sub],
                 "total_cases": len(case_ids)}
    chall_sub = {"details": [d for d in (chall_row.get("details") or [])
                             if isinstance(d, dict)
                             and d.get("case_id") in _sub],
                 "total_cases": len(case_ids)}
    paired = _paired_comparison(champ_sub, chall_sub)
    # sem_pareamento = ZERO casos pareáveis (não é paisagem plana — é falta de
    # dado). Reportar inconclusivo, não NO-GO (review [12]).
    if paired["verdict"] == "sem_pareamento":
        return {"go": False, "inconclusive": True, "paired": paired,
                "probe_eval_id": probe_eval_id, "case_ids": case_ids,
                "note": "Sonda inconclusiva: nenhum caso pareável entre "
                        "champion e a variante (dados ausentes). Re-rode o "
                        "champion."}
    go = paired["only_b_passes"] > paired["only_a_passes"]
    logger.info("event=optimizer.probe agent=%s go=%s only_b=%s only_a=%s",
                data.agent_id, go, paired["only_b_passes"],
                paired["only_a_passes"])
    return {
        "go": go,
        "paired": paired,
        "probe_eval_id": probe_eval_id,
        "case_ids": case_ids,
        "note": (
            "GO: a variante ganhou casos na sonda — vale o run completo no "
            "treino." if go else
            "NO-GO: paisagem plana provável (a variante não ganhou casos na "
            "sonda) — não gaste o gold set completo com esta variante; "
            "proponha outra ou melhore o gold set."),
    }


@router.post("/promote")
async def promote_variant(data: PromoteRequest, request: Request):
    """Aplica ao agente o system_prompt do challenger VENCEDOR (report→apply
    fechando o ciclo do arco). Honestidade primeiro: exige par de runs
    'experiment' COMPARÁVEIS do mesmo alvo, challenger com selo
    (config_overrides) e champion sem; o veredito pareado (McNemar) guia —
    'b_melhor' promove direto, inconclusivo/empate exigem force explícito.
    Blast-radius: pipelines PUBLICADOS que contêm o agente exigem ack.
    O prompt atual vira revisão restaurável (PR1) antes do apply."""
    import json as _json

    caller = await require_role("root", "admin")(request)
    agent = await agents_repo.find_by_id(data.agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{data.agent_id}' não encontrado.")

    champ = await eval_runs_repo.find_by_id(data.champion_eval_id)
    chall = await eval_runs_repo.find_by_id(data.challenger_eval_id)
    if not champ or not chall:
        raise HTTPException(404, "Run champion/challenger não encontrado.")
    for label, run in (("champion", champ), ("challenger", chall)):
        if (run.get("run_type") or "") != "experiment":
            raise HTTPException(422, f"{label}: run_type deve ser 'experiment' "
                                     "(segregação do arco).")
        if (run.get("status") or "") != "completed":
            raise HTTPException(422, f"{label}: run precisa estar completed "
                                     f"(está {run.get('status')!r}).")
        if run.get("agent_id") != data.agent_id or run.get("pipeline_id"):
            raise HTTPException(422, f"{label}: alvo do run não é este agente.")
    if champ.get("config_overrides"):
        raise HTTPException(422, "champion não pode ter config_overrides — "
                                 "ele é a config ATUAL do agente.")
    try:
        overrides = _json.loads(chall.get("config_overrides") or "null")
    except Exception:
        overrides = None
    if not isinstance(overrides, dict) or not overrides:
        raise HTTPException(422, "challenger sem selo de variante "
                                 "(config_overrides) — nada a promover.")
    if set(overrides) - {"system_prompt"}:
        raise HTTPException(
            422, "Promoção suporta apenas 'system_prompt' por ora — "
                 "skill_purpose será promovível quando o propositor emitir "
                 "variantes de Purpose (edite a skill pelo editor, com "
                 "histórico do PR1).")
    new_prompt = (overrides.get("system_prompt") or "").strip()
    if not new_prompt:
        raise HTTPException(422, "challenger com system_prompt vazio.")

    # Comparabilidade dura (mesmo racional do /eval-runs/compare): dataset
    # idêntico por CONTEÚDO — sem isso o veredito não significa nada.
    if champ.get("gold_version") != chall.get("gold_version"):
        raise HTTPException(409, "Runs de gold_version diferentes — não "
                                 "comparáveis; re-rode o par no mesmo dataset.")
    if champ.get("gold_hash") and chall.get("gold_hash") and \
            champ.get("gold_hash") != chall.get("gold_hash"):
        raise HTTPException(409, "O CONTEÚDO do gold mudou entre os runs "
                                 "(hashes diferentes) — re-rode o par.")

    from app.routes.dashboard import _paired_comparison, _parse_json_field
    for r in (champ, chall):
        _parse_json_field(r, "details", [])
    paired = _paired_comparison(champ, chall)
    if paired["verdict"] == "sem_pareamento":
        raise HTTPException(409, "Nenhum caso pareável entre os runs — "
                                 "promoção às cegas não é permitida (nem com "
                                 "force). Re-rode o par de experimentos.")
    if paired["verdict"] != "b_melhor" and not data.force:
        raise HTTPException(409, {
            "error": "verdict_not_better",
            "message": "O veredito pareado não aponta o challenger como "
                       f"melhor ({paired['verdict']}, p={paired['mcnemar_p']})"
                       " — envie force=true para promover assim mesmo "
                       "(decisão sua, registrada na revisão).",
            "paired": paired,
        })

    # Blast-radius: pipelines PUBLICADOS que contêm o agente mudam de
    # comportamento com o novo prompt (o invoke lê o system_prompt VIVO).
    affected: list[dict] = []
    try:
        from app.core.database import pipelines_repo
        from app.catalog.pipeline_defs import _build_subgraph
        for p in await pipelines_repo.find_all(limit=200):
            try:
                sub = await _build_subgraph(p["id"])
            except Exception:
                continue
            if any(n.get("id") == data.agent_id
                   for n in (sub or {}).get("nodes", [])):
                affected.append({"id": p["id"], "name": p.get("name"),
                                 "status": p.get("status")})
    except Exception:
        logger.warning("event=optimizer.blast_radius_failed", exc_info=True)
    published = [p for p in affected if p.get("status") == "publicado"]
    if published and not data.ack_blast:
        raise HTTPException(409, {
            "error": "blast_radius",
            "message": "O agente participa de pipeline(s) PUBLICADO(s) — o "
                       "novo prompt muda o comportamento deles em produção. "
                       "Envie ack_blast=true para confirmar.",
            "published_pipelines": published,
        })

    # Apply com histórico (PR1): backfill do prompt atual + revisão nova
    # source='promotion' com o SELO completo no note (experimentos, veredito,
    # modelo efetivo — Model Drifting: prompt é artefato acoplado ao modelo).
    from app.core import revisions as _rev
    from app.routes.agents import _bump_version
    await _rev.safe_backfill(
        entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=data.agent_id,
        old_content=agent.get("system_prompt") or "",
        version=agent.get("version"),
    )
    new_version = _bump_version(agent.get("version", "1.0.0"))
    await agents_repo.update(data.agent_id, {
        "system_prompt": new_prompt, "version": new_version})
    sealed = {
        "provider": agent.get("llm_provider"), "model": agent.get("model"),
        "judge_model": chall.get("judge_model"),
        "gold_hash": chall.get("gold_hash"),
        "champion_eval_id": data.champion_eval_id,
        "challenger_eval_id": data.challenger_eval_id,
        "verdict": paired["verdict"], "mcnemar_p": paired["mcnemar_p"],
        "forced": bool(data.force and paired["verdict"] != "b_melhor"),
    }
    note = ("PROMOÇÃO de variante do Experimento de prompt: "
            + _json.dumps(sealed, ensure_ascii=False)
            + (f" | nota: {data.note}" if data.note else ""))
    revision_id = await _rev.safe_record(
        entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=data.agent_id,
        content=new_prompt, version=new_version, source="promotion",
        author_user_id=caller.get("id"), note=note[:2000],
    )
    try:
        from app.core.database import audit_repo
        await audit_repo.create({
            "entity_type": "agent", "entity_id": data.agent_id,
            "action": "prompt_promoted", "actor": caller.get("username"),
            "details": _json.dumps(sealed, ensure_ascii=False)[:2000],
        })
    except Exception:
        logger.warning("event=optimizer.promote_audit_failed", exc_info=True)
    logger.info("event=optimizer.promoted agent=%s challenger=%s verdict=%s",
                data.agent_id, data.challenger_eval_id, paired["verdict"])
    promo_warnings: list[str] = []
    if (chall.get("gold_split") or "") == "train":
        promo_warnings.append(
            "O par foi medido só no TREINO (gold_split='train') — o veredito "
            "pode estar superajustado. Recomendado: confirmar com um par de "
            "runs no HOLDOUT antes de confiar no ganho (48.0.0).")
    return {
        "message": "Variante promovida — o prompt anterior está no Histórico "
                   "de revisões (restaurável).",
        "version": new_version, "revision_id": revision_id,
        "paired": paired, "sealed": sealed,
        "affected_pipelines": affected,
        "warnings": promo_warnings,
        "revalidate_hint": (
            "O selo registra o modelo efetivo do agente na promoção — se a "
            "config de LLM mudar depois, re-rode o experimento (prompt "
            "otimizado não transfere entre modelos)."),
    }


@router.post("/propose")
async def propose_variants(data: ProposeRequest, request: Request):
    """Propõe variantes de system_prompt para experimento A/B (report-only)."""
    caller = await require_role("root", "admin")(request)

    agent = await agents_repo.find_by_id(data.agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{data.agent_id}' não encontrado.")

    # Seções de texto livre da skill (contexto grounded) + recusa declarativa.
    skill_sections = await _agent_skill_sections(agent, reject_declarative=True)

    from app.harness.evaluator import gold_version_filters
    cases = await gold_cases_repo.find_all(
        limit=500, **gold_version_filters(data.gold_version))
    if not cases:
        raise HTTPException(
            422, "Nenhum caso no Golden Dataset para este gold_version — o "
                 "propositor precisa do resumo do gold para fundamentar "
                 "(e o experimento precisaria dele para medir).")
    # Split (48.0.0, PR4a): o propositor SÓ vê o TREINO — holdout é invisível
    # a ele (anti-overfit; a confirmação final roda lá). O detector de
    # vazamento continua varrendo TODOS os casos (defesa em profundidade).
    holdout_ids = {c.get("id") for c in cases
                   if (c.get("split") or "") == "holdout"}
    train_cases = [c for c in cases if c.get("id") not in holdout_ids]
    warnings: list[str] = []
    if train_cases:
        gold_summary = summarize_gold(train_cases)
        if holdout_ids:
            gold_summary["split_note"] = (
                f"resumo do TREINO ({len(train_cases)} casos); "
                f"{len(holdout_ids)} no holdout, invisíveis ao propositor")
    else:
        # Tudo marcado holdout (degenerado): sem treino não há o que otimizar
        # de forma anti-overfit. Usa TODOS os casos mas AVISA (review [8]:
        # antes o fallback vazava o holdout em silêncio).
        gold_summary = summarize_gold(cases)
        warnings.append(
            "Nenhum caso de TREINO (todos marcados holdout ou gold não "
            "dividido corretamente) — o resumo usa todos os casos e o "
            "anti-overfit fica sem holdout. Rode 'Dividir treino/holdout'.")

    # Último run CONCLUÍDO do alvo (feedback grounded). Prefere não-experiment
    # (série real); na ausência, um experiment anterior também informa.
    # Filtro por gold_version quando específico (review PR3b [36]): fundamentar
    # a reescrita em falhas de OUTRO dataset descreveria erros que o
    # experimento não vai medir — mesmo precedente do evaluator.
    runs = await eval_runs_repo.find_all(
        agent_id=data.agent_id, status="completed", limit=5,
        **({"gold_version": data.gold_version}
           if data.gold_version != "latest" else {}))
    last_run_row = next(
        (r for r in runs if (r.get("run_type") or "") != "experiment"),
        runs[0] if runs else None)
    # exclude_case_ids=holdout (review [1]/[6]): o feedback é o grounding mais
    # influente — falhas de casos de holdout NÃO podem chegar ao propositor.
    last_run = summarize_last_run(last_run_row, exclude_case_ids=holdout_ids)

    # Aviso anti-Goodhart: optimizer == judge → o propositor otimiza para o
    # próprio gosto do juiz que pontuará as variantes. Aviso, não bloqueio —
    # escolha visível do operador (rotas em Configurações → Roteamento LLM).
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
