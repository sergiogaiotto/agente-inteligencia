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


# ── DELETE /eval-runs/{id} (housekeeping — 27.1.0) ────────────────

def test_delete_eval_run_ok(monkeypatch):
    # 43.0.0: a rota consulta o run ANTES (guard de 'running') — mocka o find.
    monkeypatch.setattr(dash.eval_runs_repo, "find_by_id",
                        _async({"id": "some-id", "status": "completed"}))
    monkeypatch.setattr(dash.eval_runs_repo, "delete", _async(True))
    r = _client().delete("/api/v1/eval-runs/some-id")
    assert r.status_code == 200, r.text
    assert "removida" in r.json()["message"].lower()


def test_delete_eval_run_404_when_missing(monkeypatch):
    monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _async(None))
    monkeypatch.setattr(dash.eval_runs_repo, "delete", _async(False))
    r = _client().delete("/api/v1/eval-runs/ghost")
    assert r.status_code == 404, r.text


def test_delete_eval_run_409_quando_running(monkeypatch):
    """43.0.0 (review [11]): run em execução não pode ser removido — o
    worker/request em voo continuaria pagando LLM invisível."""
    monkeypatch.setattr(dash.eval_runs_repo, "find_by_id",
                        _async({"id": "busy", "status": "running"}))
    r = _client().delete("/api/v1/eval-runs/busy")
    assert r.status_code == 409, r.text


# ── run_evaluation com agente deletado → invalid_agent (27.1.0) ───

@pytest.mark.asyncio
async def test_run_evaluation_invalid_agent(monkeypatch):
    """Agente deletado → run encerra como 'invalid_agent'/skipped SEM avaliar
    caso algum (não polui accuracy). Antes: cada caso caía no except do engine
    e virava 'failed', gerando accuracy 0.0 espúria."""
    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _async(None))
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _async(None))
    # HÁ casos no dataset (passa o guard no_cases)...
    monkeypatch.setattr(
        evaluator.gold_cases_repo, "find_all",
        _async([{"input_text": "x", "weight": 1.0}]),
    )
    # ...mas o agente NÃO existe:
    monkeypatch.setattr(evaluator.agents_repo, "find_by_id", _async(None))

    # execute_interaction NÃO pode ser chamado (nenhum caso avaliado).
    def _boom(*a, **k):
        raise AssertionError("execute_interaction não deveria rodar com agente inválido")
    monkeypatch.setattr(evaluator, "execute_interaction", _boom)

    out = await evaluator.run_evaluation("r1", "ghost-agent", "latest", "baseline")
    assert out["status"] == "invalid_agent", out
    assert "não existe" in out["message"]


# ── Paridade de shape entre as vias de leitura de eval runs (49.0.1) ──
# Achado do E2E de profundidade (2026-07-17) + revisão adversarial: o resumo
# do evaluator fala `total`/`eval_id`; a linha crua fala `total_cases`/`id`.
# Consumidor que aprende o shape numa via (POST síncrono, GET /{id}, GET
# lista, compare) não pode perder campo ao migrar pra outra — TODO payload
# carrega ambos os nomes de cada par (_ensure_eval_run_aliases).

_ROW = {"id": "e1", "status": "completed", "total_cases": 4, "passed": 4,
        "failed": 0, "details": "[]", "dimension_breakdown": "{}"}


def _assert_pairs(body: dict, total, ident):
    assert body["total"] == total
    assert body["total_cases"] == total
    assert body["id"] == ident
    assert body["eval_id"] == ident


def test_execute_sync_response_carries_both_names(monkeypatch):
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
    monkeypatch.setattr(dash.agents_repo, "find_by_id", _async({"id": "a1"}))
    monkeypatch.setattr(
        evaluator, "run_evaluation",
        _async({"eval_id": "e9", "status": "completed",
                "total": 8, "passed": 7, "failed": 1}),
    )
    r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
    assert r.status_code == 200, r.text
    _assert_pairs(r.json(), total=8, ident="e9")


def test_get_eval_run_carries_both_names(monkeypatch):
    monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _async(dict(_ROW)))
    r = _client().get("/api/v1/eval-runs/e1")
    assert r.status_code == 200, r.text
    _assert_pairs(r.json(), total=4, ident="e1")


def test_get_eval_run_in_flight_keeps_total_key(monkeypatch):
    """Linha em voo (poll do async): total_cases NULL → total=None mas a
    CHAVE existe — poller distingue 'ainda sem número' de 'shape mudou'."""
    row = {**_ROW, "status": "running", "total_cases": None,
           "passed": None, "failed": None}
    monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _async(row))
    body = _client().get("/api/v1/eval-runs/e1").json()
    assert "total" in body and body["total"] is None
    assert body["eval_id"] == "e1"


def test_list_eval_runs_rows_carry_both_names(monkeypatch):
    monkeypatch.setattr(dash.eval_runs_repo, "find_all", _async([dict(_ROW)]))
    r = _client().get("/api/v1/eval-runs")
    assert r.status_code == 200, r.text
    _assert_pairs(r.json()["runs"][0], total=4, ident="e1")


def test_summary_of_run_carries_both_names():
    """/eval-runs/compare monta run_a/run_b via _summary_of_run — a quarta
    via de leitura precisa da mesma paridade."""
    body = dash._summary_of_run(dict(_ROW))
    _assert_pairs(body, total=4, ident="e1")


def test_alias_is_coupled_to_real_evaluator_contract():
    """Anti-drift: o alias na rota é chaveado nos literais `total`/`eval_id`
    do resumo REAL de run_evaluation — se alguém renomear lá, este teste
    quebra junto (senão o alias vira no-op silencioso e a paridade some)."""
    import inspect
    src = inspect.getsource(evaluator.run_evaluation)
    assert '"eval_id": eval_id' in src, "resumo do evaluator renomeou eval_id"
    assert '"total": total' in src, "resumo do evaluator renomeou total"
