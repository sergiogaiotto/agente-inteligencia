"""Fase B — Slice 1 (2026-06-07): a SA declarativa extrai `inputs` de um bloco
estruturado embutido na saída do roteador (além de JSON puro), p/ alimentar o
binding. Base para o roteador (Slice 2 / Compor) entregar os params extraídos.
"""
from __future__ import annotations

import pytest


class TestExtractInputsFromText:
    def test_fenced_json_block(self):
        from app.agents.engine import _extract_inputs_from_text
        t = 'Roteando para Busca endereço.\n```json\n{"inputs": {"cep": "13211740"}}\n```'
        assert _extract_inputs_from_text(t) == {"cep": "13211740"}

    def test_inline_object_unwraps_inputs(self):
        from app.agents.engine import _extract_inputs_from_text
        t = 'decisão: {"target": "Busca endereço", "inputs": {"cep": "01001000"}}'
        assert _extract_inputs_from_text(t) == {"cep": "01001000"}

    def test_plain_dict_without_inputs_key(self):
        from app.agents.engine import _extract_inputs_from_text
        assert _extract_inputs_from_text('x {"cep": "123"} y') == {"cep": "123"}

    def test_no_json_returns_empty(self):
        from app.agents.engine import _extract_inputs_from_text
        assert _extract_inputs_from_text("apenas prosa, sem json") == {}
        assert _extract_inputs_from_text(None) == {}


class TestDeclarativeUsesExtractedInputs:
    @pytest.mark.asyncio
    async def test_router_block_feeds_binding(self, monkeypatch):
        from app.agents import engine as eng
        captured: dict = {}

        async def fake(**kw):
            captured.update(kw)
            return {"context": {"resposta": "ok"}, "bindings_executed": [{"status": 200}],
                    "errors": [], "final_state": "completed"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake)

        await eng._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(),
            user_input='Isto é um CEP. Roteando.\n```json\n{"inputs": {"cep": "13211740"}}\n```',
            session_id="s",
        )
        assert captured["inputs"] == {"cep": "13211740"}

    @pytest.mark.asyncio
    async def test_structured_target_inputs_unwrapped(self, monkeypatch):
        from app.agents import engine as eng
        captured: dict = {}

        async def fake(**kw):
            captured.update(kw)
            return {"context": {}, "bindings_executed": [], "errors": [], "final_state": "completed"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake)

        await eng._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(),
            user_input='{"target": "X", "inputs": {"cep": "99999000"}}', session_id="s",
        )
        assert captured["inputs"] == {"cep": "99999000"}

    @pytest.mark.asyncio
    async def test_plain_text_still_question(self, monkeypatch):
        from app.agents import engine as eng
        captured: dict = {}

        async def fake(**kw):
            captured.update(kw)
            return {"context": {}, "bindings_executed": [], "errors": [], "final_state": "completed"}
        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake)

        await eng._run_declarative_as_interaction(
            agent={"id": "a"}, parsed_skill=object(), user_input="só texto", session_id="s")
        assert captured["inputs"] == {"question": "só texto"}
