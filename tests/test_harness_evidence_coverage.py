"""evidence_coverage do eval_run deixa de ser coluna morta (66.4.4).

Achado do E2E 2026-07-21 (workflow de leitores + grep manual): a ÚNICA
ocorrência de evidence_coverage no app era o `REAL DEFAULT 0` do schema —
nenhum writer. Todo run exibia 0 FABRICADO, mesmo com grounding perfeito ao
vivo (steps com evidence_score 0.9–1.0 no Playground), minando a confiança
na métrica ("métrica sem falsa confiança": antes 0 = mentira; agora 0 = run
realmente sem RAG).

Contrato selado:
 1. modo PIPELINE — dono do output com evidence_score>0 → coverage 1.0 no
    UPDATE persistido, no retorno e no drill-down do caso (details[]);
 2. sem grounding em lugar nenhum → coverage 0.0 LEGÍTIMO;
 3. modo AGENTE — envelope top-level com evidence_score>0 → coverage 1.0;
 4. misto (1 groundeado + 1 sem) → 0.5; caso com ERRO de invoke fica fora
    do denominador (não dá para saber se teria evidência).

Mocks em app.harness.evaluator (repos/engine) — sem DB/LLM reais (mesmo
padrão de test_harness_pipeline_mode).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.harness.evaluator as evaluator


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _settings_stub(**over):
    base = dict(
        harness_use_verifier=False, verifier_v2_enabled=False,
        ragas_ground_truth_enabled=False,
        harness_min_accuracy=0.80, harness_min_avg_factuality=3.5,
        harness_min_avg_completeness=3.0, harness_min_avg_tone=3.0,
        harness_max_safety_violation_rate=0.05,
        harness_min_contract_compliance=0.95,
        harness_max_hallucination_rate=0.10,
        harness_max_dim_regression_pct=5.0,
        harness_max_regression_pct=5.0,
        harness_phrases_gate=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _gold(i="gc1"):
    return {
        "id": i, "input_text": "minha internet caiu",
        "expected_state": "Recommend", "expected_output": "", "weight": 1.0,
        "category": "tecnico", "case_type": "normal", "channel": "api",
        "red_flags": None,
    }


def _pipe_result(owner_ev_score):
    return {
        "output": "Diagnóstico com base no runbook.",
        "final_state": "LogAndClose",
        "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
        "interaction_id": "it-1",
        "duration_ms": 100.0,
        "pipeline_steps": [
            {"agent_name": "Maestro", "status": "completed", "evidence_score": 0},
            {"agent_name": "Esp. Técnico", "status": "completed",
             "final_state": "LogAndClose",
             "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
             "evidence_score": owner_ev_score},
        ],
    }


def _wire(monkeypatch, *, cases, pipeline=True, results=None):
    updated: dict = {}

    async def _update(_id, data):
        updated.update(data)

    async def _find_all_runs(**kw):
        return []

    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _async(None))
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _find_all_runs)
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async(cases))
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())
    if pipeline:
        monkeypatch.setattr(
            evaluator.pipelines_repo, "find_by_id",
            _async({"id": "p1", "name": "Farol", "status": "publicado"}))
        monkeypatch.setattr(
            "app.catalog.pipeline_defs._build_subgraph",
            _async({"root_agent_id": "root-1",
                    "nodes": [{"id": "root-1"}, {"id": "a2"}]}))
        mock = AsyncMock(side_effect=results)
        monkeypatch.setattr(evaluator, "execute_pipeline", mock)
    else:
        monkeypatch.setattr(evaluator.agents_repo, "find_by_id",
                            _async({"id": "a1", "name": "Solo"}))
        mock = AsyncMock(side_effect=results)
        monkeypatch.setattr(evaluator, "execute_interaction", mock)
    return updated


@pytest.mark.asyncio
async def test_pipeline_groundeado_persiste_coverage_1(monkeypatch):
    updated = _wire(monkeypatch, cases=[_gold()],
                    results=[_pipe_result(0.9)])
    out = await evaluator.run_evaluation("r1", pipeline_id="p1")
    assert updated["evidence_coverage"] == 1.0
    assert out["evidence_coverage"] == 1.0
    details = json.loads(updated["details"])
    assert details[0]["evidence_score"] == 0.9


@pytest.mark.asyncio
async def test_pipeline_sem_rag_coverage_zero_legitimo(monkeypatch):
    updated = _wire(monkeypatch, cases=[_gold()],
                    results=[_pipe_result(0)])
    out = await evaluator.run_evaluation("r1", pipeline_id="p1")
    assert updated["evidence_coverage"] == 0.0
    assert out["evidence_coverage"] == 0.0


@pytest.mark.asyncio
async def test_modo_agente_le_o_envelope_top_level(monkeypatch):
    result = {
        "output": "resposta groundeada", "final_state": "LogAndClose",
        "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
        "interaction_id": "it-2", "duration_ms": 80.0,
        "evidence_score": 0.7,
    }
    updated = _wire(monkeypatch, cases=[_gold()], pipeline=False,
                    results=[result])
    out = await evaluator.run_evaluation("r1", agent_id="a1")
    assert updated["evidence_coverage"] == 1.0
    assert out["evidence_coverage"] == 1.0


@pytest.mark.asyncio
async def test_misto_e_erro_fora_do_denominador(monkeypatch):
    # 3 casos: groundeado + sem RAG + ERRO de invoke → coverage = 1/2 (o
    # errado fica fora do denominador; não dá p/ saber se teria evidência).
    updated = _wire(
        monkeypatch, cases=[_gold("g1"), _gold("g2"), _gold("g3")],
        results=[_pipe_result(0.9), _pipe_result(0),
                 RuntimeError("invoke quebrou")])
    out = await evaluator.run_evaluation("r1", pipeline_id="p1")
    assert updated["evidence_coverage"] == 0.5
    assert out["evidence_coverage"] == 0.5
