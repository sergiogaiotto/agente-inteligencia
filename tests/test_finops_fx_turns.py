"""FinOps médios (35.12.0): câmbio USD→BRL configurável + grão por turno.

- fx_usd_brl: era 5.30 HARDCODED no Alpine do Cockpit — vira setting runtime-
  editável exposto ao template como callable Jinja (padrão text_to_sql_enabled).
- FIN-3: turns.tokens_used/latency_ms existiam MORTAS no DDL — agora os turnos
  de SAÍDA carregam tokens billed + latência (o de input fica 0 por design: a
  geração pertence à resposta). Pipeline consolidado = grão POR STEP.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class TestCambioConfiguravel:
    def test_setting_nos_toques(self):
        from app.core.config import Settings, _UI_TO_ENV_MAP, PARAMETER_UI_KEYS
        assert "fx_usd_brl" in Settings.model_fields
        assert Settings.model_fields["fx_usd_brl"].default == 5.30  # paridade com o hardcode antigo
        assert _UI_TO_ENV_MAP["fx_usd_brl"] == "FX_USD_BRL"
        assert "fx_usd_brl" in PARAMETER_UI_KEYS

    def test_template_usa_o_global_nao_hardcode(self):
        html = Path("app/templates/pages/mesh_playground.html").read_text(encoding="utf-8")
        assert "tcoFx: Number('{{ fx_usd_brl() }}') || 5.30" in html
        assert "tcoFx: 5.30," not in html  # o hardcode saiu

    def test_global_jinja_registrado_como_callable(self):
        # callable = lê o setting a CADA render (runtime sem restart)
        src = Path("app/main.py").read_text(encoding="utf-8")
        assert 'env.globals["fx_usd_brl"]' in src
        assert "lambda: _gs_fx().fx_usd_brl" in src


class TestTurnsGrain:
    def test_fsm_output_turn_carrega_tokens_e_latencia(self):
        src = Path("app/agents/state_machine.py").read_text(encoding="utf-8")
        assert '"_t0_monotonic"' in src            # âncora no intake
        assert '"tokens_used": _tok' in src
        assert '"latency_ms": _lat' in src
        # tokens vêm do billed (o que o provider cobra), não do total cru
        assert 'get("total_billed")' in src

    def test_pipeline_consolidado_grao_por_step(self):
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert '"tokens_used": int(step.get("tokens_used") or 0)' in src
        assert '"latency_ms": float(step.get("duration_ms") or 0)' in src

    def test_declarativo_tokens_zero_legitimo(self):
        eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
        ws = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        # caminhos sem LLM: tokens 0 explícito + latência real quando disponível
        assert eng.count('"tokens_used": 0,') >= 1
        assert ws.count('"tokens_used": 0,') >= 1

    def test_persist_invoke_turn_deriva_do_trace(self):
        ws = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert 'float((trace_data or {}).get("duration_ms") or 0)' in ws
        assert 'st.get("tokens_used")' in ws  # soma dos steps quando pipeline

    @pytest.mark.asyncio
    async def test_fsm_end_to_end_popula_grao(self, monkeypatch):
        """Comportamental: run_intake → run_log_and_close grava o turno de
        saída com tokens do ctx.metadata e latência > 0.

        Relógio CONTROLADO (35.14.6): com repos mockados o intake→close roda
        dentro de UM tick do time.monotonic() do Windows (resolução ~15ms) →
        latência 0 e flake. O fake avança 10ms por leitura — determinístico."""
        import time as _time
        _clk = {"v": 1000.0}

        def _fake_monotonic():
            _clk["v"] += 0.010
            return _clk["v"]
        monkeypatch.setattr(_time, "monotonic", _fake_monotonic)
        from unittest.mock import AsyncMock
        from app.agents.state_machine import (
            InteractionStateMachine, InteractionContext, State)
        from app.core import database as db
        created = []

        async def cap_create(data):
            created.append(dict(data))
            return data
        monkeypatch.setattr(db.turns_repo, "create", cap_create)
        monkeypatch.setattr(db.turns_repo, "find_all", AsyncMock(return_value=[]))
        monkeypatch.setattr(db.interactions_repo, "create", AsyncMock())
        monkeypatch.setattr(db.interactions_repo, "update", AsyncMock())
        monkeypatch.setattr(db.interactions_repo, "find_by_id", AsyncMock(return_value=None))
        monkeypatch.setattr(db.audit_repo, "create", AsyncMock())

        ctx = InteractionContext()
        fsm = InteractionStateMachine(ctx)
        await fsm.run_intake("pergunta", agent_id="a1")
        ctx.metadata["tokens"] = {"total_billed": 321}
        ctx.final_output = "resposta"
        # caminho mínimo até o close (estados intermediários pulados no teste:
        # seta o estado e chama o close diretamente)
        ctx.current_state = State.RECOMMEND
        await fsm.run_log_and_close()

        out_turns = [t for t in created if "output_text_redacted" in t]
        assert out_turns, "turno de saída não gravado"
        assert out_turns[0]["tokens_used"] == 321
        assert out_turns[0]["latency_ms"] > 0
        # o turno de INPUT fica 0/ausente por design
        in_turns = [t for t in created if "user_text_redacted" in t]
        assert all(not t.get("tokens_used") for t in in_turns)
