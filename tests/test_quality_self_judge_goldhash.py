"""Confiar no sinal (33.9.0) — Q5 anti-auto-preferência + Q6 gold-hash.

Q5: o Verifier marca self_judged quando o MESMO modelo gerou o draft E o julgou.
Q6: hash imutável do case-set do gold → comparabilidade pega 'mesmo rótulo,
conteúdo mudou' (o rótulo texto-livre não pegava).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _force_verifier_v2(monkeypatch):
    """verify() cai no _LegacyVerifier quando VERIFIER_V2_ENABLED=False (default no
    ambiente hermético). O path v2 (que seta generator_model/self_judged) só roda
    com o toggle ON — força aqui p/ o teste ser DETERMINÍSTICO (não depender do
    .env). Sem isto: passa local (que tem o toggle) e falha no CI."""
    from app.core import config as _config
    monkeypatch.setenv("VERIFIER_V2_ENABLED", "true")
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


class _FakeJudge:
    async def evaluate(self, **kw):
        return {
            "dimensions": {}, "unsupported_claims": [],
            "model": "gpt-4o", "judge_tokens": 10, "judge_cost_usd": 0.001,
        }


class TestSelfJudged:
    @pytest.mark.asyncio
    async def test_mesmo_modelo_gera_e_julga_marca_self_judged(self, monkeypatch, _force_verifier_v2):
        from app.verifier import multi_dim_judge
        from app.verifier.runtime import Verifier

        monkeypatch.setattr(multi_dim_judge, "MultiDimJudge", lambda: _FakeJudge())
        v = Verifier()
        r = await v.verify(
            draft="algo", evidences=[], profile="rigorous",
            llm_model="gpt-4o", persist=False,
        )
        assert r.generator_model == "gpt-4o"
        assert r.self_judged is True

    @pytest.mark.asyncio
    async def test_modelo_diferente_nao_e_self_judged(self, monkeypatch, _force_verifier_v2):
        from app.verifier import multi_dim_judge
        from app.verifier.runtime import Verifier

        monkeypatch.setattr(multi_dim_judge, "MultiDimJudge", lambda: _FakeJudge())
        v = Verifier()
        r = await v.verify(
            draft="algo", evidences=[], profile="rigorous",
            llm_model="azure-o1", persist=False,  # juiz=gpt-4o ≠ gerador
        )
        assert r.self_judged is False

    @pytest.mark.asyncio
    async def test_sem_generator_model_nao_e_self_judged(self, monkeypatch, _force_verifier_v2):
        from app.verifier import multi_dim_judge
        from app.verifier.runtime import Verifier

        monkeypatch.setattr(multi_dim_judge, "MultiDimJudge", lambda: _FakeJudge())
        v = Verifier()
        r = await v.verify(draft="algo", evidences=[], profile="rigorous", persist=False)
        assert r.self_judged is False  # llm_model=None → sem comparação


class TestGoldHash:
    def test_mesmo_conteudo_mesmo_hash_e_ordem_independente(self):
        from app.harness.evaluator import _compute_gold_hash
        a = [{"id": "1", "input_text": "q", "expected_output": "a"}]
        b = [{"id": "1", "input_text": "q", "expected_output": "a"}]
        assert _compute_gold_hash(a) == _compute_gold_hash(b)

        d = [{"id": "2", "input_text": "x", "expected_output": "y"},
             {"id": "1", "input_text": "q", "expected_output": "a"}]
        e = [{"id": "1", "input_text": "q", "expected_output": "a"},
             {"id": "2", "input_text": "x", "expected_output": "y"}]
        assert _compute_gold_hash(d) == _compute_gold_hash(e)  # ordem não importa

    def test_conteudo_editado_muda_o_hash(self):
        from app.harness.evaluator import _compute_gold_hash
        base = [{"id": "1", "input_text": "q", "expected_output": "a"}]
        edited = [{"id": "1", "input_text": "q", "expected_output": "OUTRO"}]
        assert _compute_gold_hash(base) != _compute_gold_hash(edited)
