"""Prova de ponta (execute_pipeline REAL, LLM mockado) do fluxo de args na cadeia.

1. FASE 3 — os args `llm` dobrados na entrada JÁ ALCANÇAM os agentes de IA downstream,
   via "## Solicitação original" (o `user_input` não é reatribuído no loop). Ou seja:
   não há nada a construir — este teste TRAVA o comportamento como regressão.
2. POSTURA B ponta-a-ponta — os `sealed_inputs` chegam ao GATE condicional pela cadeia
   REAL: `inputs.tier == 'gold'` roteia por VALOR, sem LLM.
"""
import json

import pytest


def _agent(aid, name):
    return {"id": aid, "name": name, "status": "active", "kind": "subagent",
            "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "prompt real"}


def _conn(src, tgt, *, ctype="sequential", expr=None):
    cfg = json.dumps({"expr": expr}) if expr is not None else "{}"
    return {"source_agent_id": src, "target_agent_id": tgt, "connection_type": ctype, "config": cfg}


def _patch_topology(monkeypatch, by_source):
    async def fake_find_all(source_agent_id=None, limit=20, **_):
        return list(by_source.get(source_agent_id, []))
    monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)


def _patch_agents(monkeypatch, agents):
    async def fake_find_by_id(aid):
        return agents.get(aid)
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)


def _patch_executions(monkeypatch):
    invoked = []

    async def fake_exec(*, agent_id, user_input, channel="api", attachments=None,
                        pipeline_context=None, session_id=None, sealed_inputs=None, **_):
        invoked.append({"agent_id": agent_id, "user_input": user_input, "sealed_inputs": sealed_inputs})
        return {"output": f"output-of-{agent_id}", "final_state": "Recommend",
                "interaction_id": None, "duration_ms": 1, "evidence_score": 0, "transitions": [], "trace": {}}
    monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)
    return invoked


# como o /invoke dobra os args `llm` na entrada:
FOLDED = 'analise\n\n## Parâmetros estruturados\n```json\n{"tom": "formal"}\n```'


class TestFase3LlmArgsReachDownstream:
    @pytest.mark.asyncio
    async def test_llm_args_block_reaches_second_agent(self, monkeypatch):
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {"A": _agent("A", "Maestro"), "B": _agent("B", "Especialista")})
        _patch_topology(monkeypatch, {"A": [_conn("A", "B")]})   # linear A→B
        invoked = _patch_executions(monkeypatch)

        await eng.execute_pipeline(entry_agent_id="A", user_input=FOLDED)

        by_id = {x["agent_id"]: x for x in invoked}
        assert '"tom": "formal"' in by_id["A"]["user_input"]        # entry vê (óbvio)
        # DOWNSTREAM: o bloco llm chega via "## Solicitação original" — Fase 3 satisfeita
        assert "## Solicitação original" in by_id["B"]["user_input"]
        assert '"tom": "formal"' in by_id["B"]["user_input"]


class TestPosturaBEndToEnd:
    @pytest.mark.asyncio
    async def test_sealed_inputs_route_conditional_by_value_gold(self, monkeypatch):
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {"A": _agent("A", "Maestro"), "B": _agent("B", "Premium")})
        _patch_topology(monkeypatch, {"A": [
            _conn("A", "B", ctype="conditional", expr="inputs.tier == 'gold'")]})
        invoked = _patch_executions(monkeypatch)

        await eng.execute_pipeline(entry_agent_id="A", user_input="x", sealed_inputs={"tier": "gold"})
        assert "B" in [x["agent_id"] for x in invoked]   # gold → B roda (por valor, sem LLM)

    @pytest.mark.asyncio
    async def test_sealed_inputs_skip_when_value_differs(self, monkeypatch):
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {"A": _agent("A", "Maestro"), "B": _agent("B", "Premium")})
        _patch_topology(monkeypatch, {"A": [
            _conn("A", "B", ctype="conditional", expr="inputs.tier == 'gold'")]})
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="A", user_input="x", sealed_inputs={"tier": "silver"})
        ran = [x["agent_id"] for x in invoked]
        assert "A" in ran and "B" not in ran   # silver → B pulado por valor
        assert {s["agent_id"]: s["status"] for s in res["pipeline_steps"]}["B"] == "skipped_conditional"
