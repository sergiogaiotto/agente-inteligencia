"""Bug 3 de 3 (2026-06-01): TypeError tz-naive vs tz-aware em get_invocation_detail.

User mostrou nos logs:
    asyncpg.exceptions.DataError: ... can't subtract offset-naive and
    offset-aware datetimes ... agents.py:825

O 500 reportado naquele path (`/agents/{id}/stats`) vinha de código antigo
em VPS — `get_agent_stats` já estava corrigido em main (commit 906809a usa
`datetime.now(timezone.utc)`). Mas o handler IRMÃO `get_invocation_detail`
em `agents.py:999` ainda usava `datetime.utcnow()` (tz-naive). Disparava
TypeError quando:

1. A FK direta `api_call_logs.interaction_id` está NULL (rows pré-migração)
2. A interaction tem `ended_at` NULL (sessão ainda aberta)
3. Algum `api_call_logs.created_at` é timestamptz (Postgres default)

Causa-raiz: `datetime.utcnow()` retorna datetime sem tzinfo. Comparar com
`log["created_at"]` (tz-aware do Postgres) levanta TypeError.

Fix: `datetime.now(timezone.utc)` — tz-aware, comparação OK.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_client():
    """TestClient com router de agents."""
    from app.routes.agents import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _patch_get_invocation_detail_deps(monkeypatch, *, ended_at, api_logs_raw):
    """Patcha todas as deps de get_invocation_detail."""
    agent = {"id": "agent-1", "name": "X", "kind": "subagent"}
    interaction = {
        "id": "itx-1",
        "agent_id": "agent-1",
        "started_at": datetime(2026, 5, 25, 0, 0, 0, tzinfo=timezone.utc),
        "ended_at": ended_at,   # None ou datetime
    }

    async def fake_agent_find(aid):
        return agent if aid == "agent-1" else None

    async def fake_int_find(iid):
        return interaction if iid == "itx-1" else None

    async def fake_turns(*args, **kwargs):
        return []

    async def fake_tool_calls(*args, **kwargs):
        return []

    async def fake_api_call_logs(*args, **kwargs):
        # 1ª chamada: por interaction_id (vazia → cai no fallback temporal)
        # 2ª chamada: por agent_id (retorna logs raw)
        if "interaction_id" in kwargs:
            return []
        return api_logs_raw

    async def fake_binding_execs(*args, **kwargs):
        return []

    async def fake_audit(*args, **kwargs):
        return []

    monkeypatch.setattr("app.routes.agents.agents_repo.find_by_id", fake_agent_find)
    monkeypatch.setattr("app.routes.agents.interactions_repo.find_by_id", fake_int_find)
    monkeypatch.setattr("app.routes.agents.turns_repo.find_all", fake_turns)
    monkeypatch.setattr("app.routes.agents.tool_calls_repo.find_all", fake_tool_calls)
    monkeypatch.setattr("app.routes.agents.api_call_logs_repo.find_all", fake_api_call_logs)
    monkeypatch.setattr("app.routes.agents.binding_executions_repo.find_all", fake_binding_execs)
    monkeypatch.setattr("app.routes.agents.audit_repo.find_all", fake_audit)


class TestGetInvocationDetailTzSafe:
    def test_ended_at_null_with_tz_aware_log_does_not_raise(self, app_client, monkeypatch):
        """REGRESSÃO do TypeError: interaction.ended_at é NULL E
        api_call_logs.created_at vem tz-aware do Postgres → handler usava
        datetime.utcnow() (tz-naive) e quebrava. Agora deve responder 200."""
        log_in_window = {
            "id": "log-1",
            "interaction_id": None,  # força o fallback temporal
            "created_at": datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc),
            "agent_id": "agent-1",
        }
        _patch_get_invocation_detail_deps(
            monkeypatch,
            ended_at=None,                        # gatilho do utcnow()
            api_logs_raw=[log_in_window],
        )
        r = app_client.get("/api/v1/agents/agent-1/invocations/itx-1")
        assert r.status_code == 200, r.text
        # E o log dentro da janela foi corretamente incluído
        body = r.json()
        assert len(body["api_call_logs"]) == 1

    def test_ended_at_set_still_works(self, app_client, monkeypatch):
        """Quando ended_at vem do DB (tz-aware), o handler segue funcionando
        normalmente — não é regressão do fix."""
        _patch_get_invocation_detail_deps(
            monkeypatch,
            ended_at=datetime(2026, 5, 25, 23, 59, 59, tzinfo=timezone.utc),
            api_logs_raw=[],
        )
        r = app_client.get("/api/v1/agents/agent-1/invocations/itx-1")
        assert r.status_code == 200, r.text
        assert r.json()["api_call_logs"] == []

    def test_log_outside_window_is_filtered_out(self, app_client, monkeypatch):
        """Janela temporal continua filtrando — o fix não afrouxou nada."""
        log_before = {
            "id": "log-old",
            "interaction_id": None,
            "created_at": datetime(2026, 5, 24, 23, 59, 0, tzinfo=timezone.utc),
            "agent_id": "agent-1",
        }
        _patch_get_invocation_detail_deps(
            monkeypatch,
            ended_at=None,
            api_logs_raw=[log_before],
        )
        r = app_client.get("/api/v1/agents/agent-1/invocations/itx-1")
        assert r.status_code == 200, r.text
        assert r.json()["api_call_logs"] == [], (
            "log fora da janela deveria ter sido filtrado"
        )
