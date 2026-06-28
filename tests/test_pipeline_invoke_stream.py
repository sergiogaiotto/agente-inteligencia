"""Streaming (SSE) do invoke selado — passo-a-passo ao vivo (fatia #1 da exp. v2).

POST /pipelines/{id}/invoke/stream roda o pipeline SELADO (root+membros) e emite,
via progress_callback do engine, 1 evento SSE por transição: agent_start/done/
skipped/error + pipeline_done (com o result) + end. O modal mostra a checklist
viva. Espelha o padrão do /workspace/chat/stream.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
from app.routes import pipelines as pl_routes

MESH_FLOW = Path("app/templates/pages/mesh_flow.html")


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl_routes.router)
    app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test"}
    return TestClient(app, raise_server_exceptions=False)


def _stub_pipeline(monkeypatch):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P", "status": "publicado"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))


def test_stream_emite_eventos_sse_ao_vivo(monkeypatch):
    captured = {}
    async def fake_exec(**k):
        captured.update(k)
        cb = k.get("progress_callback")
        if cb:
            await cb({"type": "pipeline_start", "total_agents": 1})
            await cb({"type": "agent_start", "agent_id": "r", "agent_name": "Raiz", "processing_message": "pensando"})
            await cb({"type": "agent_done", "agent_id": "r", "agent_name": "Raiz"})
            await cb({"type": "pipeline_done", "result": {"output": "ok", "pipeline_steps": []}})
        return {}
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)

    r = _client().post("/api/v1/pipelines/p1/invoke/stream", json={"message": "oi"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # eventos por transição + resultado final + fim
    assert "event: agent_start" in body
    assert "pensando" in body  # a mensagem de status (processing_message) chega ao vivo
    assert "event: pipeline_done" in body
    assert "event: end" in body
    # executou SELADO: allowed_agent_ids = membros do subgrafo
    assert captured["allowed_agent_ids"] == {"r"}
    assert captured["progress_callback"] is not None


def test_stream_projeta_pipeline_done_por_verbosidade(monkeypatch):
    # o pipeline_done final HONRA verbosidade (igual ao /invoke) — senão a console
    # "ver como integração" mentiria. Aqui verbosity=minimal → result enxuto.
    async def fake_exec(**k):
        cb = k.get("progress_callback")
        if cb:
            await cb({"type": "pipeline_done", "result": {
                "output": "x", "status": "completed", "interaction_id": "i",
                "total_agents": 1, "completed_agents": 1,
                "pipeline_steps": [{"agent_name": "A", "status": "completed", "trace": {"sql_rendered": "SELECT 1"}}],
            }})
        return {}
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)

    r = _client().post("/api/v1/pipelines/p1/invoke/stream?verbosity=minimal", json={"message": "oi"})
    body = r.text
    assert "event: pipeline_done" in body
    assert '"verbosity": "minimal"' in body          # projetado
    assert "pipeline_steps" not in body              # minimal não traz steps
    assert "sql_rendered" not in body                # nem o trace interno


def test_stream_400_sem_mensagem(monkeypatch):
    _stub_pipeline(monkeypatch)
    r = _client().post("/api/v1/pipelines/p1/invoke/stream", json={"message": "   "})
    assert r.status_code == 400, r.text


def test_stream_409_aposentado(monkeypatch):
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P", "status": "aposentado"}))
    r = _client().post("/api/v1/pipelines/p1/invoke/stream", json={"message": "oi"})
    assert r.status_code == 409, r.text


def test_modal_consome_stream_ao_vivo():
    src = MESH_FLOW.read_text(encoding="utf-8")
    # runPipeline usa o endpoint de streaming + parser de eventos + checklist viva
    assert "/invoke/stream" in src
    assert "_handleRunEvent(" in src
    assert 'data-testid="pipeline-run-live"' in src
    assert "runModal.liveSteps" in src
    assert "liveIcon(" in src
