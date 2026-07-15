"""Guarda de ALVO no compare + filtros por alvo no GET /eval-runs (item 5 PR1).

Antes: comparar agente A vs pipeline B (ou dois agentes diferentes) passava
silenciosamente como comparable=true — delta sem significado estatístico.
Agora: alvos diferentes → comparable=false com reason; runs legados pré-33.20
(agent_id/pipeline_id NULL) são EXPLICITAMENTE não-comparáveis (convenção
"métricas sem falsa confiança"), com hint de re-rodar a avaliação.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.dashboard as dash


def _client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


def _run(rid, **over):
    base = {
        "id": rid, "release_id": "r1", "run_type": "baseline",
        "gold_version": "latest", "gold_hash": "h1", "status": "completed",
        "agent_id": "ag1", "pipeline_id": None,
        "accuracy": 0.9, "total_cases": 10, "passed": 9, "failed": 1,
        "dimension_breakdown": "{}", "details": "[]",
    }
    base.update(over)
    return base


def _find_by_id(runs: dict):
    async def _fn(rid):
        return runs.get(rid)
    return _fn


def _compare(monkeypatch, run_a, run_b):
    monkeypatch.setattr(
        dash.eval_runs_repo, "find_by_id", _find_by_id({"A": run_a, "B": run_b})
    )
    return _client().get("/api/v1/eval-runs/compare?a=A&b=B")


# ── guarda de alvo ─────────────────────────────────────────────────

def test_same_target_comparable(monkeypatch):
    r = _compare(monkeypatch, _run("A"), _run("B"))
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["comparable"] is True, body["comparable_reason"]
    assert body["deltas"], "deltas devem ser computados quando comparable"
    # alvo exposto no sumário (UI vai rotular os seletores A/B com isso)
    assert body["run_a"]["agent_id"] == "ag1"
    assert body["run_b"]["pipeline_id"] is None


def test_different_agents_not_comparable(monkeypatch):
    r = _compare(monkeypatch, _run("A"), _run("B", agent_id="ag2"))
    body = r.json()
    assert body["comparable"] is False
    assert "alvos diferentes" in body["comparable_reason"]
    assert body["deltas"] == {} and body["divergent_cases"] == []


def test_agent_vs_pipeline_not_comparable(monkeypatch):
    r = _compare(
        monkeypatch, _run("A"), _run("B", agent_id=None, pipeline_id="p1")
    )
    body = r.json()
    assert body["comparable"] is False
    assert "alvos diferentes" in body["comparable_reason"]
    assert "pipeline p1" in body["comparable_reason"]


def test_legacy_run_without_target_not_comparable(monkeypatch):
    """Run pré-33.20 (alvo NULL) → recusa explícita, não comparação muda."""
    r = _compare(monkeypatch, _run("A", agent_id=None), _run("B"))
    body = r.json()
    assert body["comparable"] is False
    assert "legado" in body["comparable_reason"]
    assert "Re-rode" in body["comparable_reason"]


def test_target_guard_wins_over_status(monkeypatch):
    """Ordem da cadeia: alvo é a checagem mais estrutural — vem antes."""
    r = _compare(
        monkeypatch, _run("A", status="running"), _run("B", agent_id="ag2")
    )
    assert "alvos diferentes" in r.json()["comparable_reason"]


def test_same_target_still_validates_gold(monkeypatch):
    """Guarda de alvo não engole as checagens existentes (status/gold)."""
    r = _compare(monkeypatch, _run("A"), _run("B", gold_hash="h2"))
    body = r.json()
    assert body["comparable"] is False
    assert "MUDOU" in body["comparable_reason"]


# ── filtros por alvo no GET /eval-runs ─────────────────────────────

def test_list_eval_runs_filters_by_target(monkeypatch):
    captured = {}

    async def _find_all(limit=20, **kw):
        captured.update(kw, limit=limit)
        return []

    monkeypatch.setattr(dash.eval_runs_repo, "find_all", _find_all)
    r = _client().get("/api/v1/eval-runs?agent_id=ag1&release_id=r1")
    assert r.status_code == 200, r.text
    assert captured == {"agent_id": "ag1", "release_id": "r1", "limit": 20}


def test_list_eval_runs_filter_pipeline(monkeypatch):
    captured = {}

    async def _find_all(limit=20, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(dash.eval_runs_repo, "find_all", _find_all)
    _client().get("/api/v1/eval-runs?pipeline_id=p1")
    assert captured == {"pipeline_id": "p1"}
