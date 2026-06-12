"""Fluxograma de agentes (PR4) — replay "Última execução".

O trace de um pipeline é persistido em interactions.trace_data (JSON). O replay
no Fluxograma precisa da execução de PIPELINE mais recente num shape canvas-ready
(step por agent_id, status ran/skipped, skip_reason + diagnóstico, final_state).
GET /api/v1/mesh/last-run varre as interactions (DESC) e devolve a primeira com
pipeline_steps não-vazio — pulando invocações sem trace replayável.

Cobertura:
- nenhuma execução replayável → found=False
- pula trace_data vazio/inválido/sem pipeline_steps; pega a 1ª válida (mais recente)
- extrai status/skip_reason/final_state e o diagnóstico humano de trace.diagnostics[0].text
- keyed por agent_id (= id do nó no canvas)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest


def _patch_repo(monkeypatch, rows):
    repo = AsyncMock()
    repo.find_all = AsyncMock(return_value=rows)
    monkeypatch.setattr("app.core.database.interactions_repo", repo)
    return repo


@pytest.mark.asyncio
async def test_no_replayable_run_returns_found_false(monkeypatch):
    from app.routes import mesh
    _patch_repo(monkeypatch, [
        {"id": "i1", "trace_data": ""},                       # vazio
        {"id": "i2", "trace_data": "{not json"},              # inválido
        {"id": "i3", "trace_data": json.dumps({"mode": "agent"})},  # sem pipeline_steps
        {"id": "i4", "trace_data": json.dumps({"pipeline_steps": []})},  # lista vazia
    ])
    res = await mesh.get_last_run()
    assert res == {"found": False, "steps": []}


@pytest.mark.asyncio
async def test_picks_first_pipeline_and_maps_steps(monkeypatch):
    from app.routes import mesh
    td = {
        "agent_id": "root-1",
        "final_state": "Recommend",
        "pipeline_steps": [
            {"agent_id": "root-1", "agent_name": "Triagem", "status": "completed", "final_state": "Recommend", "duration_ms": 1200},
            {"agent_id": "sa-1", "agent_name": "Especialista A", "status": "completed", "final_state": "Recommend", "duration_ms": 800},
            {"agent_id": "sa-2", "agent_name": "Especialista B", "status": "skipped_conditional",
             "skip_reason": "conditional_false", "final_state": "SkippedConditional", "duration_ms": 0,
             "trace": {"diagnostics": [{"level": "info", "text": "regra não casou: 'pix' ausente"}]}},
        ],
    }
    _patch_repo(monkeypatch, [
        {"id": "newest-no-trace", "trace_data": "{}"},        # mais recente porém sem pipeline → pula
        {"id": "pipe-1", "title": "consorcio aprovado", "created_at": "2026-06-12T10:00:00", "trace_data": json.dumps(td)},
        {"id": "older", "trace_data": json.dumps({"pipeline_steps": [{"agent_id": "x", "status": "completed"}]})},
    ])
    res = await mesh.get_last_run()
    assert res["found"] is True
    assert res["session_id"] == "pipe-1"
    assert res["title"] == "consorcio aprovado"
    assert res["final_state"] == "Recommend"
    assert res["entry_agent_id"] == "root-1"
    steps = {s["agent_id"]: s for s in res["steps"]}
    assert set(steps) == {"root-1", "sa-1", "sa-2"}
    assert steps["sa-1"]["status"] == "completed"
    assert steps["sa-2"]["status"] == "skipped_conditional"
    assert steps["sa-2"]["skip_reason"] == "conditional_false"
    assert steps["sa-2"]["diagnostic"] == "regra não casou: 'pix' ausente"
    assert steps["root-1"]["final_state"] == "Recommend"


@pytest.mark.asyncio
async def test_diagnostic_defaults_empty_when_absent(monkeypatch):
    from app.routes import mesh
    td = {"pipeline_steps": [{"agent_id": "a", "status": "completed", "final_state": "Recommend"}]}
    _patch_repo(monkeypatch, [{"id": "p", "trace_data": json.dumps(td)}])
    res = await mesh.get_last_run()
    assert res["steps"][0]["diagnostic"] == ""
    assert res["steps"][0]["skip_reason"] is None
