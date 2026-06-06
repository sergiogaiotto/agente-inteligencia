"""Bug user 2026-06-06: rodei um pipeline no workspace (toggle Pipeline);
o item pipeline apontou certo e a Rastreabilidade + Execution Log
apareceram DURANTE a execução. Mas ao SAIR e VOLTAR pra sessão, tudo
sumiu e o toggle mostrou 'Agente' (errado — é Pipeline).

Causa-raiz: o frontend roda pipeline via POST /chat/stream (SSE, pra
mostrar o log ao vivo). Esse endpoint chamava execute_pipeline mas
DESCARTAVA o result e NUNCA gravava trace_data — só o POST /chat sync
persistia. Sem trace_data, o GET /workspace/sessions infere mode='agent'
(pipeline_steps vazios) e execution_log vazio → painéis somem e o toggle
cai em 'agent'.

Fix:
- engine.execute_pipeline persiste trace_data (mode='pipeline' +
  pipeline_steps + trace.execution_log) no interaction mestre — cobre
  stream E sync num lugar só. `_build_pipeline_trace_data` isola o
  payload pra teste behavioral.
- workspace.html loadSession: defesa em profundidade — deriva 'pipeline'
  quando o agente da sessão é raiz de pipeline (pipelineRoots, carregado
  no boot antes de loadSession), consertando o toggle até de sessões
  antigas gravadas sem trace.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_ENGINE_SRC = (
    Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"
).read_text(encoding="utf-8")
_WS_HTML = (
    Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html"
).read_text(encoding="utf-8")


def _sample_result() -> dict:
    return {
        "final_state": "LogAndClose",
        "evidence_score": 0.7,
        "transitions": [{"to": "Recommend"}],
        "duration_ms": 1234,
        "trace": {"execution_log": [{"title": "passo 1"}], "evidence_count": 2},
        "pipeline_steps": [{"agent_id": "a1", "status": "completed"}],
    }


# ─── _build_pipeline_trace_data (payload de persistência) ────────────


class TestBuildPipelineTraceData:
    def test_mode_is_pipeline(self):
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", _sample_result())
        assert td["mode"] == "pipeline"

    def test_preserves_pipeline_steps(self):
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", _sample_result())
        assert td["pipeline_steps"] == [{"agent_id": "a1", "status": "completed"}]

    def test_preserves_execution_log(self):
        """O Execution Log agregado tem que sobreviver — é o que reaparece
        no painel ao recarregar (via _restoreLog → trace.trace.execution_log)."""
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", _sample_result())
        assert td["trace"]["execution_log"] == [{"title": "passo 1"}]

    def test_carries_ids(self):
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", _sample_result())
        assert td["interaction_id"] == "sess-1"
        assert td["agent_id"] == "a1"

    def test_json_serializable(self):
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", _sample_result())
        dumped = json.dumps(td, ensure_ascii=False, default=str)
        assert '"mode": "pipeline"' in dumped

    def test_safe_defaults_when_result_minimal(self):
        """final_result sem chaves → defaults seguros (sem KeyError)."""
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", {})
        assert td["mode"] == "pipeline"
        assert td["pipeline_steps"] == []
        assert td["transitions"] == []
        assert td["evidence_score"] == 0
        assert td["duration_ms"] == 0
        assert td["trace"] == {}


# ─── Round-trip: payload gravado → GET /sessions devolve mode='pipeline' ─


class TestGetSessionRoundTrip:
    @pytest.mark.asyncio
    async def test_persisted_pipeline_trace_round_trips(self, monkeypatch):
        """O payload de _build_pipeline_trace_data, lido de volta pelo
        GET /sessions, restaura mode='pipeline' + steps + execution_log —
        exatamente o que o frontend usa pra toggle + Rastreabilidade."""
        from app.routes import workspace as ws
        from app.agents.engine import _build_pipeline_trace_data

        td = _build_pipeline_trace_data("sess-1", "a1", _sample_result())

        async def fake_find_by_id(sid):
            return {
                "id": sid,
                "agent_id": "a1",
                "state": "LogAndClose",
                "trace_data": json.dumps(td),
            }

        async def fake_turns(interaction_id=None, limit=200, **_):
            return []

        monkeypatch.setattr(
            "app.routes.workspace.interactions_repo.find_by_id", fake_find_by_id
        )
        monkeypatch.setattr("app.routes.workspace.turns_repo.find_all", fake_turns)

        out = await ws.get_session("sess-1")
        assert out["trace"]["mode"] == "pipeline"
        assert out["trace"]["pipeline_steps"] == [{"agent_id": "a1", "status": "completed"}]
        assert out["trace"]["trace"]["execution_log"] == [{"title": "passo 1"}]

    @pytest.mark.asyncio
    async def test_session_without_trace_falls_back_to_agent(self, monkeypatch):
        """Controle: sessão SEM trace_data e sem pipeline_steps continua
        mode='agent' (não vira pipeline por engano)."""
        from app.routes import workspace as ws

        async def fake_find_by_id(sid):
            return {"id": sid, "agent_id": "a9", "state": "LogAndClose", "trace_data": None}

        async def fake_turns(interaction_id=None, limit=200, **_):
            return []

        monkeypatch.setattr(
            "app.routes.workspace.interactions_repo.find_by_id", fake_find_by_id
        )
        monkeypatch.setattr("app.routes.workspace.turns_repo.find_all", fake_turns)

        out = await ws.get_session("sess-2")
        assert out["trace"]["mode"] == "agent"


# ─── Source smoke: execute_pipeline grava trace_data ────────────────


class TestExecutePipelinePersistsTrace:
    def test_calls_update_with_trace_data(self):
        assert "_build_pipeline_trace_data(" in _ENGINE_SRC
        assert "interactions_repo.update(" in _ENGINE_SRC
        assert '"trace_data": json.dumps(' in _ENGINE_SRC

    def test_persist_guarded_by_master_id(self):
        assert "if master_interaction_id:" in _ENGINE_SRC

    def test_persist_failure_logs_event(self):
        # Falha de persist não derruba a resposta — só loga pra troubleshooting
        assert "pipeline.trace_data_persist_failed" in _ENGINE_SRC


# ─── Source smoke: frontend loadSession fallback ────────────────────


class TestLoadSessionPipelineRootFallback:
    def test_derives_mode_from_pipeline_roots(self):
        assert (
            "const isPipelineRoot = (this.pipelineRoots || []).some(p => p.id === sessionAgentId);"
            in _WS_HTML
        )

    def test_combined_into_is_pipeline_session(self):
        assert (
            "d.trace?.mode === 'pipeline' || hasPipelineSteps || isPipelineRoot" in _WS_HTML
        )
