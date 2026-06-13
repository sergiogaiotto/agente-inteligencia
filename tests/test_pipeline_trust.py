"""PR7 (Parte B, último) — observabilidade/trust de pipelines + cost auto-wire.

Cobre: _compute_trust (reliability/p95/avg, puro); execute_pipeline_entry soma
custo REAL dos steps (cost auto-wire) e chama recompute_entry_trust (não-sandbox).
"""
import asyncio

import app.catalog.executor as ex
import app.agents.engine as engine
from app.catalog.queries import _compute_trust
from app.agents.engine import _step_cost_and_tokens
from app.core.llm_pricing import compute_cost


def _noop_async(value=None):
    async def _fn(*a, **k):
        return value
    return _fn


# ───────────── _compute_trust (puro) ─────────────
def test_compute_trust_reliability_and_avg():
    rows = [
        {"status": "completed", "total_latency_ms": 100, "total_cost_usd": 0.02},
        {"status": "completed", "total_latency_ms": 200, "total_cost_usd": 0.04},
        {"status": "failed", "total_latency_ms": 300, "total_cost_usd": 0.0},
        {"status": "running", "total_latency_ms": 0, "total_cost_usd": 0.0},  # ignorado
    ]
    t = _compute_trust(rows)
    assert t["n"] == 3                       # 'running' excluído
    assert t["reliability"] == round(2 / 3, 4)
    assert t["avg_cost_usd"] == round((0.02 + 0.04 + 0.0) / 3, 6)
    assert t["latency_p95_ms"] == 300.0      # p95 de [100,200,300]


def test_compute_trust_empty():
    t = _compute_trust([])
    assert t == {"reliability": 0.0, "latency_p95_ms": 0.0, "avg_cost_usd": 0.0, "n": 0}
    assert _compute_trust([{"status": "running", "total_latency_ms": 5, "total_cost_usd": 1}])["n"] == 0


def test_compute_trust_all_completed_is_full_reliability():
    rows = [{"status": "completed", "total_latency_ms": 50, "total_cost_usd": 0.01} for _ in range(4)]
    t = _compute_trust(rows)
    assert t["reliability"] == 1.0
    assert t["n"] == 4


# ───────────── _step_cost_and_tokens: desempacota result['trace'] (regressão) ─────────────
def test_step_cost_reads_tokens_from_trace_not_toplevel():
    # Shape REAL do execute_interaction: tokens em trace['tokens'], modelo em
    # trace['agent_model']. (O bug do review lia chaves top-level inexistentes → 0.)
    model = "gpt-4o"
    result = {
        "output": "x",
        "trace": {"tokens": {"input": 1000, "output": 500, "total": 1500},
                   "agent_provider": "azure", "agent_model": model},
    }
    cost, tokens = _step_cost_and_tokens(result, {"llm_provider": "azure", "model": model})
    assert tokens == 1500
    expected = compute_cost("azure", model, 1000, 500)
    assert cost == float(expected or 0.0)
    if expected:   # modelo precificado → custo real > 0 (não o 0 do bug)
        assert cost > 0

def test_step_cost_defensive_when_no_trace():
    cost, tokens = _step_cost_and_tokens({"output": "x"}, {"llm_provider": "azure", "model": "gpt-4o"})
    assert cost == 0.0 and tokens == 0


# ───────────── execute_pipeline_entry: custo real + recompute_entry_trust ─────────────
def test_execute_pipeline_entry_sums_real_cost_and_recomputes_trust(monkeypatch):
    calls = {"finalize": None, "cost": None, "trust": None}
    monkeypatch.setattr(engine, "execute_pipeline", _noop_async({
        "pipeline_steps": [
            {"agent_id": "a", "status": "completed", "cost_usd": 0.03, "tokens_used": 120},
            {"agent_id": "b", "status": "completed", "cost_usd": 0.05, "tokens_used": 200},
        ],
        "completed_agents": 2, "duration_ms": 90, "interaction_id": "int1", "status": "completed",
    }))
    monkeypatch.setattr(ex, "append_step_result", _noop_async())
    async def fake_finalize(eid, **k): calls["finalize"] = k
    async def fake_cost(entry_id, **k): calls["cost"] = {"entry_id": entry_id, **k}
    async def fake_trust(entry_id): calls["trust"] = entry_id
    monkeypatch.setattr(ex, "finalize_execution", fake_finalize)
    monkeypatch.setattr(ex, "record_invocation_cost", fake_cost)
    monkeypatch.setattr(ex, "recompute_entry_trust", fake_trust)
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u"}, user_input="oi",
    ))
    # custo somado REAL dos steps (0.03 + 0.05)
    assert abs(calls["finalize"]["total_cost_usd"] - 0.08) < 1e-9
    assert abs(calls["cost"]["cost_usd"] - 0.08) < 1e-9
    assert calls["cost"]["tokens_used"] == 320          # 120 + 200 (real)
    assert calls["trust"] == "pe"                       # trust recomputado na entry


def test_execute_pipeline_entry_sandbox_skips_trust(monkeypatch):
    calls = {"cost": False, "trust": False}
    monkeypatch.setattr(engine, "execute_pipeline", _noop_async({
        "pipeline_steps": [{"agent_id": "a", "status": "completed", "cost_usd": 0.01, "tokens_used": 10}],
        "completed_agents": 1, "duration_ms": 5, "interaction_id": "i", "status": "completed",
    }))
    monkeypatch.setattr(ex, "append_step_result", _noop_async())
    monkeypatch.setattr(ex, "finalize_execution", _noop_async())
    async def fake_cost(*a, **k): calls["cost"] = True
    async def fake_trust(*a, **k): calls["trust"] = True
    monkeypatch.setattr(ex, "record_invocation_cost", fake_cost)
    monkeypatch.setattr(ex, "recompute_entry_trust", fake_trust)
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="a",
        consumer_user={"id": "u"}, user_input="oi", is_sandbox=True,
    ))
    assert calls["cost"] is False    # sandbox não grava custo
    assert calls["trust"] is False   # nem recomputa trust
