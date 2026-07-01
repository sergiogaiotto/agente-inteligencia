"""Postura B — roteamento DETERMINÍSTICO por `inputs.X` (args selados x-uso:param).

Os args selados (envelope param) ficam disponíveis como `inputs.<campo>` nas regras
condicionais das arestas (`config.expr`). Assim uma aresta pode ramificar por VALOR
de arg — sem LLM no roteamento — resolvendo a cadeia inteira de forma determinística.
Campo ausente → ChainableUndefined (falsy), nunca crasha.
"""
import json

import pytest

import app.core.database as db
import app.agents.engine as engine
from app.agents.engine import _build_conditional_context, _eval_conditional, CONDITIONAL_VARS_META


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


class TestConditionalContext:
    def test_inputs_available_in_context(self):
        ctx = _build_conditional_context(inputs={"tier": "gold"})
        assert ctx["inputs"] == {"tier": "gold"}

    def test_inputs_none_is_empty_dict(self):
        assert _build_conditional_context()["inputs"] == {}

    def test_eval_routes_by_input_value(self):
        gold = _build_conditional_context(inputs={"tier": "gold"})
        silver = _build_conditional_context(inputs={"tier": "silver"})
        assert _eval_conditional("inputs.tier == 'gold'", gold) is True
        assert _eval_conditional("inputs.tier == 'gold'", silver) is False

    def test_eval_numeric_input(self):
        ctx = _build_conditional_context(inputs={"limite": 5000})
        assert _eval_conditional("inputs.limite > 1000", ctx) is True
        assert _eval_conditional("inputs.limite > 9000", ctx) is False

    def test_eval_missing_key_is_falsy(self):
        # campo ausente → ChainableUndefined (falsy), sem crash
        assert _eval_conditional("inputs.tier == 'gold'", _build_conditional_context()) is False


class TestVarsMeta:
    def test_inputs_in_conditional_vars_meta(self):
        assert "inputs" in {v["name"] for v in CONDITIONAL_VARS_META}

    def test_guardrail_accepts_inputs_member_access(self):
        # o tradutor NL→Jinja (suggest-conditional) valida vars contra o vocabulário
        # canônico; `inputs.tier` usa a var `inputs` → precisa passar no guardrail.
        from app.agents.conditional_suggest import validate_conditional_expression
        canonical = {v["name"] for v in CONDITIONAL_VARS_META}
        res = validate_conditional_expression("inputs.tier == 'gold'", canonical)
        assert res["valid"] is True, res


class TestShouldSkipThreadsInputs:
    @pytest.mark.asyncio
    async def test_conditional_edge_routes_on_inputs(self, monkeypatch):
        # aresta condicional cuja regra é `inputs.tier == 'gold'`. Sem bloco de roteador
        # no output → a expr decide. gold roda, silver skipa — sem LLM.
        conn = {"source_agent_id": "src", "target_agent_id": "tgt",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "inputs.tier == 'gold'"})}
        monkeypatch.setattr(db.mesh_repo, "find_all", _async([conn]))

        skip_gold = await engine._should_skip_conditional(
            source_id="src", target_id="tgt", last_output="", last_final_state="",
            target_name="Tgt", inputs={"tier": "gold"})
        assert skip_gold is False   # gold → NÃO skipa (roda)

        skip_silver = await engine._should_skip_conditional(
            source_id="src", target_id="tgt", last_output="", last_final_state="",
            target_name="Tgt", inputs={"tier": "silver"})
        assert skip_silver is True   # silver → skipa

    @pytest.mark.asyncio
    async def test_no_inputs_falls_back_to_skip(self, monkeypatch):
        # sem inputs, a regra por valor não casa → skipa (fail-safe do gate).
        conn = {"source_agent_id": "src", "target_agent_id": "tgt",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "inputs.tier == 'gold'"})}
        monkeypatch.setattr(db.mesh_repo, "find_all", _async([conn]))
        skip = await engine._should_skip_conditional(
            source_id="src", target_id="tgt", last_output="", last_final_state="",
            target_name="Tgt", inputs=None)
        assert skip is True
