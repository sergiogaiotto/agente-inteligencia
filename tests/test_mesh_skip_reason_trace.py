"""Rastreabilidade do MOTIVO do skip no pipeline (Fatia 3a — 2026-06-07).

Quando um SA é pulado num fan-out condicional, o trace dizia sempre "Conexão
condicional avaliou false" — impreciso. Com o target estruturado (Fatia 1), há
DOIS motivos distintos de skip e o operador precisa distinguir:

  • `structured_target_not_chosen`: o roteador emitiu {"target": OUTRO} → este SA
    foi PRETERIDO (1-de-N), não "avaliou false";
  • `conditional_false`: não havia target estruturado e a `expr` de keyword não
    casou → condição não satisfeita.

A derivação é precisa por construção: o override estruturado roda ANTES da expr
em `_should_skip_conditional` — logo, skip COM target estruturado nomeando outro
⟺ preterido; skip SEM bloco ⟺ expr. O engine re-deriva isso no call site (via
`_extract_routed_target`) e expõe em `step.skip_reason` + diagnóstico + evento.
"""
from __future__ import annotations

import json

import pytest


def _agent(aid: str, name: str) -> dict:
    return {"id": aid, "name": name, "status": "active", "kind": "subagent",
            "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "p"}


def _conn(src: str, tgt: str, *, expr: str | None = None) -> dict:
    cfg = json.dumps({"expr": expr}) if expr is not None else "{}"
    return {"source_agent_id": src, "target_agent_id": tgt,
            "connection_type": "conditional", "config": cfg}


def _patch_topology(monkeypatch, conns_by_source: dict):
    async def fake_find_all(source_agent_id=None, limit=20, **_):
        return list(conns_by_source.get(source_agent_id, []))
    monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)


def _patch_agents(monkeypatch, agents: dict):
    async def fake_find_by_id(aid):
        return agents.get(aid)
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)


def _patch_executions(monkeypatch, outputs: dict):
    async def fake_exec(*, agent_id, user_input, channel="api", attachments=None,
                        pipeline_context=None, session_id=None, **_):
        return {"output": outputs.get(agent_id, f"out-{agent_id}"),
                "final_state": "Recommend", "interaction_id": None, "duration_ms": 1,
                "evidence_score": 0, "transitions": [], "trace": {}}
    monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)


def _step(res, aid):
    return next(s for s in res["pipeline_steps"] if s["agent_id"] == aid)


class TestSkipReasonTrace:
    @pytest.mark.asyncio
    async def test_structured_skip_is_preterido(self, monkeypatch):
        """Roteador emite {target: Busca endereço} → Tavily é PRETERIDO, não
        'avaliou false'. O step carrega skip_reason + diagnóstico nomeando quem
        o roteador escolheu."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "BU": _agent("BU", "Buscador A"), "END": _agent("END", "Busca endereço"),
            "TAV": _agent("TAV", "Tavily"), "FAQ": _agent("FAQ", "FAQ Claro")})
        _patch_topology(monkeypatch, {"BU": [
            _conn("BU", "END", expr="'cep' in input_lower"),
            _conn("BU", "TAV", expr="'turismo' in input_lower"),
            _conn("BU", "FAQ", expr="'loja' in input_lower")]})
        _patch_executions(monkeypatch,
                          {"BU": '{"target": "Busca endereço", "inputs": {"cep": "13211740"}}'})

        res = await eng.execute_pipeline(entry_agent_id="BU", user_input="13211740")

        tav = _step(res, "TAV")
        assert tav["status"] == "skipped_conditional"
        assert tav["skip_reason"] == "structured_target_not_chosen"
        text = tav["trace"]["diagnostics"][0]["text"].lower()
        assert "preterido" in text and "busca endereço" in text

    @pytest.mark.asyncio
    async def test_expr_skip_is_conditional_false(self, monkeypatch):
        """Sem bloco estruturado, o skip é por expr de keyword → motivo
        'conditional_false' (condição não satisfeita), texto sem 'preterido'."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "BU": _agent("BU", "Buscador A"), "END": _agent("END", "Busca endereço"),
            "TAV": _agent("TAV", "Tavily")})
        _patch_topology(monkeypatch, {"BU": [
            _conn("BU", "END", expr="'cep' in input_lower"),
            _conn("BU", "TAV", expr="'turismo' in input_lower")]})
        _patch_executions(monkeypatch, {"BU": "ok, processando"})

        res = await eng.execute_pipeline(entry_agent_id="BU", user_input="meu cep")

        tav = _step(res, "TAV")
        assert tav["status"] == "skipped_conditional"
        assert tav["skip_reason"] == "conditional_false"
        text = tav["trace"]["diagnostics"][0]["text"].lower()
        assert "preterido" not in text and "condição" in text
