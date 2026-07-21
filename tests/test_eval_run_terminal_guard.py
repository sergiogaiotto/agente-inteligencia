"""Guarda de terminal do run do harness (66.4.1) — achado do E2E 2026-07-21.

Gap reproduzido na VPS: um eval-run SÍNCRONO morto em voo (cliente desconecta
→ Starlette cancela o handler → CancelledError, que é BaseException e passa
reto pelo `except Exception` da rota; ou exceção que escapa do loop) deixava a
linha de eval_runs em 'running' ETERNO — indeletável (DELETE recusa 'running')
até o resume_on_boot do próximo restart. O mesmo valia para a sonda do
otimizador (asyncio.wait_for cancela o inner no timeout → 504 com linha órfã).

Contrato da fachada `run_evaluation` (wrapper de `_run_evaluation_impl`):
 1. CancelledError no meio do run → linha criada pela chamada vira
    'interrupted' (via jobs._mark, guardado por status IN queued/running) e a
    exceção é RE-LEVANTADA (a cura nunca engole o cancel);
 2. Exception → linha vira 'failed' e a exceção propaga (rota segue com 500);
 3. linha-JOB (eval_id pré-claimado pelo worker) NÃO é curada aqui — o
    terminal dela pertence a jobs._run/_mark e ao reaper (timeout/failed têm
    semântica própria; curar na fachada roubaria o status deles).
"""

import asyncio

import pytest

from app.harness import evaluator
from app.harness import jobs as harness_jobs


class _FakeEvalRuns:
    def __init__(self):
        self.created = []
        self.updated = []

    async def create(self, data):
        self.created.append(data)
        return data

    async def update(self, run_id, data):
        self.updated.append((run_id, data))
        return True


class _RaisingGold:
    def __init__(self, exc):
        self._exc = exc

    async def find_all(self, **kw):
        raise self._exc


@pytest.fixture
def marked(monkeypatch):
    """Grava as curas sem tocar o Postgres (jobs._mark é resolvido em runtime
    pelo import tardio da fachada — o patch no módulo jobs é suficiente)."""
    calls = []

    async def _fake_mark(eval_id, status, error=None, gate_reason=None):
        calls.append({"eval_id": eval_id, "status": status, "error": error,
                      "gate_reason": gate_reason})

    monkeypatch.setattr(harness_jobs, "_mark", _fake_mark)
    return calls


@pytest.mark.asyncio
async def test_cancel_em_voo_cura_como_interrupted(monkeypatch, marked):
    fake_runs = _FakeEvalRuns()
    monkeypatch.setattr(evaluator, "eval_runs_repo", fake_runs)
    monkeypatch.setattr(evaluator, "gold_cases_repo",
                        _RaisingGold(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await evaluator.run_evaluation("rel-1", agent_id="ag-1")
    # A linha foi criada 'running' pelo caminho síncrono…
    assert fake_runs.created and fake_runs.created[0]["status"] == "running"
    # …e a fachada a curou como 'interrupted' SEM engolir o cancel.
    assert len(marked) == 1
    assert marked[0]["status"] == "interrupted"
    assert marked[0]["eval_id"] == fake_runs.created[0]["id"]
    assert marked[0]["error"] == "run_cancelled"


@pytest.mark.asyncio
async def test_excecao_em_voo_cura_como_failed(monkeypatch, marked):
    fake_runs = _FakeEvalRuns()
    monkeypatch.setattr(evaluator, "eval_runs_repo", fake_runs)
    monkeypatch.setattr(evaluator, "gold_cases_repo",
                        _RaisingGold(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        await evaluator.run_evaluation("rel-1", agent_id="ag-1")
    assert len(marked) == 1
    assert marked[0]["status"] == "failed"
    assert marked[0]["error"] == "eval_execution_failed"


@pytest.mark.asyncio
async def test_linha_job_preclaimada_nao_e_curada_pela_fachada(monkeypatch, marked):
    fake_runs = _FakeEvalRuns()
    monkeypatch.setattr(evaluator, "eval_runs_repo", fake_runs)
    monkeypatch.setattr(evaluator, "gold_cases_repo",
                        _RaisingGold(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await evaluator.run_evaluation("rel-1", agent_id="ag-1",
                                       eval_id="job-preclaimado")
    # eval_id veio de fora (worker claimou) → o impl NÃO criou linha nova e a
    # fachada NÃO cura — o terminal é de jobs._run/_mark e do reaper.
    assert fake_runs.created == []
    assert marked == []


@pytest.mark.asyncio
async def test_cura_nunca_mascara_a_excecao_original(monkeypatch):
    """_mark quebrado (DB fora) não pode transformar o cancel em outra coisa."""
    fake_runs = _FakeEvalRuns()
    monkeypatch.setattr(evaluator, "eval_runs_repo", fake_runs)
    monkeypatch.setattr(evaluator, "gold_cases_repo",
                        _RaisingGold(asyncio.CancelledError()))

    async def _broken_mark(*a, **kw):
        raise OSError("db fora do ar")

    monkeypatch.setattr(harness_jobs, "_mark", _broken_mark)
    with pytest.raises(asyncio.CancelledError):
        await evaluator.run_evaluation("rel-1", agent_id="ag-1")
