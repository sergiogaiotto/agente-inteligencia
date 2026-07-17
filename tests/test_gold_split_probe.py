"""Split train/holdout + captura por caso + sonda go/no-go (48.0.0, PR4a).

Cobre:
1. Evaluator: filtro gold_split ('train' inclui NULL; 'holdout' só
   reservados) e case_ids (minibatch); gold_split persistido no run;
   captura experiment_case_results SÓ em run_type='experiment'.
2. Worker do job repassa gold_split claimado.
3. Rota /execute: ''→None; valor inválido 422; 202 persiste a fatia.
4. Auto-split: adversarial → holdout SEMPRE; normais estratificados por
   categoria (determinístico); 404 sem casos.
5. Propositor: resumo SÓ do treino (holdout invisível) + split_note; leak
   detector segue varrendo TODOS (eco de gabarito do holdout → rejeição).
6. Promoção: aviso quando o par foi medido só no treino.
7. Sonda: minibatch estratificado do champion; go/no-go honesto; 422 sem
   details no champion.
8. Templates: marcadores da UI.

Mocks nos módulos — sem DB/LLM reais, convenção da suíte.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.harness.evaluator as evaluator
import app.harness.jobs as jobs
import app.routes.dashboard as dash
import app.routes.optimizer as opt


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
        harness_max_dim_regression_pct=5.0, harness_max_regression_pct=5.0,
        harness_phrases_gate=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


_RESULT = {
    "output": "resposta ok", "final_state": "LogAndClose",
    "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
    "interaction_id": "int-1", "trace": {},
}


def _mk_case(cid, split=None, **over):
    c = {"id": cid, "input_text": f"pergunta {cid}",
         "expected_state": "Recommend", "expected_output": "", "weight": 1.0,
         "category": over.pop("category", "t"), "case_type": "normal",
         "channel": "api", "red_flags": None, "split": split}
    c.update(over)
    return c


def _wire_eval(monkeypatch, cases):
    created, updated = {}, {}

    async def _create(d):
        created.update(d)

    async def _update(_id, d):
        updated.update(d)

    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _create)
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _async([]))
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async(cases))
    monkeypatch.setattr(evaluator.agents_repo, "find_by_id", _async({"id": "a1"}))
    exec_int = AsyncMock(return_value=dict(_RESULT))
    monkeypatch.setattr(evaluator, "execute_interaction", exec_int)
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())
    monkeypatch.setattr(evaluator, "_write_drift_events", _async(None))
    monkeypatch.setattr(evaluator, "_tag_synthetic_interactions", _async(None))
    monkeypatch.setattr("app.core.cost_ledger.record_invocation_cost", _async(None))
    monkeypatch.setattr("app.core.api_key_budget.cost_and_tokens_from_result",
                        lambda r: (0.0, 0))
    captures = []

    async def _flush(eval_id, rows):
        # rows = [(case_id, passed, output, reasons)] — flush único (batched)
        for (case_id, passed, output, _reasons) in rows:
            captures.append({"case_id": case_id, "passed": passed,
                             "output": output})

    monkeypatch.setattr(evaluator, "_flush_experiment_captures", _flush)
    return created, updated, exec_int, captures


_CASES = [_mk_case("h1", split="holdout"), _mk_case("t1", split="train"),
          _mk_case("n1", split=None)]


class TestEvaluatorSplit:
    @pytest.mark.asyncio
    async def test_train_inclui_null_e_persiste_fatia(self, monkeypatch):
        created, _, exec_int, _ = _wire_eval(monkeypatch, list(_CASES))
        await evaluator.run_evaluation("r1", agent_id="a1", gold_split="train")
        assert exec_int.await_count == 2  # t1 + n1 (NULL conta como treino)
        assert created["gold_split"] == "train"

    @pytest.mark.asyncio
    async def test_holdout_so_reservados(self, monkeypatch):
        _, _, exec_int, _ = _wire_eval(monkeypatch, list(_CASES))
        await evaluator.run_evaluation("r1", agent_id="a1",
                                       gold_split="holdout")
        assert exec_int.await_count == 1

    @pytest.mark.asyncio
    async def test_case_ids_minibatch(self, monkeypatch):
        _, _, exec_int, _ = _wire_eval(monkeypatch, list(_CASES))
        await evaluator.run_evaluation("r1", agent_id="a1",
                                       case_ids=["t1"])
        assert exec_int.await_count == 1

    @pytest.mark.asyncio
    async def test_captura_so_em_experiment(self, monkeypatch):
        _, _, _, captures = _wire_eval(monkeypatch, [_mk_case("c1")])
        await evaluator.run_evaluation("r1", agent_id="a1")
        assert captures == []  # baseline não captura
        await evaluator.run_evaluation("r1", agent_id="a1",
                                       run_type="experiment")
        assert len(captures) == 1
        assert captures[0]["case_id"] == "c1"
        assert captures[0]["output"] == "resposta ok"

    @pytest.mark.asyncio
    async def test_worker_repassa_gold_split(self, monkeypatch):
        row = {"id": "ev1", "release_id": "r1", "gold_version": "latest",
               "run_type": "experiment", "agent_id": "a1", "pipeline_id": None,
               "owner_user_id": "u1", "config_overrides": None,
               "gold_split": "train"}

        class _Con:
            async def fetchrow(self, sql, *a):
                assert "gold_split" in sql
                return row

            async def execute(self, sql, *a):
                return "UPDATE 1"

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return _Con()

                    async def __aexit__(self, *exc):
                        return False
                return _Ctx()

        monkeypatch.setattr(jobs, "_pool", lambda: _Pool())
        monkeypatch.setattr(jobs, "_timeout_minutes", lambda: 1.0)
        run_eval = AsyncMock(return_value={"status": "completed"})
        monkeypatch.setattr("app.harness.evaluator.run_evaluation", run_eval)
        await jobs._run_job("ev1")
        assert run_eval.await_args.kwargs["gold_split"] == "train"


# ═══ Rota /execute + auto-split ══════════════════════════════════════════

def _dash_client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


class TestRotaSplit:
    def _wire(self, monkeypatch):
        monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
        monkeypatch.setattr(dash.agents_repo, "find_by_id", _async({"id": "a1"}))

    def test_string_vazia_vira_none(self, monkeypatch):
        self._wire(monkeypatch)
        seen = {}

        async def _run_eval(*a, **kw):
            seen.update(kw)
            return {"status": "completed", "accuracy": 1.0,
                    "gate_result": "approved"}

        monkeypatch.setattr("app.harness.evaluator.run_evaluation", _run_eval)
        r = _dash_client().post("/api/v1/eval-runs/execute", json={
            "release_id": "r1", "agent_id": "a1", "gold_split": ""})
        assert r.status_code == 200, r.text
        assert seen["gold_split"] is None

    def test_422_valor_invalido(self, monkeypatch):
        self._wire(monkeypatch)
        r = _dash_client().post("/api/v1/eval-runs/execute", json={
            "release_id": "r1", "agent_id": "a1", "gold_split": "metade"})
        assert r.status_code == 422 and "gold_split" in r.json()["detail"]

    def test_202_persiste_fatia(self, monkeypatch):
        self._wire(monkeypatch)
        created = {}

        async def _create(d):
            created.update(d)

        monkeypatch.setattr(dash.eval_runs_repo, "create", _create)
        monkeypatch.setattr(jobs, "dispatch", lambda eid: True)
        monkeypatch.setattr("app.core.config.get_settings",
                            lambda: SimpleNamespace(harness_async_enabled=True))
        r = _dash_client().post("/api/v1/eval-runs/execute", json={
            "release_id": "r1", "agent_id": "a1", "gold_split": "train"})
        assert r.status_code == 202, r.text
        assert created["gold_split"] == "train"


class _BatchCon:
    """Captura o executemany do auto-split transacional (48.0.0)."""
    def __init__(self):
        self.assignments = None

    async def executemany(self, sql, args):
        self.assignments = list(args)

    def transaction(self):
        con = self

        class _Tx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *exc):
                return False
        return _Tx()


class _BatchPool:
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


class TestAutoSplit:
    def _admin(self, monkeypatch):
        monkeypatch.setattr(dash, "require_role",
                            lambda *r: _async({"id": "u1", "role": "admin"}))

    def test_403_sem_papel(self, monkeypatch):
        """Review [13]: o split governa o anti-overfit — só root/admin."""
        from fastapi import HTTPException as _E

        def _deny(*r):
            async def _dep(request):
                raise _E(403, "Permissão insuficiente")
            return _dep

        monkeypatch.setattr(dash, "require_role", _deny)
        monkeypatch.setattr(dash.gold_cases_repo, "find_all",
                            _async([_mk_case("c1")]))
        r = _dash_client().post("/api/v1/gold-cases/auto-split",
                                json={"gold_version": "latest"})
        assert r.status_code == 403

    def test_divide_estratificado_transacional(self, monkeypatch):
        self._admin(monkeypatch)
        cases = ([_mk_case(f"a{i}", category="x") for i in range(7)]
                 + [_mk_case(f"b{i}", category="y") for i in range(3)]
                 + [_mk_case("adv1", case_type="adversarial")])
        monkeypatch.setattr(dash.gold_cases_repo, "find_all", _async(cases))
        con = _BatchCon()
        monkeypatch.setattr("app.core.database._get_pool",
                            lambda: _BatchPool(con))
        r = _dash_client().post("/api/v1/gold-cases/auto-split",
                                json={"gold_version": "latest",
                                      "holdout_pct": 0.3})
        assert r.status_code == 200, r.text
        body = r.json()
        # tudo num ÚNICO executemany (transacional, review [3]/[14])
        assert con.assignments is not None
        updates = {cid: split for (cid, split) in con.assignments}
        assert updates["adv1"] == "holdout"
        # categoria x (7): ceil(0.3*7)=3 holdout; y (3): ceil(0.9)=1
        assert sum(1 for cid, s in updates.items()
                   if s == "holdout" and cid.startswith("a")
                   and not cid.startswith("adv")) == 3
        assert sum(1 for cid, s in updates.items()
                   if s == "holdout" and cid.startswith("b")) == 1
        assert body["train"] == 6 and body["holdout"] == 5

    def test_holdout_zero_com_categorias_singleton(self, monkeypatch):
        """Review [9]: categorias de 1 caso → holdout=0; a mensagem NÃO pode
        prometer holdout que não existe."""
        self._admin(monkeypatch)
        cases = [_mk_case("s1", category="a"), _mk_case("s2", category="b"),
                 _mk_case("s3", category="c")]
        monkeypatch.setattr(dash.gold_cases_repo, "find_all", _async(cases))
        con = _BatchCon()
        monkeypatch.setattr("app.core.database._get_pool",
                            lambda: _BatchPool(con))
        r = _dash_client().post("/api/v1/gold-cases/auto-split",
                                json={"gold_version": "latest"})
        body = r.json()
        assert body["holdout"] == 0 and body["singleton_categories"] == 3
        assert "ATENÇÃO" in body["message"] and "sem holdout" in body["message"]

    def test_404_sem_casos(self, monkeypatch):
        self._admin(monkeypatch)
        monkeypatch.setattr(dash.gold_cases_repo, "find_all", _async([]))
        r = _dash_client().post("/api/v1/gold-cases/auto-split",
                                json={"gold_version": "latest"})
        assert r.status_code == 404


# ═══ Propositor train-only + promoção + sonda ════════════════════════════

def _opt_client():
    app = FastAPI()
    app.include_router(opt.router)
    return TestClient(app, raise_server_exceptions=False)


_SKILL_MD = ("---\nid: urn:skill:x:y:1\nversion: 1.0.0\nkind: subagent\n"
             "owner: t\nstability: stable\n---\n# Skill: X\n## Purpose\np\n")


class TestProposerTrainOnly:
    def test_resumo_so_do_treino_e_leak_cobre_holdout(self, monkeypatch):
        holdout_gab = ("gabarito SECRETO do holdout que jamais deveria "
                       "aparecer numa variante proposta")
        cases = [
            _mk_case("t1", split="train"),
            _mk_case("h1", split="holdout", expected_output=holdout_gab,
                     input_text="pergunta reservada do holdout com texto longo"),
        ]
        monkeypatch.setattr(opt, "require_role",
                            lambda *r: _async({"id": "u1", "role": "admin"}))
        monkeypatch.setattr(opt.agents_repo, "find_by_id",
                            _async({"id": "a1", "name": "Ag", "skill_id": "s1",
                                    "system_prompt": "Você é o Ag."}))
        monkeypatch.setattr(opt.skills_repo, "find_by_id",
                            _async({"id": "s1", "raw_content": _SKILL_MD}))
        monkeypatch.setattr(opt.gold_cases_repo, "find_all", _async(cases))
        monkeypatch.setattr(opt.eval_runs_repo, "find_all", _async([]))

        async def _resolve(task, **kw):
            return ("azure", "gpt-5-opt") if task == "optimizer" else ("azure", "gpt-4o")

        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", _resolve)
        monkeypatch.setattr("app.core.cost_ledger.record_invocation_cost",
                            _async(None))
        # variante ecoa o GABARITO do holdout → leak (mesmo o resumo não o vendo)
        leak_content = json.dumps({
            "system_prompt": f"Responda: {holdout_gab}.", "rationale": "r"})

        async def _llm(messages, provider, model, **kw):
            # holdout invisível ao propositor
            assert "SECRETO" not in messages[1]["content"]
            assert "split_note" in messages[1]["content"]
            return leak_content, provider, model

        monkeypatch.setattr("app.routes.wizard._wizard_llm_complete", _llm)
        r = _opt_client().post("/api/v1/optimizer/propose",
                               json={"agent_id": "a1", "n_variants": 1})
        assert r.status_code == 200, r.text
        body = r.json()
        assert [v["kind"] for v in body["variants"]] == ["control"]
        assert any("vazamento" in w for w in body["warnings"])


class TestSonda:
    def _wire(self, monkeypatch, *, champ_details, probe_details,
              probe_result=None, gold=None, champ_overrides=None):
        monkeypatch.setattr(opt, "require_role",
                            lambda *r: _async({"id": "u1", "role": "admin"}))
        monkeypatch.setattr(opt.agents_repo, "find_by_id",
                            _async({"id": "a1", "name": "Ag"}))
        runs = {
            "CH": {"id": "CH", "run_type": "experiment", "status": "completed",
                   "agent_id": "a1", "gold_split": "train",
                   "config_overrides": champ_overrides,
                   "details": json.dumps(champ_details)},
            "probe1": {"id": "probe1",
                       "details": json.dumps(probe_details)},
        }

        async def _find(rid):
            return dict(runs.get(rid) or {}) or None

        monkeypatch.setattr(opt.eval_runs_repo, "find_by_id", _find)
        # gold p/ o filtro de holdout (48.0.0) — default: casos do champion,
        # todos treino.
        _gold = gold if gold is not None else [
            _mk_case(d["case_id"], split="train",
                     category=d.get("category", "x"))
            for d in champ_details]
        monkeypatch.setattr(opt.gold_cases_repo, "find_all", _async(_gold))
        run_eval = AsyncMock(return_value=probe_result
                             or {"eval_id": "probe1", "status": "completed"})
        monkeypatch.setattr("app.harness.evaluator.run_evaluation", run_eval)
        return run_eval

    def _body(self):
        return {"agent_id": "a1", "release_id": "r1",
                "champion_eval_id": "CH", "n_cases": 4,
                "config_overrides": {"system_prompt": "variante"}}

    def test_go_quando_challenger_ganha(self, monkeypatch):
        champ = [{"case_id": f"c{i}", "passed": False, "category": "x"}
                 for i in range(4)]
        probe = [{"case_id": f"c{i}", "passed": True} for i in range(4)]
        run_eval = self._wire(monkeypatch, champ_details=champ,
                              probe_details=probe)
        r = _opt_client().post("/api/v1/optimizer/probe", json=self._body())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["go"] is True and body["probe_eval_id"] == "probe1"
        kw = run_eval.await_args.kwargs
        assert sorted(kw["case_ids"]) == ["c0", "c1", "c2", "c3"]
        assert kw["run_type"] == "experiment"
        assert kw["gold_split"] == "train"  # sonda mede SEMPRE no treino

    def test_no_go_em_paisagem_plana(self, monkeypatch):
        champ = [{"case_id": f"c{i}", "passed": True, "category": "x"}
                 for i in range(4)]
        probe = [{"case_id": f"c{i}", "passed": True} for i in range(4)]
        self._wire(monkeypatch, champ_details=champ, probe_details=probe)
        r = _opt_client().post("/api/v1/optimizer/probe", json=self._body())
        assert r.json()["go"] is False
        assert "NO-GO" in r.json()["note"]

    def test_422_champion_com_overrides(self, monkeypatch):
        """Review [10]: champion precisa ser a BASELINE (sem variante)."""
        champ = [{"case_id": f"c{i}", "passed": False, "category": "x"}
                 for i in range(4)]
        self._wire(monkeypatch, champ_details=champ, probe_details=[],
                   champ_overrides=json.dumps({"system_prompt": "outra"}))
        r = _opt_client().post("/api/v1/optimizer/probe", json=self._body())
        assert r.status_code == 422 and "config_overrides" in r.json()["detail"]

    def test_holdout_excluido_da_sonda(self, monkeypatch):
        """Review [7]: mesmo que o champion tenha avaliado holdout, a sonda
        NUNCA mede nesses casos."""
        champ = [{"case_id": f"c{i}", "passed": False, "category": "x"}
                 for i in range(6)]
        probe = [{"case_id": f"c{i}", "passed": True} for i in range(6)]
        gold = ([_mk_case(f"c{i}", split="train", category="x")
                 for i in range(4)]
                + [_mk_case(f"c{i}", split="holdout", category="x")
                   for i in range(4, 6)])
        run_eval = self._wire(monkeypatch, champ_details=champ,
                              probe_details=probe, gold=gold)
        r = _opt_client().post("/api/v1/optimizer/probe",
                               json={**self._body(), "n_cases": 6})
        assert r.status_code == 200, r.text
        kw = run_eval.await_args.kwargs
        # c4/c5 são holdout → excluídos; só treino entra
        assert set(kw["case_ids"]) == {"c0", "c1", "c2", "c3"}

    def test_inconclusivo_quando_mini_run_no_cases(self, monkeypatch):
        """Review [12]: mini-run que não avaliou casos NÃO é NO-GO."""
        champ = [{"case_id": f"c{i}", "passed": False, "category": "x"}
                 for i in range(4)]
        self._wire(monkeypatch, champ_details=champ, probe_details=[],
                   probe_result={"eval_id": "probe1", "status": "no_cases"})
        r = _opt_client().post("/api/v1/optimizer/probe", json=self._body())
        body = r.json()
        assert body["go"] is False and body["inconclusive"] is True
        assert "NO-GO" not in body["note"]

    def test_422_champion_sem_details(self, monkeypatch):
        self._wire(monkeypatch, champ_details=[], probe_details=[])
        r = _opt_client().post("/api/v1/optimizer/probe", json=self._body())
        assert r.status_code == 422 and "details" in r.json()["detail"]


class TestPromocaoAvisoTreino:
    def test_aviso_quando_par_medido_no_treino(self, monkeypatch):
        det = json.dumps([{"case_id": f"d{i}", "passed": True}
                          for i in range(7)])
        det_a = json.dumps([{"case_id": f"d{i}", "passed": False}
                            for i in range(7)])
        runs = {
            "A": {"id": "A", "run_type": "experiment", "status": "completed",
                  "agent_id": "a1", "pipeline_id": None, "gold_version": "v1",
                  "gold_hash": "h1", "config_overrides": None,
                  "total_cases": 7, "details": det_a},
            "B": {"id": "B", "run_type": "experiment", "status": "completed",
                  "agent_id": "a1", "pipeline_id": None, "gold_version": "v1",
                  "gold_hash": "h1", "gold_split": "train", "total_cases": 7,
                  "config_overrides": json.dumps({"system_prompt": "novo"}),
                  "details": det},
        }
        monkeypatch.setattr(opt, "require_role",
                            lambda *r: _async({"id": "u1", "role": "admin",
                                               "username": "root"}))
        monkeypatch.setattr(opt.agents_repo, "find_by_id", _async({
            "id": "a1", "version": "1.0.0", "system_prompt": "atual",
            "llm_provider": "azure", "model": "gpt-4o"}))
        monkeypatch.setattr(opt.agents_repo, "update", _async({}))

        async def _find(rid):
            return dict(runs.get(rid) or {}) or None

        monkeypatch.setattr(opt.eval_runs_repo, "find_by_id", _find)
        monkeypatch.setattr("app.core.database.pipelines_repo.find_all",
                            _async([]))
        monkeypatch.setattr("app.core.database.audit_repo.create", _async(None))
        import app.core.revisions as revisions
        monkeypatch.setattr(revisions, "safe_backfill", _async(None))
        monkeypatch.setattr(revisions, "safe_record", _async("rev1"))
        r = _opt_client().post("/api/v1/optimizer/promote", json={
            "agent_id": "a1", "champion_eval_id": "A",
            "challenger_eval_id": "B"})
        assert r.status_code == 200, r.text
        assert any("TREINO" in w for w in r.json()["warnings"])


class TestLastRunNaoVazaHoldout:
    def test_summarize_last_run_exclui_holdout(self):
        """Review [1]/[6]: falhas de casos de holdout no último run NÃO podem
        chegar ao feedback do propositor."""
        from app.optimizer.proposer import summarize_last_run
        run = {"run_type": "baseline", "accuracy": 0.5, "gate_result": "rejected",
               "details": json.dumps([
                   {"case_id": "t1", "passed": False, "category": "tec",
                    "expected_state": "Refuse", "actual_state": "Recommend"},
                   {"case_id": "h1", "passed": False, "category": "SECRETA",
                    "expected_state": "Refuse", "actual_state": "Recommend"},
               ])}
        out = summarize_last_run(run, exclude_case_ids={"h1"})
        cats = [f["categoria"] for f in out["falhas"]]
        assert "tec" in cats and "SECRETA" not in cats
        # sem exclusão, o holdout apareceria (prova que o filtro é o guard)
        out2 = summarize_last_run(run)
        assert "SECRETA" in [f["categoria"] for f in out2["falhas"]]


class TestRetencaoCapturas:
    @pytest.mark.asyncio
    async def test_purge_sintetico_apaga_capturas_por_idade(self, monkeypatch):
        import app.core.retention as retention
        monkeypatch.setattr(retention, "_synthetic_retention_days", lambda: 7)

        class _Con:
            def __init__(self):
                self.deletes = []

            async def fetch(self, sql, *a):
                return []

            async def execute(self, sql, *a):
                self.deletes.append(sql)
                return "DELETE 3"

        con = _Con()

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return con

                    async def __aexit__(self, *exc):
                        return False
                return _Ctx()

        monkeypatch.setattr(retention, "_pool", lambda: _Pool())
        monkeypatch.setattr(retention, "_purge_ids",
                            _async({"deleted": 0, "scrubbed_verifications": 0}))
        out = await retention.purge_synthetic_once()
        assert out["experiment_captures_deleted"] == 3
        assert any("experiment_case_results" in s for s in con.deletes)


def test_templates_da_fatia():
    from pathlib import Path
    src = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")
    assert 'data-testid="gold-auto-split"' in src
    assert 'data-testid="harness-goldsplit"' in src
    assert 'data-testid="optimizer-probe"' in src
    # promote handler mostra os warnings do backend (review [2]/[18])
    assert "r.warnings||[]" in src.replace(" ", "")
    assert "gold_split:'train'" in src  # A/B do otimizador mede no treino
