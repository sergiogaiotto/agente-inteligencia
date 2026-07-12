"""Custo não subconta mais o input em turnos multi-chamada (TCO auditável).

Em turnos com várias chamadas LLM (reflexão/tool-loop) o provider cobra o prompt
a CADA chamada. O engine coletava `input_billed_sum` (soma entre chamadas) mas o
custo usava só `tokens['input']` (a ÚLTIMA chamada) — subcontando. Agora custo e
tokens_used usam a soma billed, com fallback a 'input' p/ single-call/traces antigos.
"""
from pathlib import Path

import pytest

from app.core import llm_pricing

# azure/gpt-4o default: 0.0025/1k in, 0.01/1k out
IN_PRICE, OUT_PRICE = 0.0025, 0.01


@pytest.fixture(autouse=True)
def _reset_overrides():
    llm_pricing.set_pricing_overrides({})
    yield
    llm_pricing.set_pricing_overrides({})


def _trace(**tok):
    return {"trace": {"agent_provider": "azure", "agent_model": "gpt-4o", "tokens": tok}}


def test_step_cost_usa_input_billed_sum():
    from app.agents.engine import _step_cost_and_tokens
    # 3 chamadas: última input=100, mas billed (soma)=300; output=50
    result = _trace(input=100, output=50, total=150, input_billed_sum=300, total_billed=350)
    cost, tokens = _step_cost_and_tokens(result, {})
    assert cost == pytest.approx(round(300 / 1000 * IN_PRICE + 50 / 1000 * OUT_PRICE, 6))
    assert tokens == 350                       # total_billed, não total (150)


def test_step_cost_fallback_single_call():
    """Sem input_billed_sum (single-call / trace antigo) → usa 'input' (retrocompat)."""
    from app.agents.engine import _step_cost_and_tokens
    result = _trace(input=100, output=50, total=150)
    cost, tokens = _step_cost_and_tokens(result, {})
    assert cost == pytest.approx(round(100 / 1000 * IN_PRICE + 50 / 1000 * OUT_PRICE, 6))
    assert tokens == 150


def test_budget_cost_usa_input_billed_sum():
    from app.core.api_key_budget import cost_and_tokens_from_result
    result = _trace(input=100, output=50, total=150, input_billed_sum=300, total_billed=350)
    cost, tokens = cost_and_tokens_from_result(result)
    assert cost == pytest.approx(round(300 / 1000 * IN_PRICE + 50 / 1000 * OUT_PRICE, 6))
    assert tokens == 350


def test_billed_maior_que_last_quando_multi_chamada():
    """Regressão do bug: o custo billed deve ser ESTRITAMENTE maior que o antigo
    (só-última) quando houve multi-chamada."""
    from app.agents.engine import _step_cost_and_tokens
    billed, _ = _step_cost_and_tokens(
        _trace(input=100, output=50, total=150, input_billed_sum=300, total_billed=350), {})
    last_only, _ = _step_cost_and_tokens(_trace(input=100, output=50, total=150), {})
    assert billed > last_only


def test_todos_os_tres_caminhos_de_custo_usam_billed():
    """engine (_step_cost_and_tokens), budget (cost_and_tokens_from_result) e o
    executor de catálogo (_invoke_step) usam input_billed_sum p/ o custo."""
    assert "tok.get(\"input_billed_sum\")" in Path("app/agents/engine.py").read_text(encoding="utf-8")
    assert "tok.get(\"input_billed_sum\")" in Path("app/core/api_key_budget.py").read_text(encoding="utf-8")
    assert "tokens.get(\"input_billed_sum\")" in Path("app/catalog/executor.py").read_text(encoding="utf-8")
