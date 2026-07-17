"""Harness assíncrono + custo no ledger + teto por run (43.0.0, PR2 do arco
Otimização de Prompt/Skill).

Cobre as 4 frentes do PR:
1. Fila durável (app/harness/jobs.py): dispatch com cap, claim atômico,
   timeout → 'timeout', boot-resume (órfão → 'interrupted' + kill-switch).
2. Custo no ledger: cada caso grava em invocation_costs com source='harness'
   (off-path) e o run persiste cost_usd/avg_cost_usd.
3. Teto mid-run (harness_budget_usd_per_run): aborto gracioso — status
   'budget_exceeded', métricas PARCIAIS, gate 'skipped', SEM drift events.
4. Interações sintéticas: carimbo origin='harness' + retenção própria
   (purge_synthetic_once) independente da retenção LGPD.

Rotas: 202 + eval_id com o toggle ON; GET /eval-runs/{id} de polling declarado
DEPOIS de /eval-runs/compare (ordem de rotas do FastAPI).

Mocks nos módulos (repos/engine/pool) — sem DB/LLM reais, convenção da suíte.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.retention as retention
import app.harness.evaluator as evaluator
import app.harness.jobs as jobs
import app.routes.dashboard as dash


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


_GOLD = {
    "id": "gc1", "input_text": "minha internet caiu",
    "expected_state": "Recommend", "expected_output": "", "weight": 1.0,
    "category": "tecnico", "case_type": "normal", "channel": "api",
    "red_flags": None,
}

_RESULT = {
    "output": "Reinicie o roteador e verifique os LEDs.",
    "final_state": "LogAndClose",
    "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
    "interaction_id": "int-1",
    "trace": {},
}


def _wire_agent_mocks(monkeypatch, *, n_cases=1, settings=None):
    """Harness modo AGENTE com n_cases cópias do gold — engine/repos mockados."""
    created, updated = {}, {}

    async def _create(d):
        created.update(d)

    async def _update(_id, d):
        updated.update(d)

    cases = [{**_GOLD, "id": f"gc{i}"} for i in range(n_cases)]
    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _create)
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _async([]))
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async(cases))
    monkeypatch.setattr(evaluator.agents_repo, "find_by_id", _async({"id": "a1"}))
    exec_int = AsyncMock(return_value=dict(_RESULT))
    monkeypatch.setattr(evaluator, "execute_interaction", exec_int)
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings",
                        lambda: settings or _settings_stub())
    drift_calls = []

    async def _drift(**kw):
        drift_calls.append(kw)

    monkeypatch.setattr(evaluator, "_write_drift_events", _drift)
    return created, updated, exec_int, drift_calls


def _wire_cost(monkeypatch, per_case_cost=1.0):
    """Intercepta o encanamento de custo/carimbo: ledger e tag viram
    recorders (o evaluator os AWAITA direto — nada de task detached)."""
    ledger_calls, tag_calls = [], []

    async def fake_ledger(**kw):
        ledger_calls.append(kw)

    async def fake_tag(ids):
        tag_calls.append(ids)

    monkeypatch.setattr("app.core.cost_ledger.record_invocation_cost", fake_ledger)
    monkeypatch.setattr(evaluator, "_tag_synthetic_interactions", fake_tag)
    monkeypatch.setattr("app.core.api_key_budget.cost_and_tokens_from_result",
                        lambda r: (per_case_cost, 100))
    return ledger_calls, tag_calls


# ═══ Custo no ledger + persistência no run ═══════════════════════════════

class TestCustoNoLedger:
    @pytest.mark.asyncio
    async def test_cada_caso_grava_no_ledger_com_source_harness(self, monkeypatch):
        _, updated, _, _ = _wire_agent_mocks(monkeypatch, n_cases=2)
        ledger, _ = _wire_cost(monkeypatch, per_case_cost=0.5)

        out = await evaluator.run_evaluation("r1", agent_id="a1",
                                             owner_user_id="u1")

        assert len(ledger) == 2
        assert all(k["source"] == "harness" for k in ledger)
        assert all(k["user_id"] == "u1" for k in ledger)
        assert ledger[0]["cost_usd"] == 0.5 and ledger[0]["tokens_used"] == 100
        # run persiste o total e a média por caso (coluna avg_cost_usd do
        # schema base, antes nunca populada)
        assert updated["cost_usd"] == 1.0
        assert updated["avg_cost_usd"] == 0.5
        assert updated["status"] == "completed"
        assert out["cost_usd"] == 1.0

    @pytest.mark.asyncio
    async def test_interacoes_sao_carimbadas_como_sinteticas(self, monkeypatch):
        _wire_agent_mocks(monkeypatch, n_cases=2)
        _, tags = _wire_cost(monkeypatch)

        await evaluator.run_evaluation("r1", agent_id="a1")

        assert tags == [["int-1"], ["int-1"]]

    def test_collect_interaction_ids_pipeline_dedup(self):
        result = {
            "interaction_id": "master",
            "pipeline_steps": [
                {"interaction_id": "s1"}, {"interaction_id": "master"},
                {"status": "skipped_conditional"}, {"interaction_id": "s2"},
            ],
        }
        assert evaluator._collect_interaction_ids(result) == ["master", "s1", "s2"]

    @pytest.mark.asyncio
    async def test_owner_persistido_no_create(self, monkeypatch):
        created, _, _, _ = _wire_agent_mocks(monkeypatch)
        _wire_cost(monkeypatch)
        await evaluator.run_evaluation("r1", agent_id="a1", owner_user_id="u9")
        assert created["owner_user_id"] == "u9"


# ═══ Teto de custo mid-run ═══════════════════════════════════════════════

class TestTetoMidRun:
    @pytest.mark.asyncio
    async def test_estouro_aborta_gracioso_sem_drift(self, monkeypatch):
        _, updated, exec_int, drift = _wire_agent_mocks(
            monkeypatch, n_cases=3,
            settings=_settings_stub(harness_budget_usd_per_run=0.5),
        )
        _wire_cost(monkeypatch, per_case_cost=1.0)

        out = await evaluator.run_evaluation("r1", agent_id="a1")

        # caso 1 executa (acumulado 0 < 0.5); caso 2 já encontra 1.0 >= 0.5
        assert exec_int.await_count == 1
        assert out["status"] == "budget_exceeded"
        assert updated["status"] == "budget_exceeded"
        assert updated["gate_result"] == "skipped"
        assert updated["total_cases"] == 1  # avaliados, não planejados
        assert "teto de custo" in (updated["gate_reason"] or "")
        assert "1/3" in updated["gate_reason"]
        # métricas parciais NÃO produzem drift events (falso drift)
        assert drift == []

    @pytest.mark.asyncio
    async def test_sem_teto_default_avalia_tudo(self, monkeypatch):
        # stub SEM o atributo → getattr defensivo = teto desligado
        _, updated, exec_int, drift = _wire_agent_mocks(monkeypatch, n_cases=3)
        _wire_cost(monkeypatch, per_case_cost=99.0)

        out = await evaluator.run_evaluation("r1", agent_id="a1")

        assert exec_int.await_count == 3
        assert out["status"] == "completed"
        assert updated["status"] == "completed"
        assert len(drift) == 1


# ═══ Fila durável (app/harness/jobs.py) ══════════════════════════════════

class _FakeCon:
    def __init__(self, row=None, rows=None, execute_result="UPDATE 1"):
        self.row = row
        self.rows = rows or []
        self.execute_result = execute_result
        self.executed = []
        self.fetch_sqls = []

    async def fetchrow(self, sql, *a):
        return self.row

    async def fetch(self, sql, *a):
        self.fetch_sqls.append(sql)
        return self.rows

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return self.execute_result


class _FakePool:
    def __init__(self, con):
        self._con = con

    def acquire(self):
        con = self._con

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


_JOB_ROW = {
    "id": "ev1", "release_id": "r1", "gold_version": "latest",
    "run_type": "baseline", "agent_id": "a1", "pipeline_id": None,
    "owner_user_id": "u1",
}


class TestEvalJobs:
    @pytest.mark.asyncio
    async def test_dispatch_respeita_cap_e_dedup(self, monkeypatch):
        jobs._reset_for_tests()
        monkeypatch.setattr(jobs, "_max_concurrent", lambda: 1)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(eid):
            started.set()
            await release.wait()

        monkeypatch.setattr(jobs, "_run_job", slow)
        assert jobs.dispatch("e1") is True
        assert jobs.dispatch("e1") is False  # já em voo
        assert jobs.dispatch("e2") is False  # sem vaga (cap=1)
        await started.wait()
        release.set()
        await asyncio.sleep(0)
        jobs._reset_for_tests()

    @pytest.mark.asyncio
    async def test_claim_atomico_noop_sem_linha(self, monkeypatch):
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(_FakeCon(row=None)))
        run_eval = AsyncMock()
        monkeypatch.setattr("app.harness.evaluator.run_evaluation", run_eval)
        await jobs._run_job("ghost")
        run_eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_job_claima_e_repassa_contexto(self, monkeypatch):
        monkeypatch.setattr(jobs, "_pool",
                            lambda: _FakePool(_FakeCon(row=dict(_JOB_ROW))))
        monkeypatch.setattr(jobs, "_timeout_minutes", lambda: 1.0)
        run_eval = AsyncMock(return_value={"status": "completed"})
        monkeypatch.setattr("app.harness.evaluator.run_evaluation", run_eval)
        await jobs._run_job("ev1")
        kw = run_eval.await_args.kwargs
        assert run_eval.await_args.args == ("r1",)
        assert kw["eval_id"] == "ev1" and kw["owner_user_id"] == "u1"
        assert kw["agent_id"] == "a1" and kw["pipeline_id"] is None

    @pytest.mark.asyncio
    async def test_timeout_marca_run_como_timeout(self, monkeypatch):
        con = _FakeCon(row=dict(_JOB_ROW))
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(con))
        # 0.0005 min = 30ms de deadline
        monkeypatch.setattr(jobs, "_timeout_minutes", lambda: 0.0005)

        async def hang(*a, **k):
            await asyncio.sleep(5)

        monkeypatch.setattr("app.harness.evaluator.run_evaluation", hang)
        await jobs._run_job("ev1")
        marks = [args for (sql, args) in con.executed if "SET status=$2" in sql]
        assert marks and marks[0][1] == "timeout"

    @pytest.mark.asyncio
    async def test_resume_on_boot_orfao_vira_interrupted(self, monkeypatch):
        con = _FakeCon(rows=[], execute_result="UPDATE 2")
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(con))
        monkeypatch.setattr(jobs, "_enabled", lambda: False)  # kill-switch
        out = await jobs.resume_on_boot()
        assert out == {"interrupted": 2, "dispatched": 0}
        # kill-switch: fila NÃO consultada (nada despachado com toggle OFF)
        assert con.fetch_sqls == []

    @pytest.mark.asyncio
    async def test_resume_on_boot_despacha_queued_quando_on(self, monkeypatch):
        con = _FakeCon(rows=[{"id": "q1"}], execute_result="UPDATE 0")
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(con))
        monkeypatch.setattr(jobs, "_enabled", lambda: True)
        dispatched = []
        monkeypatch.setattr(jobs, "dispatch",
                            lambda eid: dispatched.append(eid) or True)
        out = await jobs.resume_on_boot()
        assert dispatched == ["q1"] and out["dispatched"] == 1

    @pytest.mark.asyncio
    async def test_sweep_queued_killswitch_congela_despacho(self, monkeypatch):
        """Kill-switch OFF: a HIGIENE (zumbis) roda sempre, mas a fila
        'queued' NÃO é consultada nem despachada."""
        jobs._reset_for_tests()
        con = _FakeCon(rows=[])
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(con))
        monkeypatch.setattr(jobs, "_enabled", lambda: False)
        out = await jobs.sweep_queued()
        assert out == {"dispatched": 0, "interrupted": 0}
        assert len(con.fetch_sqls) == 1  # só a query de zumbis ('running')
        assert "status='running'" in con.fetch_sqls[0]

    @pytest.mark.asyncio
    async def test_sweep_cura_zumbi_running_de_job(self, monkeypatch):
        """Review [10]/[26]: 'running' com is_job e SEM task viva → curado
        para 'interrupted' pelo sweep (antes só o restart curava)."""
        jobs._reset_for_tests()
        con = _FakeCon(rows=[{"id": "zumbi-1"}])
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(con))
        monkeypatch.setattr(jobs, "_enabled", lambda: False)
        out = await jobs.sweep_queued()
        assert out["interrupted"] == 1
        marks = [a for (sql, a) in con.executed if "interrupted" in sql]
        assert marks and marks[0][0] == ["zumbi-1"]
        # a query de zumbi filtra is_job — run SÍNCRONO em voo nunca é tocado
        assert "is_job = TRUE" in con.fetch_sqls[0]

    @pytest.mark.asyncio
    async def test_sweep_sem_vaga_nao_consulta_queued(self, monkeypatch):
        """Review [22]: com o cap ocupado, o SELECT de 'queued' nem roda."""
        jobs._reset_for_tests()
        con = _FakeCon(rows=[])
        monkeypatch.setattr(jobs, "_pool", lambda: _FakePool(con))
        monkeypatch.setattr(jobs, "_enabled", lambda: True)
        monkeypatch.setattr(jobs, "_max_concurrent", lambda: 1)
        jobs._active_eval_ids.add("em-voo")   # cap cheio
        try:
            out = await jobs.sweep_queued()
        finally:
            jobs._reset_for_tests()
        assert out["dispatched"] == 0
        assert len(con.fetch_sqls) == 1  # zumbis sim; queued não


# ═══ Fixes da revisão adversarial pré-push (43.0.0) ══════════════════════

def _wire_pipeline_min(monkeypatch, exec_pipe):
    """Wiring mínimo do modo PIPELINE (1 caso gold)."""
    created, updated = {}, {}

    async def _create(d):
        created.update(d)

    async def _update(_id, d):
        updated.update(d)

    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _create)
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _async([]))
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async([dict(_GOLD)]))
    monkeypatch.setattr(evaluator.pipelines_repo, "find_by_id",
                        _async({"id": "p1", "name": "P", "status": "rascunho"}))
    monkeypatch.setattr("app.catalog.pipeline_defs._build_subgraph",
                        _async({"root_agent_id": "root", "nodes": [{"id": "root"}]}))
    monkeypatch.setattr(
        "app.catalog.pipeline_defs.evaluate_pipeline_test_phrases",
        _async({"evaluated": 0, "passed": 0, "failing": [], "phrases_hash": None}))
    monkeypatch.setattr(evaluator, "execute_pipeline", exec_pipe)
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())
    monkeypatch.setattr(evaluator, "_write_drift_events", _async(None))
    return created, updated


class TestFixesDaRevisao:
    @pytest.mark.asyncio
    async def test_custo_do_juiz_soma_todos_os_steps_do_pipeline(self, monkeypatch):
        """Review [1]: cada step rigorous tem verification própria — somar só
        a do envelope reancorado subcontava (1/N do gasto de juiz)."""
        pipe_result = {
            "output": "ok", "final_state": "SkippedConditional",
            "transitions": [], "interaction_id": "m1",
            "pipeline_steps": [
                {"agent_name": "A", "status": "completed", "interaction_id": "s1",
                 "final_state": "LogAndClose",
                 "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
                 "verification": {"judge_cost_usd": 0.2, "dimensions": {}}},
                {"agent_name": "B", "status": "completed", "interaction_id": "s2",
                 "final_state": "LogAndClose",
                 "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
                 "verification": {"judge_cost_usd": 0.3, "dimensions": {}}},
            ],
        }
        _, updated = _wire_pipeline_min(
            monkeypatch, AsyncMock(return_value=pipe_result))
        _wire_cost(monkeypatch, per_case_cost=0.0)

        await evaluator.run_evaluation("r1", pipeline_id="p1")

        assert updated["cost_usd"] == 0.5  # 0.2 + 0.3 — todos os steps

    @pytest.mark.asyncio
    async def test_excecao_apos_gasto_preserva_custo(self, monkeypatch):
        """Review [2]: steps concluídos ANTES de um raise entram no teto e no
        ledger (final_state='CaseError') — gasto real nunca vira US$ 0."""
        async def _boom(**kw):
            cb = kw.get("progress_callback")
            if cb:
                await cb({"type": "agent_done", "cost_usd": 2.0,
                          "tokens_used": 50, "interaction_id": "s1"})
            raise RuntimeError("db down pós-gasto")

        _, updated = _wire_pipeline_min(monkeypatch, _boom)
        ledger, tags = _wire_cost(monkeypatch, per_case_cost=0.0)

        out = await evaluator.run_evaluation("r1", pipeline_id="p1")

        assert out["failed"] == 1
        assert updated["cost_usd"] == 2.0
        assert any(k.get("final_state") == "CaseError" and k["cost_usd"] == 2.0
                   for k in ledger)
        assert ["s1"] in tags  # interação órfã do raise também é carimbada

    @pytest.mark.asyncio
    async def test_invalid_target_persiste_terminal_com_eval_id(self, monkeypatch):
        """Review [6]: worker claimou a linha ('running') e o alvo é inválido
        → o terminal TEM que ser persistido (senão órfã até o boot)."""
        upd = {}

        async def _update(_id, d):
            upd.update({"_id": _id, **d})

        monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
        monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())

        out = await evaluator.run_evaluation("r1", eval_id="ev-claimed")  # sem alvo

        assert out["status"] == "invalid_target"
        assert upd["_id"] == "ev-claimed"
        assert upd["status"] == "invalid_target" and upd["gate_result"] == "skipped"

    def test_template_polling_tolerante_e_rotulo_honesto(self):
        """Reviews [3]/[4]/[12] — invariantes do template."""
        from pathlib import Path
        src = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")
        assert "misses" in src and "misses>=5" in src.replace(" ", "")
        assert "runLabel(r)" in src          # timeout/failed não viram 'skipped'
        assert "budget_exceeded" in src      # toast do teto no caminho síncrono


# ═══ Rotas: 202 + polling ════════════════════════════════════════════════

def _client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


_BODY = {"release_id": "r1", "agent_id": "a1",
         "gold_version": "latest", "run_type": "baseline"}


class TestRotasAsync:
    def test_execute_202_quando_toggle_on(self, monkeypatch):
        monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
        monkeypatch.setattr(dash.agents_repo, "find_by_id", _async({"id": "a1"}))
        created = {}

        async def _create(d):
            created.update(d)

        monkeypatch.setattr(dash.eval_runs_repo, "create", _create)
        dispatched = []
        monkeypatch.setattr(jobs, "dispatch",
                            lambda eid: dispatched.append(eid) or True)
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(harness_async_enabled=True))

        r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["eval_id"]
        assert body["poll_url"].endswith(body["eval_id"])
        assert created["status"] == "queued"
        assert created["is_job"] is True  # habilita o zombie-sweep do reaper
        assert created["agent_id"] == "a1" and created["pipeline_id"] is None
        assert dispatched == [body["eval_id"]]

    def test_get_eval_run_polling(self, monkeypatch):
        monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _async({
            "id": "ev1", "status": "running",
            "dimension_breakdown": "{}", "details": "[]",
        }))
        r = _client().get("/api/v1/eval-runs/ev1")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "running"
        assert r.json()["details"] == []  # TEXT JSON → objeto

    def test_get_eval_run_404(self, monkeypatch):
        monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _async(None))
        assert _client().get("/api/v1/eval-runs/ghost").status_code == 404

    def test_compare_nao_e_capturado_pela_rota_parametrizada(self):
        """/eval-runs/{run_id} é declarada DEPOIS de /eval-runs/compare —
        sem params, compare responde 422 (a/b obrigatórios). Se {run_id}
        capturasse 'compare', viria 404 'não encontrada'."""
        r = _client().get("/api/v1/eval-runs/compare")
        assert r.status_code == 422, r.text


# ═══ Retenção das interações sintéticas ══════════════════════════════════

class TestRetencaoSintetica:
    @pytest.mark.asyncio
    async def test_desligado_por_default_noop(self, monkeypatch):
        monkeypatch.setattr(retention, "_synthetic_retention_days", lambda: 0)
        out = await retention.purge_synthetic_once()
        assert out == {"deleted": 0, "scrubbed_verifications": 0}

    @pytest.mark.asyncio
    async def test_purga_filtra_por_origin_harness(self, monkeypatch):
        monkeypatch.setattr(retention, "_synthetic_retention_days", lambda: 7)
        con = _FakeCon(rows=[{"id": "i1"}, {"id": "i2"}])
        monkeypatch.setattr(retention, "_pool", lambda: _FakePool(con))
        purged = []

        async def fake_purge_ids(c, ids):
            purged.append(ids)
            return {"deleted": len(ids), "scrubbed_verifications": 0}

        monkeypatch.setattr(retention, "_purge_ids", fake_purge_ids)
        out = await retention.purge_synthetic_once()
        assert purged == [["i1", "i2"]] and out["deleted"] == 2
        assert "origin = 'harness'" in con.fetch_sqls[0]

    @pytest.mark.asyncio
    async def test_maybe_purge_roda_sintetica_mesmo_sem_lgpd(self, monkeypatch):
        retention._reset_for_tests()
        monkeypatch.setattr(retention, "_retention_days", lambda: 0)
        monkeypatch.setattr(retention, "_synthetic_retention_days", lambda: 7)

        async def fake_age():
            return {"deleted": 0, "scrubbed_verifications": 0}

        async def fake_syn():
            return {"deleted": 3, "scrubbed_verifications": 1}

        monkeypatch.setattr(retention, "purge_interactions_once", fake_age)
        monkeypatch.setattr(retention, "purge_synthetic_once", fake_syn)
        out = await retention.maybe_purge()
        assert out["synthetic_deleted"] == 3
        assert out["synthetic_scrubbed"] == 1
        retention._reset_for_tests()
