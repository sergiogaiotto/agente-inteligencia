"""Experimentos de prompt — seam + McNemar (44.0.0, PR3a do arco Otimização).

Cobre:
1. Seam do engine (_apply_experiment_overrides): overrides efêmeros de
   system_prompt/skill_purpose com CÓPIA defensiva (cache de topologia nunca
   é envenenado); allowlist por chaves fixas; no-op sem skill.
2. Propagação: run_evaluation passa config_overrides ao execute_interaction;
   run_type='experiment' NÃO escreve drift events; worker do jobs.py claima a
   coluna config_overrides (JSON TEXT) e repassa.
3. McNemar exato (mcnemar_exact_p) + comparação pareada no /eval-runs/compare
   (verdito a_melhor/b_melhor/inconclusivo/empate + truncated).
4. Rota /eval-runs/execute: validação do experimento (só agente, allowlist,
   run_type obrigatório, recusa skill declarativa) e persistência do selo.
5. Lista default sem experiments (include_experiments=false).

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
from app.agents.engine import _apply_experiment_overrides
from app.harness.evaluator import mcnemar_exact_p
from app.routes.dashboard import _paired_comparison


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ═══ 1. Seam do engine ═══════════════════════════════════════════════════

class TestSeamEngine:
    def test_none_e_noop_sem_copia(self):
        agent = {"system_prompt": "original"}
        assert _apply_experiment_overrides(agent, None) is agent

    def test_override_system_prompt_com_copia_defensiva(self):
        agent = {"system_prompt": "original", "_parsed_skill": {"purpose": "p0"}}
        out = _apply_experiment_overrides(agent, {"system_prompt": "variante"})
        assert out["system_prompt"] == "variante"
        # o dict ORIGINAL (potencialmente cacheado) permanece intacto
        assert agent["system_prompt"] == "original"

    def test_override_purpose_nao_vaza_para_o_skill_data_original(self):
        skill = {"purpose": "p0", "workflow": "w0"}
        agent = {"system_prompt": "s", "_parsed_skill": skill}
        out = _apply_experiment_overrides(agent, {"skill_purpose": "p1"})
        assert out["_parsed_skill"]["purpose"] == "p1"
        assert out["_parsed_skill"]["workflow"] == "w0"
        assert skill["purpose"] == "p0"  # original intacto (cópia)

    def test_purpose_sem_skill_e_noop(self):
        agent = {"system_prompt": "s", "_parsed_skill": {}}
        out = _apply_experiment_overrides(agent, {"skill_purpose": "p1"})
        assert out.get("_parsed_skill") == {}

    def test_chave_desconhecida_e_ignorada(self):
        # defesa em profundidade: a rota valida; o seam simplesmente não lê
        # chaves fora do allowlist (Decisions/Inputs nunca são tocáveis aqui)
        agent = {"system_prompt": "s", "decisions": "seladas"}
        out = _apply_experiment_overrides(agent, {"decisions": "hack"})
        assert out["decisions"] == "seladas"


# ═══ 2. Propagação evaluator/jobs ════════════════════════════════════════

_GOLD = {
    "id": "gc1", "input_text": "oi", "expected_state": "Recommend",
    "expected_output": "", "weight": 1.0, "category": "t",
    "case_type": "normal", "channel": "api", "red_flags": None,
}

_RESULT = {
    "output": "resposta", "final_state": "LogAndClose",
    "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
    "interaction_id": "int-1", "trace": {},
}


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


def _wire_agent(monkeypatch):
    created, updated = {}, {}

    async def _create(d):
        created.update(d)

    async def _update(_id, d):
        updated.update(d)

    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _create)
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _async([]))
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async([dict(_GOLD)]))
    monkeypatch.setattr(evaluator.agents_repo, "find_by_id", _async({"id": "a1"}))
    exec_int = AsyncMock(return_value=dict(_RESULT))
    monkeypatch.setattr(evaluator, "execute_interaction", exec_int)
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())
    monkeypatch.setattr(evaluator, "_tag_synthetic_interactions", _async(None))
    monkeypatch.setattr("app.core.cost_ledger.record_invocation_cost", _async(None))
    monkeypatch.setattr("app.core.api_key_budget.cost_and_tokens_from_result",
                        lambda r: (0.0, 0))
    drift_calls = []

    async def _drift(**kw):
        drift_calls.append(kw)

    monkeypatch.setattr(evaluator, "_write_drift_events", _drift)
    return created, updated, exec_int, drift_calls


class TestPropagacao:
    @pytest.mark.asyncio
    async def test_overrides_chegam_ao_execute_interaction(self, monkeypatch):
        _, _, exec_int, _ = _wire_agent(monkeypatch)
        ov = {"system_prompt": "variante desafiante"}
        await evaluator.run_evaluation("r1", agent_id="a1",
                                       run_type="experiment",
                                       config_overrides=ov)
        assert exec_int.await_args.kwargs["config_overrides"] == ov

    @pytest.mark.asyncio
    async def test_experiment_nao_escreve_drift(self, monkeypatch):
        _, _, _, drift = _wire_agent(monkeypatch)
        await evaluator.run_evaluation("r1", agent_id="a1",
                                       run_type="experiment")
        assert drift == []

    @pytest.mark.asyncio
    async def test_baseline_segue_escrevendo_drift(self, monkeypatch):
        _, _, _, drift = _wire_agent(monkeypatch)
        await evaluator.run_evaluation("r1", agent_id="a1")
        assert len(drift) == 1

    @pytest.mark.asyncio
    async def test_selo_persistido_no_caminho_sincrono(self, monkeypatch):
        """Review [2]: com harness_async_enabled OFF (default), o selo da
        variante TEM que ir na criação da linha — senão experimentos ficam
        indistinguíveis no banco."""
        created, _, _, _ = _wire_agent(monkeypatch)
        ov = {"system_prompt": "v2"}
        await evaluator.run_evaluation("r1", agent_id="a1",
                                       run_type="experiment",
                                       config_overrides=ov)
        assert json.loads(created["config_overrides"]) == ov

    @pytest.mark.asyncio
    async def test_pipeline_com_overrides_e_invalid_target(self, monkeypatch):
        """Review [16]: o branch pipeline não repassa overrides — aceitar
        mediria o champion 2× em silêncio; recusa com terminal persistido."""
        upd = {}

        async def _update(_id, d):
            upd.update(d)

        monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
        monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())
        out = await evaluator.run_evaluation(
            "r1", pipeline_id="p1", eval_id="ev-x",
            config_overrides={"system_prompt": "v"})
        assert out["status"] == "invalid_target"
        assert upd["status"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_drift_reader_pula_experiment_como_baseline(self, monkeypatch):
        """Review [1]: a segregação vale nas DUAS direções — um challenger
        concluído não pode virar o b0 do próximo run normal."""
        rows = [
            {"run_type": "experiment", "accuracy": 0.4},
            {"run_type": "baseline", "accuracy": 0.9},
        ]
        monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _async(rows))
        seen = []

        async def _emit(release_id, agent_id, pipeline_id, metric, base, cur,
                        *a, **k):
            seen.append((metric, base))
            return False

        monkeypatch.setattr(evaluator, "_emit_drift_event", _emit)
        await evaluator._write_drift_events(
            release_id="r1", gold_hash="h", current_metrics={"accuracy": 0.8},
            regression_pct_threshold=5.0, agent_id="a1")
        accs = [s for s in seen if s[0] == "accuracy"]
        assert accs and accs[0][1] == 0.9  # b0 = baseline REAL, não o challenger

    @pytest.mark.asyncio
    async def test_worker_repassa_config_overrides_da_linha(self, monkeypatch):
        row = {
            "id": "ev1", "release_id": "r1", "gold_version": "latest",
            "run_type": "experiment", "agent_id": "a1", "pipeline_id": None,
            "owner_user_id": "u1",
            "config_overrides": json.dumps({"system_prompt": "v2"}),
        }

        class _Con:
            async def fetchrow(self, sql, *a):
                # review [15]: o RETURNING do claim TEM que carregar a coluna
                # — sem este assert, removê-la manteria o teste verde.
                assert "config_overrides" in sql
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
        assert run_eval.await_args.kwargs["config_overrides"] == {"system_prompt": "v2"}

    @pytest.mark.asyncio
    async def test_worker_overrides_corrompido_vira_none(self, monkeypatch):
        row = {
            "id": "ev1", "release_id": "r1", "gold_version": "latest",
            "run_type": "experiment", "agent_id": "a1", "pipeline_id": None,
            "owner_user_id": "u1", "config_overrides": "{corrompido",
        }

        class _Con:
            async def fetchrow(self, sql, *a):
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
        assert run_eval.await_args.kwargs["config_overrides"] is None


# ═══ 3. McNemar + pareado ════════════════════════════════════════════════

class TestMcNemar:
    def test_sem_discordantes_p_1(self):
        assert mcnemar_exact_p(0, 0) == 1.0

    def test_6_a_0_e_significativo(self):
        assert mcnemar_exact_p(6, 0) == pytest.approx(0.03125)

    def test_5_a_0_nao_e_significativo(self):
        assert mcnemar_exact_p(5, 0) == pytest.approx(0.0625)

    def test_9_a_1_e_significativo(self):
        assert mcnemar_exact_p(9, 1) == pytest.approx(2 * 11 / 1024)

    def test_simetrico(self):
        assert mcnemar_exact_p(3, 7) == mcnemar_exact_p(7, 3)

    def test_equilibrio_p_alto(self):
        assert mcnemar_exact_p(5, 5) > 0.5


def _run_with_details(details, total=None):
    return {"details": details,
            "total_cases": total if total is not None else len(details)}


def _mk_details(passes: dict) -> list:
    return [{"case_id": k, "passed": v} for k, v in passes.items()]


class TestPairedComparison:
    def test_b_melhor_significativo(self):
        base = {f"c{i}": True for i in range(10)}
        a = _run_with_details(_mk_details({**base, **{f"d{i}": False for i in range(7)}}))
        b = _run_with_details(_mk_details({**base, **{f"d{i}": True for i in range(7)}}))
        out = _paired_comparison(a, b)
        assert out["only_b_passes"] == 7 and out["only_a_passes"] == 0
        assert out["verdict"] == "b_melhor"
        assert out["mcnemar_p"] < 0.05
        assert out["truncated"] is False

    def test_inconclusivo_com_poucos_discordantes(self):
        a = _run_with_details(_mk_details({"c1": True, "c2": False, "c3": True}))
        b = _run_with_details(_mk_details({"c1": True, "c2": True, "c3": False}))
        out = _paired_comparison(a, b)
        assert out["verdict"] == "inconclusivo"
        assert "insuficientes" in out["verdict_note"]

    def test_empate_sem_discordantes(self):
        a = _run_with_details(_mk_details({"c1": True, "c2": False}))
        b = _run_with_details(_mk_details({"c1": True, "c2": False}))
        out = _paired_comparison(a, b)
        assert out["verdict"] == "empate"
        assert out["both_pass"] == 1 and out["both_fail"] == 1

    def test_truncated_quando_details_capados(self):
        a = _run_with_details(_mk_details({"c1": True}), total=150)
        b = _run_with_details(_mk_details({"c1": True}))
        assert _paired_comparison(a, b)["truncated"] is True

    def test_sem_pareamento_quando_details_vazios(self):
        """Review [3]: zero casos pareáveis (details ausentes/corrompidos)
        NÃO é 'empate' — seria falsa confiança afirmativa."""
        a = _run_with_details([], total=50)
        b = _run_with_details([], total=50)
        out = _paired_comparison(a, b)
        assert out["verdict"] == "sem_pareamento"
        assert out["paired_cases"] == 0 and out["truncated"] is True

    def test_details_em_string_sao_tolerados(self):
        a = {"details": json.dumps(_mk_details({"c1": True})), "total_cases": 1}
        b = {"details": json.dumps(_mk_details({"c1": False})), "total_cases": 1}
        out = _paired_comparison(a, b)
        assert out["paired_cases"] == 1 and out["only_a_passes"] == 1


# ═══ 4. Rota /execute + 5. lista ═════════════════════════════════════════

def _client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


_BODY = {"release_id": "r1", "agent_id": "a1", "gold_version": "latest",
         "run_type": "experiment",
         "config_overrides": {"system_prompt": "variante"}}


def _wire_route(monkeypatch, *, skill_raw=None):
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
    monkeypatch.setattr(dash.agents_repo, "find_by_id",
                        _async({"id": "a1", "skill_id": "s1" if skill_raw else None}))
    monkeypatch.setattr(dash.skills_repo, "find_by_id",
                        _async({"id": "s1", "raw_content": skill_raw} if skill_raw else None))
    # Gate de papel do experimento (review [7]): nos testes o caller é admin.
    monkeypatch.setattr(dash, "require_role",
                        lambda *roles: _async({"id": "u1", "role": "admin"}))


class TestRotaExperiment:
    def test_422_overrides_com_pipeline(self, monkeypatch):
        _wire_route(monkeypatch)
        from app.core.database import pipelines_repo
        monkeypatch.setattr(pipelines_repo, "find_by_id", _async({"id": "p1"}))
        body = {**_BODY, "agent_id": None, "pipeline_id": "p1"}
        r = _client().post("/api/v1/eval-runs/execute", json=body)
        assert r.status_code == 422 and "UM agente" in r.json()["detail"]

    def test_422_chave_fora_do_allowlist(self, monkeypatch):
        _wire_route(monkeypatch)
        body = {**_BODY, "config_overrides": {"decisions": "hack"}}
        r = _client().post("/api/v1/eval-runs/execute", json=body)
        assert r.status_code == 422 and "não permitidas" in r.json()["detail"]

    def test_422_sem_run_type_experiment(self, monkeypatch):
        _wire_route(monkeypatch)
        body = {**_BODY, "run_type": "baseline"}
        r = _client().post("/api/v1/eval-runs/execute", json=body)
        assert r.status_code == 422 and "experiment" in r.json()["detail"]

    def test_422_skill_declarativa(self, monkeypatch):
        _wire_route(monkeypatch, skill_raw=(
            "---\nid: urn:skill:x:y:1\nversion: 1.0.0\nkind: subagent\n"
            "owner: t\nstability: stable\nexecution_mode: declarative\n---\n"
            "# Skill: X\n## Purpose\np\n"))
        r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
        assert r.status_code == 422 and "DECLARATIVA" in r.json()["detail"]

    def test_sync_repassa_overrides(self, monkeypatch):
        _wire_route(monkeypatch)
        seen = {}

        async def _run_eval(*a, **kw):
            seen.update(kw)
            return {"status": "completed", "accuracy": 1.0, "gate_result": "approved"}

        monkeypatch.setattr("app.harness.evaluator.run_evaluation", _run_eval)
        r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
        assert r.status_code == 200, r.text
        assert seen["config_overrides"] == {"system_prompt": "variante"}

    def test_202_persiste_o_selo_do_experimento(self, monkeypatch):
        _wire_route(monkeypatch)
        created = {}

        async def _create(d):
            created.update(d)

        monkeypatch.setattr(dash.eval_runs_repo, "create", _create)
        monkeypatch.setattr(jobs, "dispatch", lambda eid: True)
        monkeypatch.setattr("app.core.config.get_settings",
                            lambda: SimpleNamespace(harness_async_enabled=True))
        r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
        assert r.status_code == 202, r.text
        assert json.loads(created["config_overrides"]) == {"system_prompt": "variante"}
        assert created["run_type"] == "experiment"

    def test_403_overrides_sem_papel_elevado(self, monkeypatch):
        """Review [7]: injetar system_prompt + executar tools reais é poder
        novo — exige root/admin; sem papel, 403 antes de qualquer execução."""
        _wire_route(monkeypatch)
        from fastapi import HTTPException as _HTTPExc

        def _deny(*roles):
            async def _dep(request):
                raise _HTTPExc(403, "Permissão insuficiente")
            return _dep

        monkeypatch.setattr(dash, "require_role", _deny)
        r = _client().post("/api/v1/eval-runs/execute", json=_BODY)
        assert r.status_code == 403

    def test_422_run_type_fora_do_enum(self, monkeypatch):
        """Review [8]: a segregação depende do run_type — enum fechado."""
        _wire_route(monkeypatch)
        body = {"release_id": "r1", "agent_id": "a1", "run_type": "hack"}
        r = _client().post("/api/v1/eval-runs/execute", json=body)
        assert r.status_code == 422 and "run_type" in r.json()["detail"]

    def test_422_valores_invalidos_do_override(self, monkeypatch):
        """Review [18]: vazio/whitespace, não-string e >20000 chars."""
        _wire_route(monkeypatch)
        c = _client()
        r1 = c.post("/api/v1/eval-runs/execute",
                    json={**_BODY, "config_overrides": {"system_prompt": "   "}})
        r2 = c.post("/api/v1/eval-runs/execute",
                    json={**_BODY, "config_overrides": {"system_prompt": 123}})
        r3 = c.post("/api/v1/eval-runs/execute",
                    json={**_BODY, "config_overrides": {"system_prompt": "x" * 20001}})
        assert (r1.status_code, r2.status_code, r3.status_code) == (422, 422, 422)

    def test_422_skill_purpose_sem_skill(self, monkeypatch):
        """Review [5]: purpose sem skill seria no-op silencioso no seam — o
        'experimento' mediria o champion duas vezes."""
        _wire_route(monkeypatch)  # agente SEM skill
        body = {**_BODY, "config_overrides": {"skill_purpose": "p1"}}
        r = _client().post("/api/v1/eval-runs/execute", json=body)
        assert r.status_code == 422 and "Purpose" in r.json()["detail"]

    def test_lista_duas_janelas_experimentos_nao_expulsam_baselines(self, monkeypatch):
        """Reviews [4]/[6]: rajada de 50 challengers não pode expulsar o
        baseline real da janela (esvaziaria Baseline-por-alvo e a lista)."""
        exps = [{"id": f"e{i}", "run_type": "experiment",
                 "created_at": f"2026-07-17T10:{i % 60:02d}:00",
                 "dimension_breakdown": "{}", "details": "[]"}
                for i in range(50)]
        base = {"id": "b1", "run_type": "baseline",
                "created_at": "2026-07-01T00:00:00",
                "dimension_breakdown": "{}", "details": "[]"}

        async def _find_all(limit=20, **kw):
            if kw.get("run_type") == "experiment":
                return exps[:min(limit, 20)]
            return (exps + [base])[:limit]  # janela 3× (60) alcança o b1

        monkeypatch.setattr(dash.eval_runs_repo, "find_all", _find_all)
        # default: SEM experimentos e com o baseline visível
        r = _client().get("/api/v1/eval-runs")
        assert [x["id"] for x in r.json()["runs"]] == ["b1"]
        # harness page: experimentos entram por janela PRÓPRIA; b1 sobrevive
        r2 = _client().get("/api/v1/eval-runs?include_experiments=true")
        ids = [x["id"] for x in r2.json()["runs"]]
        assert "b1" in ids and len([i for i in ids if i.startswith("e")]) == 20

    def test_compare_traz_bloco_paired(self, monkeypatch):
        base = {
            "release_id": "r1", "status": "completed", "gold_version": "v1",
            "gold_hash": "h1", "agent_id": "a1", "pipeline_id": None,
            "run_type": "experiment", "total_cases": 2,
            "dimension_breakdown": "{}",
            "details": json.dumps([{"case_id": "c1", "passed": True},
                                   {"case_id": "c2", "passed": False}]),
        }
        runs = {"A": {**base, "id": "A"},
                "B": {**base, "id": "B",
                      "details": json.dumps([{"case_id": "c1", "passed": True},
                                             {"case_id": "c2", "passed": True}])}}

        async def _find(rid):
            return dict(runs.get(rid) or {}) or None

        monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _find)
        r = _client().get("/api/v1/eval-runs/compare?a=A&b=B")
        assert r.status_code == 200, r.text
        paired = r.json()["paired"]
        assert paired["only_b_passes"] == 1 and paired["verdict"] == "inconclusivo"

    def test_compare_nao_comparavel_traz_paired_none(self, monkeypatch):
        """Review [17]: runs de alvos diferentes → comparable=false e o bloco
        pareado NÃO aparece (pino de regressão da posição no if)."""
        runs = {
            "A": {"id": "A", "status": "completed", "gold_version": "v1",
                  "gold_hash": "h1", "agent_id": "a1", "pipeline_id": None,
                  "dimension_breakdown": "{}", "details": "[]"},
            "B": {"id": "B", "status": "completed", "gold_version": "v1",
                  "gold_hash": "h1", "agent_id": "a2", "pipeline_id": None,
                  "dimension_breakdown": "{}", "details": "[]"},
        }

        async def _find(rid):
            return dict(runs.get(rid) or {}) or None

        monkeypatch.setattr(dash.eval_runs_repo, "find_by_id", _find)
        r = _client().get("/api/v1/eval-runs/compare?a=A&b=B")
        body = r.json()
        assert body["comparable"] is False and body["paired"] is None


def test_template_compare_mostra_veredito_pareado():
    from pathlib import Path
    src = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")
    assert 'data-testid="compare-paired"' in src
    assert "include_experiments=true" in src
