"""Pacote C (33.20.0) — harness modo PIPELINE + gate de regressão configurável.

C1: run_evaluation aceita pipeline_id e invoca a cadeia SELADA por gold case
(mesmo caminho do invoke: _build_subgraph → execute_pipeline com
allowed_agent_ids). O roteamento vira parte da avaliação: `path` por caso,
verification do último step completed, alvo gravado no run.
C3: max_regression_pct (acurácia) saiu do hardcode GATE_THRESHOLDS e virou
settings.harness_max_regression_pct (runtime-editável).

Mocks em app.harness.evaluator (repos/engine) — sem DB/LLM reais.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.harness.evaluator as evaluator
import app.routes.dashboard as dash


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def json_deepcopy(obj):
    import json
    return json.loads(json.dumps(obj))


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


_GOLD_CASE = {
    "id": "gc1", "input_text": "minha internet caiu", "expected_state": "Recommend",
    "expected_output": "", "weight": 1.0, "category": "tecnico",
    "case_type": "normal", "channel": "api", "red_flags": None,
}

# Fan-out 1-de-N realista (blocker do review adversarial): o ÚLTIMO nó da
# cadeia BFS é um step PULADO, então o envelope do engine expõe
# final_state='SkippedConditional' e transitions=[] no top-level (item B2).
# O harness precisa reancorar decisão/verification no último step COMPLETED
# (Esp. NOC — o dono do output avaliado), senão todo caso roteado a um ramo
# que não seja o último nó reprova por state_mismatch.
_PIPE_RESULT = {
    "output": "Incidente SEV-2 aberto, SLA 8h.",
    "final_state": "SkippedConditional",
    "transitions": [],
    "interaction_id": "master-1",
    "duration_ms": 123.0,
    "pipeline_steps": [
        {"agent_name": "Maestro", "status": "completed",
         # verification de OUTRO step (major do review): NÃO pode ser usada
         # para julgar o output final — dims de score 1 denunciariam.
         "verification": {"ok": True, "confidence": 0.5,
                          "dimensions": {"factuality": {"score": 1},
                                         "completeness": {"score": 1},
                                         "tone_adherence": {"score": 1},
                                         "safety": {"score": 1}}}},
        {"agent_name": "Triagem Téc", "status": "completed"},
        {"agent_name": "Esp. NOC", "status": "completed",
         "final_state": "LogAndClose",
         "transitions": [{"from": "Recommend", "to": "LogAndClose"}],
         "verification": {"ok": True, "confidence": 0.9,
                          "dimensions": {"factuality": {"score": 5},
                                         "completeness": {"score": 4},
                                         "tone_adherence": {"score": 5},
                                         "safety": {"score": 1}}}},
        {"agent_name": "Esp. Planos", "status": "skipped_conditional"},
    ],
}


def _wire_pipeline_mocks(monkeypatch, *, pipe=None, subgraph=None, result=None,
                         find_all_baseline=None):
    created, updated = {}, {}

    async def _create(data):
        created.update(data)

    async def _update(_id, data):
        updated.update(data)

    async def _find_all(**kw):
        if "gold_hash" in kw or kw.get("run_type") == "baseline":
            return find_all_baseline(kw) if find_all_baseline else []
        return [dict(_GOLD_CASE)]

    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _create)
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _find_all)
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async([dict(_GOLD_CASE)]))
    monkeypatch.setattr(
        evaluator.pipelines_repo, "find_by_id",
        _async(pipe if pipe is not None else {"id": "p1", "name": "Pulsar", "status": "rascunho"}),
    )
    monkeypatch.setattr(
        "app.catalog.pipeline_defs._build_subgraph",
        _async(subgraph if subgraph is not None
               else {"root_agent_id": "root-1", "nodes": [{"id": "root-1"}, {"id": "a2"}]}),
    )
    exec_pipe = AsyncMock(return_value=result or dict(_PIPE_RESULT))
    monkeypatch.setattr(evaluator, "execute_pipeline", exec_pipe)
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings", lambda: _settings_stub())
    return created, updated, exec_pipe


class TestRunEvaluationPipelineMode:
    @pytest.mark.asyncio
    async def test_invoca_cadeia_selada_e_aprova(self, monkeypatch):
        created, updated, exec_pipe = _wire_pipeline_mocks(monkeypatch)

        out = await evaluator.run_evaluation("r1", pipeline_id="p1")

        assert out["gate_result"] == "approved" and out["passed"] == 1
        # alvo gravado no run desde o create
        assert created["pipeline_id"] == "p1" and created["agent_id"] is None
        # cadeia SELADA: root + membros + grounding pinado (reprodutibilidade)
        kw = exec_pipe.await_args.kwargs
        assert kw["entry_agent_id"] == "root-1"
        assert kw["allowed_agent_ids"] == {"root-1", "a2"}
        assert kw["pipeline_id"] == "p1"
        assert kw["grounding_strict"] is False
        assert kw["context_mode"] == "none"

    @pytest.mark.asyncio
    async def test_ultimo_no_pulado_nao_reprova_o_caso(self, monkeypatch):
        """BLOCKER do review: fan-out 1-de-N termina em step PULADO →
        top-level final_state='SkippedConditional'. A decisão deve vir do
        último step COMPLETED (Esp. NOC: Recommend) — o caso PASSA."""
        _, updated, _ = _wire_pipeline_mocks(monkeypatch)
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        import json
        details = json.loads(updated["details"])
        assert details[0]["actual_state"] == "Recommend"
        assert details[0]["passed"] is True
        assert out["passed"] == 1

    @pytest.mark.asyncio
    async def test_path_registrado_por_caso(self, monkeypatch):
        _, updated, _ = _wire_pipeline_mocks(monkeypatch)
        await evaluator.run_evaluation("r1", pipeline_id="p1")
        import json
        details = json.loads(updated["details"])
        assert details[0]["path"] == [
            "Maestro:ok", "Triagem Téc:ok", "Esp. NOC:ok", "Esp. Planos:skip",
        ]

    @pytest.mark.asyncio
    async def test_verification_vem_do_dono_do_output(self, monkeypatch):
        """MAJOR do review: as dims vêm do ÚLTIMO step completed (Esp. NOC,
        factuality 5) — nunca de outro step (Maestro tem factuality 1)."""
        _, updated, _ = _wire_pipeline_mocks(monkeypatch)
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["avg_factuality"] == 5.0
        assert out["judge_used"] is True

    @pytest.mark.asyncio
    async def test_dono_do_output_sem_verification_nao_herda_de_outro_step(self, monkeypatch):
        """Se o dono do output NÃO tem snapshot, a verification de um step
        anterior (Maestro) não pode ser usada — dims ficam None (ou re-judge
        quando use_verifier)."""
        result = json_deepcopy(_PIPE_RESULT)
        del result["pipeline_steps"][2]["verification"]  # Esp. NOC sem snapshot
        _, updated, _ = _wire_pipeline_mocks(monkeypatch, result=result)
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["avg_factuality"] is None
        assert out["judge_used"] is False

    @pytest.mark.asyncio
    async def test_pipeline_inexistente_encerra_sem_avaliar(self, monkeypatch):
        _, updated, exec_pipe = _wire_pipeline_mocks(monkeypatch, pipe=False)
        monkeypatch.setattr(evaluator.pipelines_repo, "find_by_id", _async(None))
        out = await evaluator.run_evaluation("r1", pipeline_id="ghost")
        assert out["status"] == "invalid_pipeline"
        assert exec_pipe.await_count == 0
        assert updated["status"] == "invalid_pipeline"

    @pytest.mark.asyncio
    async def test_pipeline_aposentado_encerra(self, monkeypatch):
        _wire_pipeline_mocks(monkeypatch, pipe={"id": "p1", "name": "X", "status": "aposentado"})
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["status"] == "invalid_pipeline"
        assert "aposentado" in out["message"]

    @pytest.mark.asyncio
    async def test_sem_root_encerra(self, monkeypatch):
        _wire_pipeline_mocks(monkeypatch, subgraph={"root_agent_id": None, "nodes": []})
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["status"] == "invalid_pipeline"

    @pytest.mark.asyncio
    async def test_xor_de_alvo(self, monkeypatch):
        _wire_pipeline_mocks(monkeypatch)
        both = await evaluator.run_evaluation("r1", agent_id="a1", pipeline_id="p1")
        neither = await evaluator.run_evaluation("r1")
        assert both["status"] == "invalid_target"
        assert neither["status"] == "invalid_target"


class TestRegressionGateConfiguravel:
    """C3 + filtro por alvo: baseline de regressão do MESMO alvo, e o
    threshold de acurácia agora vem de settings.harness_max_regression_pct."""

    def _baseline_provider(self, captured):
        def _provider(kw):
            if kw.get("run_type") == "baseline":
                captured.update(kw)
                return [{"accuracy": 1.0, "avg_factuality": None,
                         "avg_completeness": None, "avg_tone": None}]
            return []  # drift baseline: nenhum
        return _provider

    @pytest.mark.asyncio
    async def test_regressao_usa_setting_e_filtra_por_alvo(self, monkeypatch):
        captured: dict = {}
        # caso que FALHA (expected_state divergente) → accuracy 0.0 vs baseline 1.0
        failing = dict(_GOLD_CASE, expected_state="Refuse")
        _, updated, _ = _wire_pipeline_mocks(
            monkeypatch, find_all_baseline=self._baseline_provider(captured))
        monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async([failing]))

        out = await evaluator.run_evaluation("r1", pipeline_id="p1", run_type="regression")
        assert out["gate_result"] == "rejected"
        assert any("regression_accuracy" in r for r in (out["gate_reason"] or "").split("; "))
        # baseline filtrado pelo MESMO alvo (pipeline)
        assert captured.get("pipeline_id") == "p1"

    @pytest.mark.asyncio
    async def test_threshold_alto_tolera_queda(self, monkeypatch):
        captured: dict = {}
        failing = dict(_GOLD_CASE, expected_state="Refuse")
        _wire_pipeline_mocks(
            monkeypatch, find_all_baseline=self._baseline_provider(captured))
        monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async([failing]))
        monkeypatch.setattr(
            evaluator, "get_settings",
            lambda: _settings_stub(harness_max_regression_pct=200.0,
                                   harness_min_accuracy=0.0),
        )
        out = await evaluator.run_evaluation("r1", pipeline_id="p1", run_type="regression")
        assert not any("regression_accuracy" in r for r in (out["gate_reason"] or "").split("; "))


class TestRotaExecuteXor:
    def _client(self):
        app = FastAPI()
        app.include_router(dash.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_422_sem_alvo_ou_com_ambos(self, monkeypatch):
        monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
        base = {"release_id": "r1", "gold_version": "latest", "run_type": "baseline"}
        r1 = self._client().post("/api/v1/eval-runs/execute", json=base)
        r2 = self._client().post("/api/v1/eval-runs/execute",
                                 json={**base, "agent_id": "a1", "pipeline_id": "p1"})
        assert r1.status_code == 422 and r2.status_code == 422

    def test_404_pipeline_inexistente(self, monkeypatch):
        monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
        monkeypatch.setattr("app.core.database.pipelines_repo.find_by_id", _async(None))
        r = self._client().post(
            "/api/v1/eval-runs/execute",
            json={"release_id": "r1", "pipeline_id": "ghost"})
        assert r.status_code == 404 and "Pipeline" in r.json()["detail"]

    def test_200_modo_pipeline_repassa_alvo(self, monkeypatch):
        monkeypatch.setattr(dash.releases_repo, "find_by_id", _async({"id": "r1"}))
        monkeypatch.setattr("app.core.database.pipelines_repo.find_by_id", _async({"id": "p1"}))
        seen = {}

        async def _fake_run(release_id, agent_id, gold_version, run_type,
                            pipeline_id=None, owner_user_id=None,
                            config_overrides=None, gold_split=None):
            # owner_user_id (35.2.0): quem disparou vira dono das interactions
            # config_overrides (44.0.0): experimento — None fora de experiment
            # gold_split (48.0.0): fatia train/holdout — None = todos
            seen.update(release_id=release_id, agent_id=agent_id, pipeline_id=pipeline_id)
            return {"status": "completed", "accuracy": 1.0, "gate_result": "approved"}

        monkeypatch.setattr(evaluator, "run_evaluation", _fake_run)
        r = self._client().post(
            "/api/v1/eval-runs/execute",
            json={"release_id": "r1", "pipeline_id": "p1"})
        assert r.status_code == 200
        assert seen == {"release_id": "r1", "agent_id": None, "pipeline_id": "p1"}
