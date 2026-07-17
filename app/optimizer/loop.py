"""Loop reflexivo GEPA-style (49.0.0, PR4b — fecha o arco Otimização).

Orquestra rodadas de {refletir a partir da frente de Pareto → propor filhos →
sondar → avaliar sobreviventes no TREINO → atualizar a frente → early-stop/
budget}, confirmando o melhor no HOLDOUT ao fim. Report-only: aponta a melhor
variante como revisão restaurável (PR1); a promoção segue humana (PR5).

Mecânica GEPA (Agrawal et al. 2025), adaptada:
- FRENTE DE PARETO POR CASO: mantém todo candidato NÃO-dominado (bom em casos
  X, outro bom em casos Y — preserva lições complementares em vez de colapsar
  no argmax da média);
- MUTAÇÃO REFLEXIVA: o propositor recebe as FALHAS capturadas por caso
  (experiment_case_results) e reescreve o prompt — o feedback textual rico é
  a alavanca do GEPA;
- TEACHER = CAMPEÃO DA RODADA (bootstrap×2 do paper do DSPy): o pai vem da
  frente, rotacionado por rodada para diversificar a linhagem;
- MINIBATCH antes da validação completa (a sonda do PR4a) para economizar.

Este módulo separa a LÓGICA PURA (frente/seleção/early-stop — testável sem
I/O) da ORQUESTRAÇÃO async (roda no job durável de app/optimizer/jobs.py).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Lógica PURA (sem I/O — 100% testável) ──────────────────────────────

def pareto_front(candidates: list[dict]) -> list[str]:
    """IDs dos candidatos NÃO-dominados (frente de Pareto por caso).

    `candidates`: [{'id': str, 'passes': set|list de case_ids}].
    C domina D  ⇔  passes(C) ⊇ passes(D) E passes(C) ≠ passes(D). Mantemos
    todo candidato que nenhum outro domina — assim um prompt bom só nos casos
    de roteamento e outro bom só nos de extração SOBREVIVEM juntos (o argmax
    da média descartaria um deles). Empates de pass-set exato: ambos ficam."""
    sets = {c["id"]: set(c.get("passes") or ()) for c in candidates}
    front = []
    for cid, ps in sets.items():
        dominated = any(
            oid != cid and ps < os_  # subconjunto ESTRITO = dominado
            for oid, os_ in sets.items()
        )
        if not dominated:
            front.append(cid)
    return front


def select_parent(candidates: list[dict], front_ids: list[str],
                  round_index: int) -> Optional[dict]:
    """Escolhe o PAI da próxima rodada a partir da frente (teacher=campeão).

    Determinístico (reprodutível/testável; sem Math.random): ordena a frente
    por score desc, id asc e rotaciona por `round_index` — cada rodada parte
    de uma linhagem diferente da frente, aproximando a amostragem por
    cobertura do GEPA sem aleatoriedade. Retorna o dict do candidato."""
    if not front_ids:
        return None
    by_id = {c["id"]: c for c in candidates}
    ordered = sorted(
        (by_id[i] for i in front_ids if i in by_id),
        key=lambda c: (-(c.get("score") or 0.0), str(c.get("id"))),
    )
    if not ordered:
        return None
    return ordered[round_index % len(ordered)]


def should_stop(*, rounds_done: int, max_rounds: int,
                best_score_history: list[float], patience: int,
                budget_usd: float, spent_usd: float) -> tuple[bool, str]:
    """Decisão de parada. Retorna (parar, motivo).

    Para quando: (a) atingiu max_rounds; (b) budget esgotado (0 = sem teto);
    (c) sem MELHORA do melhor score por `patience` rodadas seguidas (o tail
    de otimização overfitta — resultado negativo replicado no plano)."""
    if rounds_done >= max_rounds:
        return True, f"max_rounds ({max_rounds}) atingido"
    if budget_usd > 0 and spent_usd >= budget_usd:
        return True, f"budget esgotado (US$ {spent_usd:.4f} ≥ {budget_usd:.2f})"
    if len(best_score_history) > patience:
        recent = best_score_history[-(patience + 1):]
        if max(recent[1:]) <= recent[0] + 1e-9:
            return True, (f"sem melhora em {patience} rodada(s) — early-stop "
                          "(evita overfit do tail)")
    return False, ""


def passes_from_details(details: list) -> set:
    """Conjunto de case_ids que PASSARAM, a partir do details de um eval_run."""
    out = set()
    for d in details or []:
        if isinstance(d, dict) and d.get("case_id") and d.get("passed"):
            out.add(d["case_id"])
    return out


# ─── Orquestração ASYNC (roda no job durável) ───────────────────────────

def _pool():
    from app.core.database import _get_pool
    return _get_pool()


async def _load_run(opt_id: str) -> Optional[dict]:
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM optimization_runs WHERE id=$1", opt_id)
    return dict(row) if row else None


async def _update_run(opt_id: str, fields: dict) -> None:
    if not fields:
        return
    keys = list(fields)
    sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(keys))
    async with _pool().acquire() as con:
        await con.execute(
            f"UPDATE optimization_runs SET {sets}, updated_at=now() WHERE id=$1",
            opt_id, *[fields[k] for k in keys])


async def _insert_candidate(opt_id: str, *, round: int, parent: Optional[str],
                            kind: str, system_prompt: str, eval_id: str,
                            passes: set, score: float, on_pareto: bool,
                            reflection: Optional[str]) -> str:
    cid = f"oc_{uuid.uuid4().hex[:16]}"
    async with _pool().acquire() as con:
        await con.execute(
            "INSERT INTO optimization_candidates (id, optimization_id, round, "
            "parent_candidate_id, kind, system_prompt, eval_id, passes, score, "
            "on_pareto, reflection) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            cid, opt_id, round, parent, kind, system_prompt, eval_id,
            json.dumps(sorted(passes)), float(score), bool(on_pareto),
            reflection,
        )
    return cid


async def _mark_pareto(opt_id: str, front_ids: set) -> None:
    async with _pool().acquire() as con:
        await con.execute(
            "UPDATE optimization_candidates SET on_pareto = (id = ANY($2)) "
            "WHERE optimization_id=$1", opt_id, list(front_ids))


async def _run_variant_on_train(*, opt: dict, system_prompt: Optional[str],
                                case_ids: Optional[list], caller_id: str) -> dict:
    """Roda uma variante (ou o champion, quando system_prompt=None) no TREINO
    e devolve {eval_id, passes, score, cost}. Reusa run_evaluation com o seam
    de config_overrides e gold_split='train'.

    CRÍTICO (review PR4b [1]): pass-set e score vêm dos dados COMPLETOS, não
    do `details` do eval_run (que o evaluator trunca em ≤100 casos, ou até 10
    sob 32KB) — recontar de details subestima o score e corrompe a dominância
    de Pareto em gold >100 casos. score = passed/total_cases (colunas do run,
    sobre o conjunto INTEIRO); passes = case_ids de experiment_case_results
    (uma linha por caso, SEM truncamento)."""
    from app.harness.evaluator import run_evaluation
    overrides = {"system_prompt": system_prompt} if system_prompt else None
    result = await run_evaluation(
        opt["release_id"], agent_id=opt["agent_id"],
        gold_version=opt.get("gold_version") or "latest",
        run_type="experiment", owner_user_id=caller_id,
        config_overrides=overrides, gold_split="train", case_ids=case_ids,
    )
    eval_id = result.get("eval_id")
    row = await _load_eval(eval_id)
    total = int(row.get("total_cases") or 0) or 1
    passed = int(row.get("passed") or 0)
    passes = await _passes_from_captures(eval_id)
    return {
        "eval_id": eval_id, "passes": passes,
        "score": passed / total,
        "cost": float(row.get("cost_usd") or 0.0),
    }


async def _passes_from_captures(eval_id: str) -> set:
    """case_ids que PASSARAM, de experiment_case_results (captura COMPLETA por
    caso — sem o truncamento de details). Fallback vazio se a captura falhou."""
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT case_id FROM experiment_case_results "
            "WHERE eval_id=$1 AND passed = TRUE", eval_id)
    return {r["case_id"] for r in rows}


async def _load_eval(eval_id: str) -> dict:
    async with _pool().acquire() as con:
        row = await con.fetchrow("SELECT * FROM eval_runs WHERE id=$1", eval_id)
    return dict(row) if row else {}


async def _captured_failures(eval_id: str, limit: int = 12) -> list[dict]:
    """Falhas capturadas por caso (experiment_case_results) do run — o
    feedback textual rico que a mutação reflexiva do GEPA consome."""
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT case_id, output, failure_reasons FROM experiment_case_results "
            "WHERE eval_id=$1 AND passed = FALSE ORDER BY case_id LIMIT $2",
            eval_id, limit)
    out = []
    for r in rows:
        try:
            reasons = json.loads(r["failure_reasons"] or "[]")
        except Exception:
            reasons = []
        out.append({"case_id": r["case_id"],
                    "output": (r["output"] or "")[:400],
                    "motivos": reasons[:3]})
    return out


async def run_optimization(opt_id: str, *, deadline_s: float) -> None:
    """Executa o loop reflexivo de UMA optimization_run (já claimada
    'running'). Report-only: persiste a árvore de candidatos e aponta o melhor
    + veredito no holdout; NÃO aplica ao agente (promoção humana via PR5).

    Best-effort de robustez: exceção marca o run 'failed' (o worker do job
    também cobre timeout). O deadline é o tempo de parede TOTAL do loop."""
    import time
    t0 = time.monotonic()
    # Reserva 20% do deadline para a fase PÓS-loop (confirmação no holdout =
    # 2 evals) — review [11]: sem isso o wait_for do job cancelava o holdout à
    # força. As RODADAS param neste sub-deadline; o job dá margem extra.
    rounds_deadline_s = deadline_s * 0.8
    opt = await _load_run(opt_id)
    if not opt:
        return
    caller_id = opt.get("owner_user_id")

    from app.core.config import get_settings
    from app.optimizer.proposer import (
        build_control_variant, build_proposer_messages, parse_proposer_response,
        summarize_gold, variant_leaks_gold,
    )
    from app.routes.optimizer import _agent_skill_sections  # helper compartilhado

    # Contexto grounded (uma vez): agente, seções da skill, resumo do TREINO.
    from app.core.database import agents_repo, gold_cases_repo
    from app.harness.evaluator import gold_version_filters
    agent = await agents_repo.find_by_id(opt["agent_id"])
    if not agent:
        await _update_run(opt_id, {"status": "failed", "error": "agent_gone"})
        return
    skill_sections = await _agent_skill_sections(agent)
    all_cases = await gold_cases_repo.find_all(
        limit=500, **gold_version_filters(opt.get("gold_version") or "latest"))
    holdout_ids = {c.get("id") for c in all_cases
                   if (c.get("split") or "") == "holdout"}
    train_cases = [c for c in all_cases if c.get("id") not in holdout_ids]
    gold_summary = summarize_gold(train_cases or all_cases)

    # Rota LLM do propositor (papel 'optimizer', PR3b).
    from app.llm_routing import resolve_llm_for_task
    provider, model = await resolve_llm_for_task("optimizer")

    settings = get_settings()
    patience = max(1, int(getattr(settings, "optimizer_patience", 2)))
    budget_usd = float(opt.get("budget_usd") or 0.0)
    max_rounds = int(opt.get("max_rounds") or 4)
    children_per_round = int(opt.get("children_per_round") or 2)

    candidates: list[dict] = []   # em memória: {id, passes, score, system_prompt}
    spent = 0.0
    best_history: list[float] = []

    # ── Semente: o CHAMPION (config atual) avaliado no treino ──
    champ = await _run_variant_on_train(
        opt=opt, system_prompt=None, case_ids=None, caller_id=caller_id)
    spent += champ["cost"]
    champ_cid = await _insert_candidate(
        opt_id, round=0, parent=None, kind="champion",
        system_prompt=(agent.get("system_prompt") or ""),
        eval_id=champ["eval_id"], passes=champ["passes"], score=champ["score"],
        on_pareto=True, reflection="config atual do agente (baseline)")
    candidates.append({"id": champ_cid, "passes": champ["passes"],
                       "score": champ["score"],
                       "system_prompt": agent.get("system_prompt") or "",
                       "eval_id": champ["eval_id"]})
    best_history.append(champ["score"])
    await _update_run(opt_id, {"rounds_done": 0, "cost_usd": spent})

    # Eco legítimo dos exemplos que o contexto envia ao propositor (review [8]).
    _allow_frags = tuple(gold_summary.get("exemplos_de_entrada") or ())
    # Modelo efetivo do agente na otimização (Model Drifting — review [6]).
    _eff_model = f"{agent.get('llm_provider')}/{agent.get('model')}"

    # ── Rodadas reflexivas ──
    round_no = 0
    stop_reason = ""
    while True:
        stop, stop_reason = should_stop(
            rounds_done=round_no, max_rounds=max_rounds,
            best_score_history=best_history, patience=patience,
            budget_usd=budget_usd, spent_usd=spent)
        if stop:
            break
        if (time.monotonic() - t0) > rounds_deadline_s:
            stop_reason = ("deadline de parede das rodadas atingido (reserva "
                           "p/ confirmação no holdout)")
            break
        round_no += 1

        front_ids = pareto_front(candidates)
        parent = select_parent(candidates, front_ids, round_no - 1)
        if not parent:
            stop_reason = "frente de Pareto vazia"
            break

        # Feedback textual rico: falhas capturadas do PAI (μ_f do GEPA).
        failures = await _captured_failures(parent["eval_id"])
        last_run_ctx = {"run_type": "experiment",
                        "accuracy": parent.get("score"),
                        "falhas": failures}

        produced = 0
        for k in range(children_per_round):
            if budget_usd > 0 and spent >= budget_usd:
                stop_reason = "budget esgotado no meio da rodada"
                break
            style_key, style_tip = _round_tip(round_no, k)
            messages = build_proposer_messages(
                agent={**agent, "system_prompt": parent["system_prompt"]},
                skill_sections=skill_sections, gold_summary=gold_summary,
                last_run=last_run_ctx, style_key=style_key, style_tip=style_tip)
            try:
                content, _p, _m, _pcost = await _propose(
                    messages, provider, model,
                    agent_id=opt["agent_id"], user_id=caller_id)
                spent += _pcost  # custo do propositor conta no teto (review [2])
            except Exception:
                logger.warning("event=optimizer.loop_propose_failed opt=%s round=%s",
                               opt_id, round_no)
                continue
            parsed = parse_proposer_response(content)
            if not parsed:
                continue
            child_prompt = parsed["system_prompt"]
            # Guard de vazamento (defesa em profundidade — PR3b): rejeita
            # variante que ecoe gabarito de gold case. allow_fragments = os
            # exemplos que o PRÓPRIO contexto enviou (review [8]: ecoá-los é
            # ilustração legítima, não memorização).
            if variant_leaks_gold(child_prompt, all_cases,
                                  allow_fragments=_allow_frags):
                continue
            # Avalia o filho no TREINO.
            res = await _run_variant_on_train(
                opt=opt, system_prompt=child_prompt, case_ids=None,
                caller_id=caller_id)
            spent += res["cost"]
            child_cid = await _insert_candidate(
                opt_id, round=round_no, parent=parent["id"], kind="llm",
                system_prompt=child_prompt, eval_id=res["eval_id"],
                passes=res["passes"], score=res["score"], on_pareto=False,
                reflection=(parsed.get("rationale") or "")[:1000])
            candidates.append({"id": child_cid, "passes": res["passes"],
                               "score": res["score"],
                               "system_prompt": child_prompt,
                               "eval_id": res["eval_id"]})
            produced += 1

        # Variante-CONTROLE determinística uma única vez (rodada 1) — braço de
        # controle barato do paper do DSPy. Guard de budget (review [5]: antes
        # rodava mesmo com o teto já estourado no loop de filhos).
        if round_no == 1 and not (budget_usd > 0 and spent >= budget_usd):
            ctrl = build_control_variant(agent, skill_sections)
            if not variant_leaks_gold(ctrl["system_prompt"], all_cases,
                                      allow_fragments=_allow_frags):
                res = await _run_variant_on_train(
                    opt=opt, system_prompt=ctrl["system_prompt"],
                    case_ids=None, caller_id=caller_id)
                spent += res["cost"]
                ccid = await _insert_candidate(
                    opt_id, round=round_no, parent=champ_cid, kind="control",
                    system_prompt=ctrl["system_prompt"], eval_id=res["eval_id"],
                    passes=res["passes"], score=res["score"], on_pareto=False,
                    reflection=ctrl["rationale"])
                candidates.append({"id": ccid, "passes": res["passes"],
                                   "score": res["score"],
                                   "system_prompt": ctrl["system_prompt"],
                                   "eval_id": res["eval_id"]})

        best_history.append(max(c["score"] for c in candidates))
        await _update_run(opt_id, {"rounds_done": round_no, "cost_usd": spent})
        if produced == 0:
            stop_reason = "nenhuma variante válida gerada na rodada"
            break

    # Frente final + melhor candidato (por score de TREINO, tie por id).
    front_ids = set(pareto_front(candidates))
    await _mark_pareto(opt_id, front_ids)
    best = max(candidates, key=lambda c: (c["score"], -len(c["id"])))
    champ_score = candidates[0]["score"]

    train_improved = best["id"] != champ_cid and best["score"] > champ_score

    # ── Confirmação no HOLDOUT ──
    # Rótulos distintos (review [4]): 'sem_holdout' = NÃO há dataset de holdout;
    # 'nao_confirmado_sem_ganho' = há holdout mas o treino não melhorou (nem
    # rodamos). O veredito de confirmação vem do McNemar sobre as CAPTURAS
    # completas (review [3]: details truncado ≤100 daria falsa confiança).
    holdout_verdict = "sem_holdout"
    holdout_note = ""
    if not holdout_ids:
        holdout_note = ("sem holdout — rode 'Dividir treino/holdout' para "
                        "confirmar o ganho fora do treino antes de promover")
    elif not train_improved:
        holdout_verdict = "nao_confirmado_sem_ganho"
        holdout_note = "nenhuma variante superou o champion no treino"
    else:
        h_champ = await _run_variant_on_train_holdout(
            opt, None, list(holdout_ids), caller_id)
        h_best = await _run_variant_on_train_holdout(
            opt, best["system_prompt"], list(holdout_ids), caller_id)
        spent += h_champ["cost"] + h_best["cost"]
        from app.routes.dashboard import _paired_comparison
        paired = _paired_comparison(
            {"details": h_champ["details"], "total_cases": len(holdout_ids)},
            {"details": h_best["details"], "total_cases": len(holdout_ids)})
        holdout_verdict = paired["verdict"]
        holdout_note = paired["verdict_note"]
        if paired.get("truncated"):
            holdout_note += " ⚠ pareamento PARCIAL (holdout > detalhes)"

    # improved HONESTO (review [7]): com holdout, exige que ele CONFIRME
    # ('b_melhor'); sem holdout, cai no ganho de treino (com aviso no note).
    if holdout_ids and train_improved:
        improved = holdout_verdict == "b_melhor"
    else:
        improved = train_improved

    # ── Report-only: melhor variante como REVISÃO restaurável (PR1) ──
    # Salva SÓ quando confirmado (holdout) ou, sem holdout, quando o treino
    # melhorou — nunca uma variante que o holdout DESCONFIRMOU.
    best_revision_id = None
    if best["id"] != champ_cid and improved:
        from app.core import revisions as _rev
        best_revision_id = await _rev.safe_record(
            entity_type=_rev.ENTITY_AGENT_PROMPT, entity_id=opt["agent_id"],
            content=best["system_prompt"], source="optimizer_candidate",
            author_user_id=caller_id,
            note=("melhor variante do loop reflexivo (opt " + opt_id + "): "
                  + json.dumps({"train_score": round(best["score"], 4),
                                "champion_score": round(champ_score, 4),
                                "rounds": round_no,
                                "holdout_verdict": holdout_verdict,
                                # Model Drifting (review [6]): o prompt é
                                # artefato acoplado ao modelo em que foi tunado.
                                "model": _eff_model},
                               ensure_ascii=False))[:2000])

    result = {
        "rounds": round_no, "stop_reason": stop_reason,
        "champion_score": round(champ_score, 4),
        "best_score": round(best["score"], 4),
        "train_improved": train_improved, "improved": improved,
        "holdout_verdict": holdout_verdict, "holdout_note": holdout_note,
        "model": _eff_model,
        "candidates_evaluated": len(candidates),
        "cost_usd": round(spent, 6),
    }
    await _update_run(opt_id, {
        "status": "completed", "rounds_done": round_no, "cost_usd": spent,
        "best_candidate_id": best["id"], "best_revision_id": best_revision_id,
        "holdout_verdict": holdout_verdict,
        "result": json.dumps(result, ensure_ascii=False)[:8000]})
    logger.info("event=optimizer.loop_done opt=%s rounds=%s best=%.3f champ=%.3f "
                "holdout=%s cost=%.4f", opt_id, round_no, best["score"],
                champ_score, holdout_verdict, spent)


async def _run_variant_on_train_holdout(opt, system_prompt, case_ids, caller_id):
    """Variante avaliada no HOLDOUT (gold_split='holdout' + case_ids).

    details reconstruídos das CAPTURAS completas (review [3]): experiment_
    case_results tem 1 linha por caso — assim o _paired_comparison enxerga
    TODO o holdout (truncated=False) em vez do details capado em ≤100."""
    from app.harness.evaluator import run_evaluation
    overrides = {"system_prompt": system_prompt} if system_prompt else None
    result = await run_evaluation(
        opt["release_id"], agent_id=opt["agent_id"],
        gold_version=opt.get("gold_version") or "latest",
        run_type="experiment", owner_user_id=caller_id,
        config_overrides=overrides, gold_split="holdout", case_ids=case_ids)
    eval_id = result.get("eval_id")
    row = await _load_eval(eval_id)
    async with _pool().acquire() as con:
        crows = await con.fetch(
            "SELECT case_id, passed FROM experiment_case_results WHERE eval_id=$1",
            eval_id)
    details = [{"case_id": r["case_id"], "passed": bool(r["passed"])}
               for r in crows]
    return {"cost": float(row.get("cost_usd") or 0.0), "details": details}


def _round_tip(round_no: int, k: int) -> tuple[str, str]:
    """Tip de estilo por (rodada, filho) — diversifica as mutações."""
    from app.optimizer.proposer import STYLE_TIPS
    return STYLE_TIPS[(round_no + k) % len(STYLE_TIPS)]


async def _propose(messages, provider, model, *, agent_id=None, user_id=None):
    """Chamada ao propositor com custo no ledger (source='optimizer',
    ATRIBUÍDO ao agente/usuário — review [9]). Retorna (content, provider,
    model, cost) — o cost VOLTA ao acumulador do loop (review [2]: senão o
    teto de budget e o cost_usd reportado ignoravam o gasto do propositor)."""
    from app.core.cost_ledger import record_invocation_cost
    from app.core.llm_pricing import compute_cost
    from app.routes.wizard import _wizard_llm_complete
    sink: dict = {}
    content, used_p, used_m = await _wizard_llm_complete(
        messages, provider, model, route="optimizer_loop", usage_sink=sink)
    usage = (sink or {}).get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cost = float(compute_cost(used_p, used_m, in_tok, out_tok) or 0.0)
    try:
        await record_invocation_cost(
            agent_id=agent_id, user_id=user_id,
            channel="optimizer", source="optimizer",
            cost_usd=cost, tokens_used=in_tok + out_tok, latency_ms=0.0,
            final_state="LoopPropose")
    except Exception:
        pass
    return content, used_p, used_m, cost
