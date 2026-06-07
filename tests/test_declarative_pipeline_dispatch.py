"""Fase A (2026-06-07): SA declarativa roda o engine de API Bindings quando
alcançada por um SR/AOBD via execute_pipeline — não só no invoke direto.

`execute_interaction` (chokepoint do pipeline) passa a despachar para
`execute_declarative` quando `execution_mode == "declarative"`, adaptando o
retorno via `_run_declarative_as_interaction`. (A extração de params do NL p/ o
binding é a Fase B; aqui o binding já dispara.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


class TestRunDeclarativeAsInteraction:
    @pytest.mark.asyncio
    async def test_adapts_resposta_and_shape(self, monkeypatch):
        from app.agents import engine as eng
        captured: dict = {}

        async def fake_exec(**kw):
            captured.update(kw)
            return {
                "context": {"resposta": "Rua Aristides Mariotti, Jundiaí-SP"},
                "bindings_executed": [{"status": 200, "method": "GET", "path": "/cep", "connector": "viacep"}],
                "errors": [],
                "final_state": "completed",
                "duration_ms": 120,
                "interaction_id": "itx1",
            }
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_exec)

        res = await eng._run_declarative_as_interaction(
            agent={"id": "a", "name": "Busca endereço", "kind": "subagent"},
            parsed_skill=object(),
            user_input='{"cep": "13211740"}',
            session_id="s1",
        )
        assert res["mode"] == "declarative"
        assert res["final_state"] == "completed"
        assert "Aristides" in res["output"]
        assert res["trace"]["total_steps"] == 1
        # inputs vêm do JSON literal da mensagem
        assert captured["inputs"] == {"cep": "13211740"}
        # caller é dono da sessão → não registra interaction (evita órfã)
        assert captured["register_interaction"] is False

    @pytest.mark.asyncio
    async def test_plain_text_becomes_question(self, monkeypatch):
        from app.agents import engine as eng
        captured: dict = {}

        async def fake_exec(**kw):
            captured.update(kw)
            return {"context": {}, "bindings_executed": [], "errors": [],
                    "final_state": "completed", "api_response": "ok"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_exec)

        await eng._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(),
            user_input="13211740", session_id=None,
        )
        # texto puro (não-JSON) cai em {"question": ...}
        assert captured["inputs"] == {"question": "13211740"}

    @pytest.mark.asyncio
    async def test_partial_when_errors_present(self, monkeypatch):
        from app.agents import engine as eng

        async def fake_exec(**kw):
            return {"context": {"resposta": "x"}, "bindings_executed": [{"status": 200}],
                    "errors": ["JSONPath 'a' não encontrou valor"], "final_state": "partial"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_exec)

        res = await eng._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(), user_input="oi", session_id="s")
        assert res["final_state"] == "partial"
        assert res["errors"]
        assert res["trace"]["diagnostics"][0]["level"] == "warning"


class TestDispatchWiring:
    def test_engine_dispatches_declarative_in_execute_interaction(self):
        src = (_ROOT / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        assert "async def _run_declarative_as_interaction(" in src
        assert 'if exec_profile == "declarative":' in src
        assert "_run_declarative_as_interaction(" in src
        # fail-open: erro no dispatch cai no LLM
        assert "declarative.dispatch_failed_fallback_llm" in src
