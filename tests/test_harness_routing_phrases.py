"""test_phrases → harness (36.5.0, item 1 PR1 do plano).

Run de PIPELINE roda as Frases-Prova das arestas condicionais — o MESMO
avaliador do gate de publish (evaluate_pipeline_test_phrases, determinístico,
zero LLM) — e agrega o resultado no eval_run: colunas routing_phrases_total/
passed/hash + bloco routing_phrases no dimension_breakdown. Gate OPT-IN
(harness_phrases_gate, default OFF → reprovação vira nota informativa).
Modo agente: N/A (frases pertencem a arestas) → colunas NULL.

Mocks em app.harness.evaluator e app.catalog.pipeline_defs — sem DB/LLM.
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


_GOLD_CASE = {
    "id": "gc1", "input_text": "minha internet caiu", "expected_state": "Recommend",
    "expected_output": "", "weight": 1.0, "category": "tecnico",
    "case_type": "normal", "channel": "api", "red_flags": None,
}

_PIPE_RESULT = {
    "output": "ok", "final_state": "Recommend", "transitions": [],
    "interaction_id": "m1", "duration_ms": 10.0,
    "pipeline_steps": [{"agent_name": "A", "status": "completed"}],
}

_PHRASES_REPORT = {
    "evaluated": 3, "passed": 2,
    "failing": [{
        "edge_id": "e1", "source_name": "Triagem", "target_name": "Planos",
        "expr": "'plano' in input_norm", "text": "quero um plano",
        "where": "input", "expect": True, "got": False, "error": None,
    }],
    "phrases_hash": "abc123def4567890",
}


def _wire(monkeypatch, *, phrases=None, settings=None):
    created, updated = {}, {}

    async def _create(data):
        created.update(data)

    async def _update(_id, data):
        updated.update(data)

    async def _find_all(**kw):
        return []  # baseline de regressão/drift: nenhum

    monkeypatch.setattr(evaluator.eval_runs_repo, "create", _create)
    monkeypatch.setattr(evaluator.eval_runs_repo, "update", _update)
    monkeypatch.setattr(evaluator.eval_runs_repo, "find_all", _find_all)
    monkeypatch.setattr(evaluator.gold_cases_repo, "find_all", _async([dict(_GOLD_CASE)]))
    monkeypatch.setattr(
        evaluator.pipelines_repo, "find_by_id",
        _async({"id": "p1", "name": "Pulsar", "status": "rascunho"}),
    )
    monkeypatch.setattr(
        "app.catalog.pipeline_defs._build_subgraph",
        _async({"root_agent_id": "root-1", "nodes": [{"id": "root-1"}]}),
    )
    monkeypatch.setattr(evaluator, "execute_pipeline", AsyncMock(return_value=dict(_PIPE_RESULT)))
    monkeypatch.setattr(evaluator, "_link_verification_to_gold_case", _async(None))
    monkeypatch.setattr(evaluator, "get_settings", lambda: settings or _settings_stub())

    phrases_mock = AsyncMock(
        return_value=phrases if phrases is not None else dict(_PHRASES_REPORT)
    )
    monkeypatch.setattr(
        "app.catalog.pipeline_defs.evaluate_pipeline_test_phrases", phrases_mock
    )
    return created, updated, phrases_mock


class TestRoutingPhrasesNoRun:
    @pytest.mark.asyncio
    async def test_pipeline_run_persiste_frases_e_hash(self, monkeypatch):
        _, updated, phrases_mock = _wire(monkeypatch)

        out = await evaluator.run_evaluation("r1", pipeline_id="p1")

        assert phrases_mock.await_args.args[0] == "p1"
        # subgrafo já resolvido é REPASSADO (sem re-fetch/TOCTOU)
        assert phrases_mock.await_args.kwargs["sub"]["root_agent_id"] == "root-1"
        assert updated["routing_phrases_total"] == 3
        assert updated["routing_phrases_passed"] == 2
        assert updated["routing_phrases_hash"] == "abc123def4567890"
        db = json.loads(updated["dimension_breakdown"])
        assert db["routing_phrases"]["evaluated"] == 3
        assert db["routing_phrases"]["failing"][0]["edge_id"] == "e1"
        assert out["routing_phrases"]["passed"] == 2

    @pytest.mark.asyncio
    async def test_gate_off_reprovacao_vira_nota_informativa(self, monkeypatch):
        _, updated, _ = _wire(monkeypatch)
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        # frase reprovada NÃO reprova o run (default OFF)...
        assert out["gate_result"] == "approved"
        # ...mas fica VISÍVEL (convenção: sem falsa confiança)
        assert "frases-prova" in (out["gate_reason"] or "")
        assert "informativo" in out["gate_reason"]

    @pytest.mark.asyncio
    async def test_gate_on_reprova_o_run(self, monkeypatch):
        _, updated, _ = _wire(
            monkeypatch, settings=_settings_stub(harness_phrases_gate=True)
        )
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["gate_result"] == "rejected"
        assert "routing_phrases: 1/3" in out["gate_reason"]

    @pytest.mark.asyncio
    async def test_todas_passando_sem_nota_e_aprovado(self, monkeypatch):
        report = dict(_PHRASES_REPORT, passed=3, failing=[])
        _, updated, _ = _wire(
            monkeypatch, phrases=report,
            settings=_settings_stub(harness_phrases_gate=True),
        )
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["gate_result"] == "approved"
        assert "frases-prova" not in (out["gate_reason"] or "")
        assert updated["routing_phrases_passed"] == 3

    @pytest.mark.asyncio
    async def test_modo_agente_nao_roda_frases(self, monkeypatch):
        _, updated, phrases_mock = _wire(monkeypatch)
        monkeypatch.setattr(evaluator.agents_repo, "find_by_id", _async({"id": "a1"}))
        monkeypatch.setattr(
            evaluator, "execute_interaction", AsyncMock(return_value=dict(_PIPE_RESULT))
        )
        out = await evaluator.run_evaluation("r1", agent_id="a1")
        assert out["status"] == "completed"
        assert phrases_mock.await_count == 0
        # NULL = não aplicável (≠ 0 = avaliou e não havia frases)
        assert updated["routing_phrases_total"] is None
        assert "routing_phrases" not in json.loads(updated["dimension_breakdown"])

    @pytest.mark.asyncio
    async def test_falha_de_infra_nao_derruba_o_run(self, monkeypatch):
        _, updated, phrases_mock = _wire(monkeypatch)
        phrases_mock.side_effect = RuntimeError("mesh indisponível")
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        assert out["status"] == "completed"
        assert updated["routing_phrases_total"] is None

    @pytest.mark.asyncio
    async def test_metricas_de_frases_chegam_ao_drift(self, monkeypatch):
        """36.6.0: run repassa pass-rate derivado + hash ao writer de drift
        (que tem guarda própria de comparabilidade por hash)."""
        _, updated, _ = _wire(monkeypatch)
        seen = {}

        async def _fake_drift(**kw):
            seen.update(kw)
            return 0

        monkeypatch.setattr(evaluator, "_write_drift_events", _fake_drift)
        await evaluator.run_evaluation("r1", pipeline_id="p1")
        cm = seen["current_metrics"]
        assert cm["routing_phrase_pass_rate"] == pytest.approx(2 / 3)
        assert cm["routing_phrases_hash"] == "abc123def4567890"

    @pytest.mark.asyncio
    async def test_failing_capado_e_clipado_na_fonte(self, monkeypatch):
        """failing entra no breakdown (teto 32KB) e no corpo do /execute —
        cap em PHRASES_FAILING_MAX + clip de 300 chars por campo string."""
        fat = [dict(_PHRASES_REPORT["failing"][0], edge_id=f"e{i}", expr="x" * 20)
               for i in range(60)]
        report = dict(_PHRASES_REPORT, evaluated=60, passed=0, failing=fat)
        _, updated, _ = _wire(monkeypatch, phrases=report)
        out = await evaluator.run_evaluation("r1", pipeline_id="p1")
        db = json.loads(updated["dimension_breakdown"])
        assert len(db["routing_phrases"]["failing"]) == 50
        assert len(out["routing_phrases"]["failing"]) == 50  # resposta idem

    @pytest.mark.asyncio
    async def test_breakdown_estourando_derruba_failing_mas_nao_corrompe(self, monkeypatch):
        """Guarda anti-corrupção: JSON acima de 32KB derruba o failing
        detalhado (mantém contagens) em vez do slice cego que invalidaria o
        breakdown INTEIRO (parse tolerante da UI descartaria tudo)."""
        fat = [dict(_PHRASES_REPORT["failing"][0], edge_id=f"e{i}",
                    expr="x" * 1000, error="y" * 1000, text="z" * 1000)
               for i in range(50)]
        report = dict(_PHRASES_REPORT, evaluated=50, passed=0, failing=fat)
        _, updated, _ = _wire(monkeypatch, phrases=report)
        await evaluator.run_evaluation("r1", pipeline_id="p1")
        db = json.loads(updated["dimension_breakdown"])  # JSON VÁLIDO
        assert db["routing_phrases"]["failing"] == []
        assert db["routing_phrases"]["failing_dropped"] is True
        assert db["routing_phrases"]["evaluated"] == 50
        # clip individual aconteceu antes do drop (campo string ≤ 301)
        assert len(updated["dimension_breakdown"]) <= 32000


class TestPhrasesHash:
    """phrases_hash em evaluate_pipeline_test_phrases: sela o CONTEÚDO do
    conjunto (edge_id + expr + frases) — comparabilidade entre runs."""

    def _subgraph(self, expr="decision.acao == 'planos'"):
        return {
            "root_agent_id": "root-1",
            "nodes": [{"id": "root-1", "name": "Triagem"}, {"id": "a2", "name": "Planos"}],
            "edges": [{
                "id": "e1", "source": "root-1", "target": "a2",
                "type": "conditional",
                "config": {
                    "expr": expr,
                    "test_phrases": [{"text": "quero um plano", "where": "input", "expect": True}],
                },
            }],
        }

    def _wire_defs(self, monkeypatch, subgraph):
        import app.catalog.pipeline_defs as defs
        monkeypatch.setattr(defs, "_build_subgraph", _async(subgraph))
        monkeypatch.setattr(
            "app.agents.engine.evaluate_test_phrases_for_edge",
            _async([{"text": "quero um plano", "where": "input", "expect": True,
                     "got": True, "passed": True, "error": None}]),
        )
        return defs

    @pytest.mark.asyncio
    async def test_hash_estavel_para_mesmo_conteudo(self, monkeypatch):
        defs = self._wire_defs(monkeypatch, self._subgraph())
        r1 = await defs.evaluate_pipeline_test_phrases("p1")
        r2 = await defs.evaluate_pipeline_test_phrases("p1")
        assert r1["phrases_hash"] and r1["phrases_hash"] == r2["phrases_hash"]
        assert r1["evaluated"] == 1 and r1["passed"] == 1

    @pytest.mark.asyncio
    async def test_expr_diferente_muda_o_hash(self, monkeypatch):
        defs = self._wire_defs(monkeypatch, self._subgraph())
        r1 = await defs.evaluate_pipeline_test_phrases("p1")
        monkeypatch.setattr(
            defs, "_build_subgraph", _async(self._subgraph(expr="has_image"))
        )
        r2 = await defs.evaluate_pipeline_test_phrases("p1")
        assert r1["phrases_hash"] != r2["phrases_hash"]

    def _wire_defs_echo(self, monkeypatch, subgraph):
        """Mock-eco: devolve como resultado exatamente as frases avaliáveis
        (pula texto vazio, como o engine real faz em evaluate_test_phrases)."""
        import app.catalog.pipeline_defs as defs
        monkeypatch.setattr(defs, "_build_subgraph", _async(subgraph))

        async def _echo(source_id, expr, phrases):
            return [
                {"text": p["text"], "where": p.get("where", "input"),
                 "expect": p.get("expect", True), "got": True,
                 "passed": True, "error": ""}
                for p in phrases if str(p.get("text") or "").strip()
            ]

        monkeypatch.setattr(
            "app.agents.engine.evaluate_test_phrases_for_edge", _echo
        )
        return defs

    def _subgraph_with(self, phrases):
        sg = self._subgraph()
        sg["edges"][0]["config"]["test_phrases"] = phrases
        return sg

    @pytest.mark.asyncio
    async def test_reordenar_frases_nao_muda_o_hash(self, monkeypatch):
        """Hash é do CONJUNTO avaliado: reordenar a lista (edição no-op no
        modal) não pode quebrar a comparabilidade entre runs."""
        a = {"text": "quero um plano", "where": "input", "expect": True}
        b = {"text": "urgente!", "where": "input", "expect": False}
        defs = self._wire_defs_echo(monkeypatch, self._subgraph_with([a, b]))
        r1 = await defs.evaluate_pipeline_test_phrases("p1")
        monkeypatch.setattr(
            defs, "_build_subgraph", _async(self._subgraph_with([b, a]))
        )
        r2 = await defs.evaluate_pipeline_test_phrases("p1")
        assert r1["phrases_hash"] == r2["phrases_hash"]
        assert r1["evaluated"] == r2["evaluated"] == 2

    @pytest.mark.asyncio
    async def test_so_frases_de_texto_vazio_nao_gera_hash(self, monkeypatch):
        """Frase de texto vazio é PULADA pelo avaliador: nada avaliado →
        hash None (invariante hash ⇔ evaluated > 0; '0 = avaliou, sem
        frases' na migração implica hash NULL)."""
        empty = {"text": "   ", "where": "input", "expect": True}
        defs = self._wire_defs_echo(monkeypatch, self._subgraph_with([empty]))
        r = await defs.evaluate_pipeline_test_phrases("p1")
        assert r == {
            "evaluated": 0, "passed": 0, "failing": [], "phrases_hash": None,
        }
