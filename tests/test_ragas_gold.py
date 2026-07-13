"""33.12.0 — RAGAS com gabarito (context_recall + answer_correctness).

As 2 métricas que exigem ground truth, via LLM-judge, gated default-OFF. Cobre
compute_gold_ragas (com/sem contexto, sem gabarito, clamp, parse markdown,
degradação em falha), o contrato da setting e o gate no run_evaluation. O juiz
LLM é mockado — nenhuma chamada real.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.verifier import ragas_metrics


@pytest.fixture
def patch_llm(monkeypatch):
    """Mocka resolve_llm_for_task + generate_with_hosted_fallback + compute_cost.
    holder['content'] controla o que o "juiz" devolve; calls conta chamadas."""
    calls = {"gen": 0}
    holder = {"content": '{"score": 0.8, "reason": "ok"}'}

    async def fake_resolve(task, has_image=False):
        return ("azure", "gpt-4o")

    async def fake_gen(messages, provider, model, *, purpose, prov_kwargs=None, gen_kwargs=None):
        calls["gen"] += 1
        calls[f"purpose_{calls['gen']}"] = purpose
        return (
            {"content": holder["content"],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20},
             "model": "azure/gpt-4o"},
            "azure", "gpt-4o",
        )

    def fake_cost(provider, model, in_tok=0, out_tok=0):
        return 0.001

    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)
    monkeypatch.setattr("app.core.llm_providers.generate_with_hosted_fallback", fake_gen)
    monkeypatch.setattr("app.core.llm_pricing.compute_cost", fake_cost)
    return calls, holder


class TestComputeGoldRagas:
    @pytest.mark.asyncio
    async def test_ambas_com_contexto(self, patch_llm):
        calls, _ = patch_llm
        out = await ragas_metrics.compute_gold_ragas(
            answer="resposta", ground_truth="gabarito", contexts=["ctx1", "ctx2"],
        )
        assert out["answer_correctness"]["score"] == 0.8
        assert out["answer_correctness"]["source"] == "judge"
        assert out["context_recall"]["score"] == 0.8
        assert calls["gen"] == 2                       # 2 chamadas LLM
        assert out["_meta"]["cost_usd"] == pytest.approx(0.002)  # 2 × 0.001
        assert out["_meta"]["tokens"] == 240
        assert out["_meta"]["has_contexts"] is True

    @pytest.mark.asyncio
    async def test_sem_contexto_so_answer_correctness(self, patch_llm):
        calls, _ = patch_llm
        out = await ragas_metrics.compute_gold_ragas(answer="r", ground_truth="g", contexts=[])
        assert out["answer_correctness"]["score"] == 0.8
        assert out["context_recall"]["score"] is None
        assert out["context_recall"]["source"] == "unavailable"
        assert calls["gen"] == 1                       # context_recall não chama LLM sem contexto

    @pytest.mark.asyncio
    async def test_sem_gabarito_zero_chamadas(self, patch_llm):
        calls, _ = patch_llm
        out = await ragas_metrics.compute_gold_ragas(answer="r", ground_truth="   ", contexts=["c"])
        assert out["answer_correctness"]["score"] is None
        assert out["context_recall"]["score"] is None
        assert calls["gen"] == 0                        # curto-circuito sem gabarito
        assert out["_meta"]["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_clamp_para_0_1(self, patch_llm):
        _, holder = patch_llm
        holder["content"] = '{"score": 1.7, "reason": "fora do range"}'
        out = await ragas_metrics.compute_gold_ragas(answer="r", ground_truth="g", contexts=[])
        assert out["answer_correctness"]["score"] == 1.0   # clamp [0..1]

    @pytest.mark.asyncio
    async def test_parse_json_em_markdown(self, patch_llm):
        _, holder = patch_llm
        holder["content"] = '```json\n{"score": 0.5, "reason": "y"}\n```'
        out = await ragas_metrics.compute_gold_ragas(answer="r", ground_truth="g", contexts=[])
        assert out["answer_correctness"]["score"] == 0.5

    @pytest.mark.asyncio
    async def test_content_lixo_degrada(self, patch_llm):
        _, holder = patch_llm
        holder["content"] = "não é json nenhum"
        out = await ragas_metrics.compute_gold_ragas(answer="r", ground_truth="g", contexts=[])
        assert out["answer_correctness"]["score"] is None
        assert out["answer_correctness"]["source"] == "judge"

    @pytest.mark.asyncio
    async def test_llm_excecao_nao_derruba(self, monkeypatch):
        async def fake_resolve(task, has_image=False):
            return ("azure", "gpt-4o")

        async def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)
        monkeypatch.setattr("app.core.llm_providers.generate_with_hosted_fallback", boom)
        out = await ragas_metrics.compute_gold_ragas(answer="r", ground_truth="g", contexts=["c"])
        assert out["answer_correctness"]["score"] is None
        assert out["answer_correctness"]["source"] == "unavailable"


class TestSetting:
    def test_default_off_e_nao_selada(self):
        from app.core.config import (
            Settings, PARAMETER_UI_KEYS, _UI_TO_ENV_MAP, _NON_MODEL_UI_KEYS, _SEALED_ENV_VARS,
        )
        assert Settings.model_fields["ragas_ground_truth_enabled"].default is False
        assert _UI_TO_ENV_MAP["ragas_ground_truth_enabled"] == "RAGAS_GROUND_TRUTH_ENABLED"
        assert "ragas_ground_truth_enabled" in PARAMETER_UI_KEYS
        assert "ragas_ground_truth_enabled" in _NON_MODEL_UI_KEYS   # comportamento, não credencial
        assert "RAGAS_GROUND_TRUTH_ENABLED" not in _SEALED_ENV_VARS  # .env vale de fallback

    def test_construtor_forca_true(self):
        from app.core.config import Settings
        assert Settings(ragas_ground_truth_enabled=True).ragas_ground_truth_enabled is True


class TestHarnessGate:
    def test_run_evaluation_gateia_e_chama(self):
        src = Path("app/harness/evaluator.py").read_text(encoding="utf-8")
        assert "use_ragas_gt = settings.ragas_ground_truth_enabled" in src
        assert "if use_ragas_gt:" in src
        assert "compute_gold_ragas(" in src
        # agrega as médias run-level no dimension_breakdown
        assert '"avg_context_recall"' in src
        assert '"avg_answer_correctness"' in src
