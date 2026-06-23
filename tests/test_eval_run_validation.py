"""POST /api/v1/eval-runs/execute valida release_id + agent_id ANTES de rodar.

Achado no teste E2E (2026-06-23): o endpoint executava com ids INEXISTENTES e
gravava um eval_run "lixo" (completed, accuracy 0.0) que não pode ser deletado
(não há DELETE de eval_runs). Agora valida existência → 404, sem poluir.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.dashboard as dash
import app.harness.evaluator as evaluator


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


_BODY = {"release_id": "r1", "agent_id": "a1", "gold_version": "latest", "run_type": "baseline"}


def test_404_when_release_missing(monkeypatch):
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async(None))
    r = _client().post("/api/v1/eval-runs/execute", json={**_BODY, "release_id": "ghost"})
    assert r.status_code == 404, r.text
    assert "Release" in r.json()["detail"]


def test_404_when_agent_missing(monkeypatch):
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
    monkeypatch.setattr(dash.agents_repo, "find_by_id", _async(None))
    r = _client().post("/api/v1/eval-runs/execute", json={**_BODY, "agent_id": "ghost"})
    assert r.status_code == 404, r.text
    assert "Agente" in r.json()["detail"]


def test_runs_when_both_exist(monkeypatch):
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
    monkeypatch.setattr(dash.agents_repo, "find_by_id", _async({"id": "a1"}))
    # mocka o evaluator — não tocamos DB/LLM; só validamos que passa pela checagem
    monkeypatch.setattr(evaluator, "run_evaluation", _async({"status": "completed", "accuracy": 1.0}))
    r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
