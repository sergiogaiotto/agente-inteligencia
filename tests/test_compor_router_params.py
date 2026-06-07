"""Fase B — Slice 2 (2026-06-07): o Compor (montagem do prompt do roteador em
agent_form.html) instrui o roteador a EXTRAIR e EMITIR os parâmetros que o
destino precisa, num bloco ```json {"target","inputs"}``` — consumido pelo engine
(_extract_inputs_from_text, Slice 1) como inputs do binding declarativo da SA.

Frontend Alpine não roda no pytest → smoke de fonte (mesmo padrão de
TestUiAndBackendWiring). Garante que a instrução existe na montagem do roteador.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class TestComporRouterParamsBlock:
    def _agent_form(self) -> str:
        return (_ROOT / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")

    def test_router_prompt_has_params_section(self):
        src = self._agent_form()
        assert "## Parâmetros (destinos com ferramenta/API)" in src

    def test_router_prompt_instructs_structured_block(self):
        src = self._agent_form()
        # o bloco que o engine consome: {"target": ..., "inputs": {...}}
        assert '{"target": "<nome do destino>", "inputs": {"<parametro>": "<valor extraído>"}}' in src
        assert "```json" in src

    def test_block_is_gated_to_when_params_exist(self):
        src = self._agent_form()
        assert "SOMENTE quando houver parâmetros a passar" in src

    def test_engine_consumes_same_block_shape(self):
        """Coerência ponta a ponta: o engine (Slice 1) extrai 'inputs' do bloco."""
        eng = (_ROOT / "app" / "agents" / "engine.py").read_text(encoding="utf-8")
        assert "_extract_inputs_from_text" in eng
