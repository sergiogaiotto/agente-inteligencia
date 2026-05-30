"""Onda C.1 — Endpoint GET /api/v1/agents/{id}/last-activity.

Devolve a interação mais recente do agente (canonical de atividade NL),
usado pelo painel de detalhe pra mostrar "Última atividade: 2h atrás · OK".

Cobertura:
- 404 quando agente desconhecido
- has_activity=False quando nunca foi invocado
- has_activity=True com state/ok derivado corretamente
- ok mapping: LogAndClose→True, Refuse/Failed→False, em andamento→None
- duration_ms calculado de started_at/ended_at
- Smoke estático do painel agents.html (estado + helpers + HTML)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


AGENT_ROW = {
    "id": "agent-C",
    "name": "Test Agent",
    "kind": "subagent",
    "status": "active",
    "skill_id": "skill-1",
}


@pytest.fixture
def app_client():
    from app.routes.agents import router as agents_router
    app = FastAPI()
    app.include_router(agents_router)
    return TestClient(app)


def _patch_agents(monkeypatch, agent=AGENT_ROW):
    async def fake_find(aid):
        return agent if (agent and agent["id"] == aid) else None
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find)


def _patch_interactions(monkeypatch, rows):
    async def fake_find_all(limit=1, offset=0, **filters):
        # filtra por agent_id se vier
        aid = filters.get("agent_id")
        if aid:
            return [r for r in rows if r.get("agent_id") == aid][:limit]
        return rows[:limit]
    monkeypatch.setattr("app.core.database.interactions_repo.find_all", fake_find_all)


# ────────────────────────────────────────────────────────────────
# Endpoint behavior
# ────────────────────────────────────────────────────────────────


class TestLastActivityEndpoint:
    def test_returns_404_for_unknown_agent(self, app_client, monkeypatch):
        _patch_agents(monkeypatch, agent=None)
        r = app_client.get("/api/v1/agents/missing/last-activity")
        assert r.status_code == 404

    def test_has_activity_false_when_no_interactions(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        _patch_interactions(monkeypatch, rows=[])
        r = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/last-activity")
        assert r.status_code == 200
        body = r.json()
        assert body["has_activity"] is False
        assert body["last_ts"] is None
        assert body["ok"] is None

    def test_returns_recent_interaction_with_ok_true(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        ts = datetime(2026, 5, 30, 12, 0, 0)
        end = datetime(2026, 5, 30, 12, 0, 5)  # 5s depois
        _patch_interactions(monkeypatch, rows=[{
            "id": "int-1", "agent_id": AGENT_ROW["id"],
            "state": "LogAndClose", "started_at": ts, "ended_at": end,
            "created_at": ts, "channel": "ui",
        }])
        r = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/last-activity")
        body = r.json()
        assert body["has_activity"] is True
        assert body["ok"] is True
        assert body["state"] == "LogAndClose"
        assert body["interaction_id"] == "int-1"
        assert body["channel"] == "ui"
        assert body["duration_ms"] == 5000

    def test_ok_false_for_refuse_state(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        _patch_interactions(monkeypatch, rows=[{
            "id": "int-2", "agent_id": AGENT_ROW["id"],
            "state": "Refuse", "started_at": None, "ended_at": None,
            "created_at": datetime(2026, 5, 30), "channel": "api",
        }])
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/last-activity").json()
        assert body["ok"] is False

    def test_ok_none_for_intermediate_state(self, app_client, monkeypatch):
        """States em andamento (Intake, Processing, etc.) → ok=None."""
        _patch_agents(monkeypatch)
        _patch_interactions(monkeypatch, rows=[{
            "id": "int-3", "agent_id": AGENT_ROW["id"],
            "state": "Intake", "started_at": None, "ended_at": None,
            "created_at": datetime(2026, 5, 30), "channel": "api",
        }])
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/last-activity").json()
        assert body["ok"] is None

    def test_duration_zero_when_ended_at_missing(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        _patch_interactions(monkeypatch, rows=[{
            "id": "int-4", "agent_id": AGENT_ROW["id"],
            "state": "LogAndClose",
            "started_at": datetime(2026, 5, 30), "ended_at": None,
            "created_at": datetime(2026, 5, 30), "channel": "api",
        }])
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/last-activity").json()
        assert body["duration_ms"] == 0


# ────────────────────────────────────────────────────────────────
# UI smoke — agents.html tem estado + helpers + HTML novo
# ────────────────────────────────────────────────────────────────


class TestUISmoke:
    def _html(self):
        from pathlib import Path
        return Path("app/templates/pages/agents.html").read_text(encoding="utf-8")

    def test_has_preview_capabilities_state(self):
        html = self._html()
        assert "previewCapabilities:" in html
        assert "previewLastActivity:" in html
        assert "previewLoading:" in html

    def test_open_preview_triggers_fetch_extras(self):
        html = self._html()
        assert "_fetchPreviewExtras" in html
        # Promise.all paralelo dos 2 endpoints
        assert "Promise.all" in html
        assert "/skills-context" in html
        assert "/last-activity" in html

    def test_race_condition_guarded(self):
        """User trocando de agente no meio do fetch — ignora resposta antiga."""
        html = self._html()
        assert "requestedId" in html
        assert "this.previewId !== requestedId" in html

    def test_capacidades_section_present(self):
        html = self._html()
        assert "CAPACIDADES" in html
        assert "_allBindings()" in html
        assert "_schemaSourceLabel" in html
        assert "_bindingKindColor" in html

    def test_atividade_section_present(self):
        html = self._html()
        assert "ATIVIDADE" in html
        assert "previewLastActivity.has_activity" in html
        assert "_relativeTime" in html

    def test_quick_actions_section_present(self):
        html = self._html()
        assert "QUICK ACTIONS" in html or "Ações Rápidas" in html
        # 4 botões esperados
        assert "Testar no Workspace" in html
        assert "Dry-run SKILL" in html
        assert "Ver invocações" in html
        assert "Probe MCP" in html

    def test_probe_mcp_only_shown_when_has_mcp(self):
        """Botão Probe MCP só aparece quando agente tem bindings MCP."""
        html = self._html()
        assert "_hasMcpBindings()" in html

    def test_schema_source_color_semantics(self):
        """skill_inputs/tool_input_schema/rag_fixed = verde (bom);
        legacy = âmbar (alerta)."""
        html = self._html()
        # Verde pra origens 'boas'
        assert "skill_inputs" in html
        assert "tool_input_schema" in html
        # Mapeamento com cores via helper
        assert "_schemaSourceClass" in html

    def test_relative_time_helper_handles_recent_and_old(self):
        """_relativeTime cobre s/min/h/d e cai pra timestamp pleno em > 30 dias."""
        html = self._html()
        assert "'s atrás'" in html
        assert "'min atrás'" in html
        assert "'h atrás'" in html
        assert "'d atrás'" in html
