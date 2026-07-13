"""Custo REAL do juiz instrumentado (TCO auditável).

Antes, o LLM-as-Judge rodava a cada resposta mas o custo era invisível
(verifications só guardava duration_ms) — o TCO o ESTIMAVA. Agora o juiz captura
tokens do provider × preço do modelo usado, persiste em verifications e expõe no
snapshot por step, para o Cockpit somar um custo de juiz MEDIDO.
"""
from pathlib import Path

import pytest

from app.core import llm_pricing


@pytest.mark.asyncio
async def test_judge_captura_tokens_e_custo(monkeypatch):
    from app.verifier import multi_dim_judge as mdj

    content = (
        '{"factuality":{"score":5,"reason":"ok"},'
        '"completeness":{"score":5,"reason":"ok"},'
        '"tone_adherence":{"score":5,"reason":"ok"},'
        '"safety":{"score":1,"reason":"ok"},'
        '"unsupported_claims":[]}'
    )

    async def fake_gen(messages, provider, model, *, purpose, prov_kwargs=None, gen_kwargs=None):
        return (
            {"content": content, "model": "azure/gpt-4o",
             "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
            "azure", "gpt-4o",
        )

    async def fake_resolve(task):
        return ("azure", "gpt-4o")

    monkeypatch.setattr(mdj, "generate_with_hosted_fallback", fake_gen)
    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)
    llm_pricing.set_pricing_overrides({})  # usa o default azure/gpt-4o

    j = await mdj.MultiDimJudge().evaluate(draft="rascunho", evidences=[], user_question="q")

    assert j["judge_tokens"] == 150
    # azure/gpt-4o default: 0.0025/1k in, 0.01/1k out
    esperado = round(100 / 1000 * 0.0025 + 50 / 1000 * 0.01, 6)
    assert j["judge_cost_usd"] == pytest.approx(esperado)


@pytest.mark.asyncio
async def test_judge_self_hosted_custo_zero_honesto(monkeypatch):
    """Modelo self-hosted (gpt-oss) → custo 0, mas MEDIDO (o juiz rodou)."""
    from app.verifier import multi_dim_judge as mdj

    content = '{"factuality":{"score":4,"reason":"x"},"completeness":{"score":4,"reason":"x"},"tone_adherence":{"score":5,"reason":"x"},"safety":{"score":1,"reason":"x"},"unsupported_claims":[]}'

    async def fake_gen(messages, provider, model, *, purpose, prov_kwargs=None, gen_kwargs=None):
        return ({"content": content, "model": "gpt-oss-120b/gpt-oss-120b",
                 "usage": {"prompt_tokens": 200, "completion_tokens": 80}},
                "gpt-oss-120b", "gpt-oss-120b")

    async def fake_resolve(task):
        return ("gpt-oss-120b", "gpt-oss-120b")

    monkeypatch.setattr(mdj, "generate_with_hosted_fallback", fake_gen)
    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", fake_resolve)
    llm_pricing.set_pricing_overrides({})

    j = await mdj.MultiDimJudge().evaluate(draft="d", evidences=[], user_question="q")
    assert j["judge_tokens"] == 280
    assert j["judge_cost_usd"] == 0.0     # self-hosted priced 0 (honesto)


def test_verification_result_e_persist_carregam_custo():
    src = Path("app/verifier/runtime.py").read_text(encoding="utf-8")
    # campos no dataclass + capturados do juiz + persistidos
    assert "judge_tokens: int = 0" in src and "judge_cost_usd: float = 0.0" in src
    assert 'judge_tokens = int(j.get("judge_tokens") or 0)' in src
    assert "judge_tokens, judge_cost_usd" in src           # colunas no INSERT (+generator_model/self_judged em 33.9.0)


def test_schema_e_migracao_das_colunas():
    src = Path("app/core/database.py").read_text(encoding="utf-8")
    assert "judge_tokens INTEGER DEFAULT 0" in src          # CREATE (DB novo)
    assert "ADD COLUMN IF NOT EXISTS judge_tokens INTEGER DEFAULT 0" in src   # migração
    assert "ADD COLUMN IF NOT EXISTS judge_cost_usd REAL DEFAULT 0" in src


def test_serialize_verification_expoe_custo_do_juiz():
    src = Path("app/agents/engine.py").read_text(encoding="utf-8")
    assert '"judge_cost_usd": float(getattr(v, "judge_cost_usd"' in src
    assert '"judge_tokens": int(getattr(v, "judge_tokens"' in src


def test_cockpit_usa_custo_real_do_juiz_no_tco():
    src = Path("app/templates/pages/mesh_playground.html").read_text(encoding="utf-8")
    assert "get ckJudgeMeasured()" in src and "_judgeCostUsdOf(" in src
    assert "judge_cost_usd" in src
    # a linha do TCO usa o medido quando o juiz rodou; senão a estimativa
    assert "this.ckJudgeMeasured ?" in src
