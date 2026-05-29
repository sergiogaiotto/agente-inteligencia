"""RAGAS metrics — decomposição heurística do Score de confiança em 4 métricas
estilo RAGAS, sem chamada LLM extra.

User reportou (2026-05-28): Score de confiança aparece como número único
(ex: 7%) sem decomposição. Pediu pra ver as métricas que compõem o cálculo
logo abaixo. Optou pela abordagem heurística (zero LLM extra) — context_*
sempre disponíveis, faithfulness/answer_relevancy vêm do MultiDimJudge
quando o Verifier v2 rodou.

Regras de cobertura:
- context_relevancy: empty / sem score / single / múltiplos
- context_precision: perfeitamente ordenado / desordenado / nada acima do
  threshold / threshold custom da skill
- faithfulness e answer_relevancy: com judge / sem judge (fast) / judge com
  null (sem evidências)
- _meta: contagens corretas
- tolerância a shapes: dict com relevance_score vs objeto com atributo
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.verifier.ragas_metrics import compute_heuristic_ragas


# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────

@dataclass
class _EvidenceObj:
    """Reproduz o shape com atributo (ctx.evidences usa isso em alguns paths)."""
    relevance_score: float
    source_name: str = "x"
    snippet_text: str = "y"


def _ev(scores: list[float], as_dict: bool = True):
    """Constrói lista de evidências na ordem fornecida."""
    if as_dict:
        return [{"relevance_score": s, "source_name": "x"} for s in scores]
    return [_EvidenceObj(relevance_score=s) for s in scores]


# ───────────────────────────────────────────────────────────────
# context_relevancy
# ───────────────────────────────────────────────────────────────

class TestContextRelevancy:
    def test_empty_returns_zero(self):
        """Sem evidências = score 0 com source heuristic e reason explícita."""
        r = compute_heuristic_ragas([])["context_relevancy"]
        assert r["score"] == 0.0
        assert r["source"] == "heuristic"
        assert "Sem evidências" in r["reason"]

    def test_single_evidence_returns_its_score(self):
        r = compute_heuristic_ragas(_ev([0.42]))["context_relevancy"]
        assert r["score"] == 0.42
        assert r["source"] == "heuristic"

    def test_multiple_returns_arithmetic_mean(self):
        """Bate com o evidence_score legado: avg(relevance_score)."""
        r = compute_heuristic_ragas(_ev([1.0, 0.5, 0.0]))["context_relevancy"]
        assert r["score"] == 0.5

    def test_tolerates_object_shape(self):
        """ctx.evidences chega como objetos em alguns paths — função aceita."""
        r = compute_heuristic_ragas(_ev([0.4, 0.6], as_dict=False))["context_relevancy"]
        assert r["score"] == 0.5

    def test_evidences_without_score_returns_zero(self):
        """Retriever broken (sem rerank) devolve evidências sem score — não estoura."""
        r = compute_heuristic_ragas([{"source_name": "x"}, {"source_name": "y"}])
        assert r["context_relevancy"]["score"] == 0.0
        assert "sem `relevance_score`" in r["context_relevancy"]["reason"]


# ───────────────────────────────────────────────────────────────
# context_precision
# ───────────────────────────────────────────────────────────────

class TestContextPrecision:
    def test_perfect_order_all_relevant_is_one(self):
        """[0.9, 0.8, 0.7] com threshold 0.3 → todos relevantes, ordem ideal."""
        r = compute_heuristic_ragas(_ev([0.9, 0.8, 0.7]))["context_precision"]
        assert r["score"] == 1.0

    def test_worst_order_relevant_at_bottom(self):
        """[0.1, 0.1, 0.9] com threshold 0.3 → só pos 3 relevante.
        precision@3 = 1/3 ≈ 0.333"""
        r = compute_heuristic_ragas(_ev([0.1, 0.1, 0.9]))["context_precision"]
        assert r["score"] == pytest.approx(0.3333, abs=1e-3)

    def test_mixed_order(self):
        """[0.8, 0.1, 0.4] threshold 0.3:
        - pos 1 (0.8) relevante: precision@1 = 1/1 = 1.0
        - pos 2 (0.1) não
        - pos 3 (0.4) relevante: precision@3 = 2/3 ≈ 0.667
        avg = (1.0 + 0.667) / 2 ≈ 0.833"""
        r = compute_heuristic_ragas(_ev([0.8, 0.1, 0.4]))["context_precision"]
        assert r["score"] == pytest.approx(0.8333, abs=1e-3)

    def test_nothing_above_threshold_returns_zero(self):
        """Bug do user (Tavily achou só doc fraco): 7% único score.
        Threshold 0.3 default → 0 relevantes → score 0."""
        r = compute_heuristic_ragas(_ev([0.07]))["context_precision"]
        assert r["score"] == 0.0
        assert "Nenhum chunk acima" in r["reason"]

    def test_custom_threshold_from_skill(self):
        """Skill com Evidence Policy.min_relevance=0.05 → 0.07 vira relevante."""
        r = compute_heuristic_ragas(_ev([0.07]), threshold=0.05)["context_precision"]
        assert r["score"] == 1.0

    def test_empty_returns_zero(self):
        r = compute_heuristic_ragas([])["context_precision"]
        assert r["score"] == 0.0


# ───────────────────────────────────────────────────────────────
# faithfulness + answer_relevancy (lidos do MultiDimJudge)
# ───────────────────────────────────────────────────────────────

class TestJudgeBackedMetrics:
    def test_no_verification_marks_unavailable(self):
        """Fast profile / Verifier v2 off → score None, source 'unavailable',
        reason com hint pra operador ativar."""
        r = compute_heuristic_ragas(_ev([0.5]), verification=None)
        for key in ("faithfulness", "answer_relevancy"):
            m = r[key]
            assert m["score"] is None
            assert m["source"] == "unavailable"
            assert "VERIFIER_V2_ENABLED" in m["reason"] or "standard" in m["reason"].lower()

    def test_judge_dimensions_normalized_to_unit_interval(self):
        """Judge devolve score 0-5; expomos 0-1 pra paridade com context_*."""
        verification = {
            "dimensions": {
                "factuality":   {"score": 4, "reason": "tudo suportado"},
                "completeness": {"score": 3, "reason": "ok parcial"},
            },
        }
        r = compute_heuristic_ragas(_ev([0.5]), verification=verification)
        assert r["faithfulness"]["score"] == 0.8
        assert r["faithfulness"]["source"] == "judge"
        assert r["faithfulness"]["reason"] == "tudo suportado"
        assert r["answer_relevancy"]["score"] == 0.6

    def test_judge_null_score_when_no_evidence(self):
        """Judge devolve factuality=null quando 'evidências ausentes' (do prompt
        do MultiDimJudge). Nossa camada propaga score=None mas source='judge'."""
        verification = {
            "dimensions": {
                "factuality":   {"score": None, "reason": "evidências ausentes"},
                "completeness": {"score": 4,    "reason": "ok"},
            },
        }
        r = compute_heuristic_ragas(_ev([]), verification=verification)
        assert r["faithfulness"]["score"] is None
        assert r["faithfulness"]["source"] == "judge"
        assert r["answer_relevancy"]["score"] == 0.8


# ───────────────────────────────────────────────────────────────
# _meta + integração
# ───────────────────────────────────────────────────────────────

class TestMetaAndShape:
    def test_meta_has_threshold_and_count(self):
        """UI usa _meta pra mostrar 'threshold 0.30' no canto e auditar
        quantas evidências entraram no cálculo."""
        r = compute_heuristic_ragas(_ev([0.5, 0.3]), threshold=0.25)
        meta = r["_meta"]
        assert meta["threshold_applied"] == 0.25
        assert meta["evidence_count"] == 2
        assert meta["has_judge"] is False

    def test_has_judge_true_when_verification_present(self):
        r = compute_heuristic_ragas([], verification={"dimensions": {}})
        assert r["_meta"]["has_judge"] is True

    def test_returns_all_four_metrics_always(self):
        """Independente de inputs, devolve as 4 chaves — UI usa shape estável."""
        r = compute_heuristic_ragas([])
        for key in ("context_relevancy", "context_precision",
                    "faithfulness", "answer_relevancy"):
            assert key in r
            assert "score" in r[key]
            assert "source" in r[key]
            assert "reason" in r[key]


class TestUserBugScenario:
    """Regressão do caso real reportado (screenshot _pesquisa cloud agent Uber):
    1 evidência ('Scripts Rentab e Churn') com relevance_score ≈ 0.07.
    Threshold default 0.3. Expectativa: ambas context_* baixas, contando a
    história 'retriever puxou lixo'.
    """

    def test_low_score_retrieves_irrelevant(self):
        r = compute_heuristic_ragas(_ev([0.07]), threshold=0.3)
        # context_relevancy = 0.07 (média de 1 valor)
        assert r["context_relevancy"]["score"] == pytest.approx(0.07, abs=1e-3)
        # context_precision = 0 (nada acima do threshold)
        assert r["context_precision"]["score"] == 0.0
        # Sem judge (fast profile) → answer/faithfulness indisponíveis
        assert r["faithfulness"]["score"] is None
        assert r["answer_relevancy"]["source"] == "unavailable"

    def test_decomposition_shows_retriever_problem_not_metric_problem(self):
        """Confirma que a decomposição comunica corretamente o problema —
        ambas context_* baixas = retriever, não cálculo do score."""
        r = compute_heuristic_ragas(_ev([0.07]))
        cr = r["context_relevancy"]["score"]
        cp = r["context_precision"]["score"]
        # Sinal pra operador: relevance baixo + precision zero = retriever
        assert cr < 0.3 and cp == 0.0


class TestEngineTraceIntegration:
    """Smoke do plug no _build_result: ragas_metrics aparece em result['trace']
    e tem shape correto. Vai pegar regressão de naming/path."""

    @pytest.mark.asyncio
    async def test_ragas_metrics_present_in_trace_dict(self):
        from unittest.mock import AsyncMock, patch
        from app.agents.engine import _build_result
        from app.agents.state_machine import InteractionContext, State

        ctx = InteractionContext(agent_id="x", journey="t", channel="api")
        ctx.interaction_id = "test-int"
        ctx.evidences = [{"relevance_score": 0.5, "source_name": "src1"}]
        ctx.evidence_score = 0.5
        ctx.current_state = State.RECOMMEND
        ctx.transition_log = []
        ctx.metadata = {"evidence_min_relevance": 0.3, "evidence_min_relevance_source": "default"}
        ctx.final_output = "ok"

        with patch("app.core.database.tool_calls_repo") as mock_tc, \
             patch("app.core.database.binding_executions_repo") as mock_be:
            mock_tc.find_all = AsyncMock(return_value=[])
            mock_be.find_all = AsyncMock(return_value=[])
            result = await _build_result(
                ctx, start_time=0.0,
                agent={"name": "a", "kind": "subagent", "model": "m", "llm_provider": "openai"},
                skill_data={},
                mcp_tools_detail=[],
            )
        trace = result.get("trace") or {}
        ragas = trace.get("ragas_metrics")
        assert ragas is not None, "ragas_metrics ausente do trace — UI fica sem dado"
        # Shape esperado pela UI Alpine.js
        for key in ("context_relevancy", "context_precision",
                    "faithfulness", "answer_relevancy", "_meta"):
            assert key in ragas

    @pytest.mark.asyncio
    async def test_threshold_passed_from_metadata(self):
        """Engine setou metadata['evidence_min_relevance'] = 0.15 (custom da
        skill). RAGAS metrics tem que usar esse threshold, não o default 0.3.
        """
        from unittest.mock import AsyncMock, patch
        from app.agents.engine import _build_result
        from app.agents.state_machine import InteractionContext, State

        ctx = InteractionContext(agent_id="x", journey="t", channel="api")
        ctx.interaction_id = "test-int-2"
        # Score 0.2 — fica acima de 0.15 (custom) mas abaixo de 0.3 (default).
        # Se threshold foi propagado certo, precision = 1.0; se não, = 0.0.
        ctx.evidences = [{"relevance_score": 0.2, "source_name": "src1"}]
        ctx.evidence_score = 0.2
        ctx.current_state = State.RECOMMEND
        ctx.transition_log = []
        ctx.metadata = {"evidence_min_relevance": 0.15, "evidence_min_relevance_source": "skill"}
        ctx.final_output = "ok"

        with patch("app.core.database.tool_calls_repo") as mock_tc, \
             patch("app.core.database.binding_executions_repo") as mock_be:
            mock_tc.find_all = AsyncMock(return_value=[])
            mock_be.find_all = AsyncMock(return_value=[])
            result = await _build_result(
                ctx, start_time=0.0,
                agent={"name": "a", "kind": "subagent", "model": "m", "llm_provider": "openai"},
                skill_data={},
                mcp_tools_detail=[],
            )
        ragas = result["trace"]["ragas_metrics"]
        assert ragas["_meta"]["threshold_applied"] == 0.15
        assert ragas["context_precision"]["score"] == 1.0
