"""Onda C.2 — Endpoint GET /api/v1/agents/{id}/stats?window=7d.

Agrega 5 tabelas (interactions, turns, tool_calls, api_call_logs,
binding_executions) num único JSON usado pelo painel de detalhe.

Cobertura:
- 404 quando agente desconhecido
- Window válida (24h/7d/30d/all) + invalid cai pra 7d
- Agregação correta com 0 atividade (counts zerados)
- Success rate calculado
- Tool calls breakdown (top 10 ordenado por count)
- Smoke estático do painel: state, helpers, HTML

Estratégia: mocks de _get_pool inline pra simular respostas asyncpg sem
exigir Postgres real. Testes de integração com Postgres ficam em
tests/integration/ (skipped quando sem DB).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


AGENT_ROW = {
    "id": "agent-C2",
    "name": "Test Agent C2",
    "kind": "subagent",
    "status": "active",
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


def _make_fake_pool(*, q1=None, q2=None, q3=None, q4=None, q5=None):
    """Cria mock do pool retornando respostas pré-definidas em ordem das queries
    (Q1 interactions, Q2 tokens/latency, Q3 tool_calls, Q4 api_calls, Q5 binding_executions).

    Default: tudo zerado (cenário sem atividade)."""
    q1 = q1 or {"total": 0, "ok": 0, "errors": 0, "in_progress": 0}
    q2 = q2 or {"total_tokens": 0, "avg_latency": 0.0, "p50_latency": 0.0, "p99_latency": 0.0, "turn_count": 0}
    q3 = q3 or []
    q4 = q4 or {"total": 0, "ok": 0, "errors": 0, "avg_latency": 0.0}
    q5 = q5 or {"total": 0, "ok": 0, "errors": 0}

    # Sequência de fetchrow / fetch — endpoint executa nessa ordem
    fetchrow_results = [q1, q2, q4, q5]  # 4 fetchrows (q3 é fetch=list)
    fetch_results = [q3]

    con = MagicMock()
    fetchrow_iter = iter(fetchrow_results)
    fetch_iter = iter(fetch_results)

    async def fake_fetchrow(query, *params):
        return next(fetchrow_iter)

    async def fake_fetch(query, *params):
        return next(fetch_iter)

    con.fetchrow = fake_fetchrow
    con.fetch = fake_fetch

    pool = MagicMock()

    class _AcquireCtx:
        async def __aenter__(self_inner): return con
        async def __aexit__(self_inner, *a): return None

    pool.acquire = lambda: _AcquireCtx()
    return pool


# ────────────────────────────────────────────────────────────────
# Endpoint behavior
# ────────────────────────────────────────────────────────────────


class TestStatsEndpoint:
    def test_returns_404_for_unknown_agent(self, app_client, monkeypatch):
        _patch_agents(monkeypatch, agent=None)
        r = app_client.get("/api/v1/agents/missing/stats")
        assert r.status_code == 404

    def test_zero_activity_returns_empty_aggregates(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        pool = _make_fake_pool()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        r = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["window"] == "7d"
        assert body["interactions"]["total"] == 0
        assert body["interactions"]["success_rate"] is None
        assert body["tokens"]["total"] == 0
        assert body["tool_calls"]["total"] == 0
        assert body["estimated_cost_usd"] == 0.0

    def test_full_activity_aggregated_correctly(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        pool = _make_fake_pool(
            q1={"total": 100, "ok": 90, "errors": 8, "in_progress": 2},
            q2={"total_tokens": 25000, "avg_latency": 1500.5, "p50_latency": 1200.0, "p99_latency": 3500.0, "turn_count": 100},
            q3=[
                {"tool_name": "Context 7", "count": 50, "avg_latency": 800.0, "cost_total": 0.25},
                {"tool_name": "Tavily", "count": 30, "avg_latency": 1200.0, "cost_total": 0.15},
            ],
            q4={"total": 20, "ok": 18, "errors": 2, "avg_latency": 400.0},
            q5={"total": 15, "ok": 14, "errors": 1},
        )
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats").json()
        # Interactions
        assert body["interactions"]["total"] == 100
        assert body["interactions"]["success_rate"] == 0.9
        # Tokens
        assert body["tokens"]["total"] == 25000
        # Latency
        assert body["latency_ms"]["p50"] == 1200
        assert body["latency_ms"]["p99"] == 3500
        # Tool calls
        assert body["tool_calls"]["total"] == 80
        assert body["tool_calls"]["by_tool"][0]["name"] == "Context 7"
        assert body["tool_calls"]["by_tool"][0]["count"] == 50
        # Cost
        assert body["estimated_cost_usd"] == 0.40
        # API
        assert body["api_calls"]["ok"] == 18
        # Binding
        assert body["binding_executions"]["total"] == 15

    def test_window_24h_recognized(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        pool = _make_fake_pool()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats?window=24h").json()
        assert body["window"] == "24h"

    def test_window_30d_recognized(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        pool = _make_fake_pool()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats?window=30d").json()
        assert body["window"] == "30d"

    def test_window_all_recognized(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        pool = _make_fake_pool()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats?window=all").json()
        assert body["window"] == "all"

    def test_invalid_window_falls_back_to_7d(self, app_client, monkeypatch):
        _patch_agents(monkeypatch)
        pool = _make_fake_pool()
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats?window=bogus").json()
        assert body["window"] == "7d"

    def test_tool_calls_breakdown_preserves_order(self, app_client, monkeypatch):
        """Backend ordena DESC por count — UI mostra top tools primeiro."""
        _patch_agents(monkeypatch)
        pool = _make_fake_pool(q3=[
            {"tool_name": "A", "count": 100, "avg_latency": 100.0, "cost_total": 1.0},
            {"tool_name": "B", "count": 50, "avg_latency": 200.0, "cost_total": 0.5},
            {"tool_name": "C", "count": 10, "avg_latency": 300.0, "cost_total": 0.1},
        ])
        monkeypatch.setattr("app.core.database._get_pool", lambda: pool)
        body = app_client.get(f"/api/v1/agents/{AGENT_ROW['id']}/stats").json()
        names = [t["name"] for t in body["tool_calls"]["by_tool"]]
        assert names == ["A", "B", "C"]


# ────────────────────────────────────────────────────────────────
# UI smoke
# ────────────────────────────────────────────────────────────────


class TestUISmokeStats:
    def _html(self):
        from pathlib import Path
        return Path("app/templates/pages/agents.html").read_text(encoding="utf-8")

    def test_has_stats_state(self):
        html = self._html()
        assert "previewStats:" in html
        assert "previewStatsWindow:" in html

    def test_fetch_extras_includes_stats(self):
        html = self._html()
        assert "/stats?window=" in html
        # 3 fetches paralelos agora (caps + activity + stats)
        assert "[caps, activity, stats]" in html

    def test_refetch_when_window_changes(self):
        html = self._html()
        assert "_refetchStats()" in html
        # Select de janela com @change
        assert '@change="_refetchStats()"' in html
        assert 'x-model="previewStatsWindow"' in html

    def test_has_stats_section_with_4_cards(self):
        """Grid 2x2 com Invocações + Tokens + Latência + Custo."""
        html = self._html()
        assert "STATS AGREGADOS" in html
        assert ">Invocações<" in html
        assert ">Tokens<" in html
        assert ">Latência<" in html
        assert ">Custo MCP<" in html

    def test_has_breakdown_top_tools(self):
        html = self._html()
        assert "Top Tools" in html
        # Slice 5 pra evitar lista enorme
        assert ".slice(0, 5)" in html

    def test_has_format_helpers(self):
        html = self._html()
        assert "_formatNumber(" in html
        assert "_formatCost(" in html
        assert "_formatPercent(" in html

    def test_format_number_compacts_thousands(self):
        """1234 → 1.2k; 1234567 → 1.2M."""
        html = self._html()
        assert "'k'" in html
        assert "'M'" in html

    def test_success_rate_color_semantics(self):
        """>= 90% verde, >= 50% âmbar, < 50% rose."""
        html = self._html()
        # Cores no x-class do success_rate
        assert "success_rate >= 0.9" in html
        assert "success_rate >= 0.5" in html

    def test_empty_state_has_view_all_action(self):
        """Sem atividade no período → CTA pra ver 'all'."""
        html = self._html()
        assert "Sem atividade no período" in html
        assert "previewStatsWindow='all'" in html
