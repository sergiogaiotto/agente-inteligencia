"""Persistência de invocações via /workspace/invoke-binding-direct.

User reportou (2026-06-01): invocou um binding (Tavily MCP Server) via form,
viu a resposta no chat, mas ao recarregar a sessão pela sidebar a interação
sumiu. O endpoint nunca gravava no DB — mensagens viviam só no DOM Alpine.

Fix: payload aceita `session_id` e `message`. Quando `message` vem, grava
1 turn na sessão (cria se não existe) usando o mesmo helper que o /chat
declarativo. Round-trip pelo /sessions/{id} GET reproduz o conteúdo.

Cobertura:
- Sem `message`: comportamento legado (não persiste, devolve interaction_id=None)
- Com `message` e session_id vazio: cria sessão nova, grava 2 turns
- Com `message` e session_id válido: adiciona turn na sessão existente
- Smoke do template: loadSession auto-ativa rich view em mensagens estruturadas
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# Reusa fixtures do arquivo gêmeo (skill + agent + tool).
from tests.test_workspace_invoke_direct import (
    AGENT_ROW,
    CTX7_TOOL_ROW,
    SKILL_ROW,
    app_client,  # noqa: F401  — fixture compartilhada via import
)


def _patch_db(monkeypatch):
    async def fake_agent_find(aid):
        return AGENT_ROW if aid == AGENT_ROW["id"] else None

    async def fake_skill_find(sid):
        return SKILL_ROW if sid == SKILL_ROW["id"] else None

    async def fake_tools_find_all(limit=200, offset=0, **filters):
        return [CTX7_TOOL_ROW]

    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
    monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
    monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)


def _patch_repos(monkeypatch, *, existing_session=None, existing_turns=None):
    """Patcha interactions_repo e turns_repo capturando chamadas em listas."""
    calls = {
        "interactions_create": [],
        "interactions_update": [],
        "turns_create": [],
        "interactions_find": [],
    }

    async def fake_int_find(sid):
        calls["interactions_find"].append(sid)
        return existing_session

    async def fake_int_create(row):
        calls["interactions_create"].append(row)
        return row

    async def fake_int_update(sid, patch):
        calls["interactions_update"].append((sid, patch))
        return True

    async def fake_turns_find_all(limit=500, **filters):
        return existing_turns or []

    async def fake_turns_create(row):
        calls["turns_create"].append(row)
        return row

    monkeypatch.setattr("app.routes.workspace.interactions_repo.find_by_id", fake_int_find)
    monkeypatch.setattr("app.routes.workspace.interactions_repo.create", fake_int_create)
    monkeypatch.setattr("app.routes.workspace.interactions_repo.update", fake_int_update)
    monkeypatch.setattr("app.routes.workspace.turns_repo.find_all", fake_turns_find_all)
    monkeypatch.setattr("app.routes.workspace.turns_repo.create", fake_turns_create)
    return calls


def _patch_tool_execution(monkeypatch, result_obj):
    """Faz execute_tool_call devolver um payload arbitrário (str ou dict)."""
    async def fake_exec(*args, **kwargs):
        return result_obj

    monkeypatch.setattr("app.mcp.runtime.execute_tool_call", fake_exec)


def _base_payload(**overrides):
    base = {
        "agent_id": AGENT_ROW["id"],
        "skill_id": SKILL_ROW["id"],
        "binding_kind": "mcp",
        "binding_id": CTX7_TOOL_ROW["id"],
        "operation": "docs",
        "params": {"action": "get_documentation", "subject": "FastAPI"},
    }
    base.update(overrides)
    return base


class TestPersistenceContract:
    def test_no_message_keeps_legacy_ephemeral_behavior(self, app_client, monkeypatch):
        """Sem `message` no payload, não persiste — interaction_id vem None."""
        _patch_db(monkeypatch)
        calls = _patch_repos(monkeypatch)
        _patch_tool_execution(monkeypatch, '{"docs":"hello"}')

        r = app_client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_base_payload(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["interaction_id"] is None
        assert not calls["interactions_create"], "não devia ter criado sessão"
        assert not calls["turns_create"], "não devia ter gravado turn"

    def test_with_message_and_empty_session_creates_new(self, app_client, monkeypatch):
        """`message` + session_id vazio → cria sessão nova e grava 2 turns."""
        _patch_db(monkeypatch)
        calls = _patch_repos(monkeypatch, existing_session=None)
        _patch_tool_execution(monkeypatch, {"results": [{"title": "x"}]})

        r = app_client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_base_payload(message="🛠️ Tavily (search) · query=agentes de IA"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body["interaction_id"]
        assert sid, "deveria devolver um UUID novo"
        # Sessão criada com agent_id correto + title derivado da message
        assert len(calls["interactions_create"]) == 1
        created = calls["interactions_create"][0]
        assert created["id"] == sid
        assert created["agent_id"] == AGENT_ROW["id"]
        assert "Tavily" in created["title"]
        # Dois turns: user_text + output_text (fenced JSON porque result era dict)
        assert len(calls["turns_create"]) == 2
        user_turn, assistant_turn = calls["turns_create"]
        assert user_turn["user_text_redacted"] == "🛠️ Tavily (search) · query=agentes de IA"
        assert user_turn["turn_number"] == 1
        assert assistant_turn["turn_number"] == 2
        assert "```json" in assistant_turn["output_text_redacted"]
        assert '"results"' in assistant_turn["output_text_redacted"]

    def test_with_existing_session_appends_next_turn(self, app_client, monkeypatch):
        """session_id existente → adiciona turn N+1 (não recria sessão)."""
        _patch_db(monkeypatch)
        existing_session = {"id": "s-1", "agent_id": AGENT_ROW["id"], "title": "X"}
        existing_turns = [
            {"turn_number": 1, "user_text_redacted": "oi"},
            {"turn_number": 2, "output_text_redacted": "olá"},
        ]
        calls = _patch_repos(
            monkeypatch,
            existing_session=existing_session,
            existing_turns=existing_turns,
        )
        _patch_tool_execution(monkeypatch, "resposta de tool em string")

        r = app_client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_base_payload(
                session_id="s-1",
                message="🛠️ Nova invocação",
            ),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["interaction_id"] == "s-1"
        # Sessão NÃO foi recriada, mas foi atualizada (ended_at refresh)
        assert not calls["interactions_create"]
        assert calls["interactions_update"], "ended_at deveria ter sido refrescado"
        # Turns gravados com numeração seguindo o existente (max=2 → next=3,4)
        nums = [t["turn_number"] for t in calls["turns_create"]]
        assert nums == [3, 4]
        # Resposta-string fica como string (sem fence) — frontend só fenceia objetos
        assistant_turn = calls["turns_create"][1]
        assert assistant_turn["output_text_redacted"] == "resposta de tool em string"

    def test_persistence_failure_does_not_break_invocation(self, app_client, monkeypatch):
        """Erro na persistência é silencioso — invocação retorna 200 com result."""
        _patch_db(monkeypatch)
        _patch_tool_execution(monkeypatch, '{"x":1}')

        async def fake_int_find(_):
            raise RuntimeError("DB caiu")

        monkeypatch.setattr("app.routes.workspace.interactions_repo.find_by_id", fake_int_find)

        r = app_client.post(
            "/api/v1/workspace/invoke-binding-direct",
            json=_base_payload(session_id="s-1", message="msg"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Tool executou normalmente
        assert body["ok"] is True
        # Persistência falhou silenciosamente
        assert body["interaction_id"] is None


# ─── Smoke do template: round-trip de rich view ao recarregar sessão ──


@pytest.fixture(scope="module")
def workspace_html() -> str:
    p = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html"
    return p.read_text(encoding="utf-8")


class TestRoundTripRichViewOnLoadSession:
    def test_load_session_auto_enables_rich_view_for_structured(self, workspace_html):
        """loadSession agora percorre messages e auto-ativa richViewMsgs[i]
        para mensagens de assistant com conteúdo estruturado (fenced JSON,
        etc) — espelha o caminho de chat/binding-direct."""
        assert "Round-trip de invocações de tool" in workspace_html
        assert "this.isStructuredContent(m.content)" in workspace_html
        assert "this.richViewMsgs[i]=true" in workspace_html

    def test_invoke_binding_payload_includes_session_and_message(self, workspace_html):
        """Frontend passa session_id e message no payload — sem isso o
        backend não tem como saber em qual sessão gravar."""
        assert "session_id: this.currentSessionId || ''" in workspace_html
        assert "message: _userMsgContent" in workspace_html

    def test_invoke_binding_updates_current_session_id_after(self, workspace_html):
        """Após resposta com interaction_id, frontend captura e recarrega
        lista de sessões se for sessão nova (mostra na sidebar)."""
        assert "this.currentSessionId = result.interaction_id" in workspace_html
        assert "this.sessions=(await api.get('/api/v1/workspace/sessions" in workspace_html
