"""Onda 6 — fast-follow do IDOR (33.15.0): leituras + chat.

Estende o gate de posse (do #581) às superfícies que faltavam: list_sessions
(listava sessões de TODOS), get_invocation_detail (lia turns alheios), e o chat/
chat_stream (multi-turno que reinjeta histórico). O gate/carimbo em si já é
testado em test_idor_interaction_owner; aqui cobrimos o escopo do list_sessions
(funcional) e a fiação das rotas.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _FakeCon:
    def __init__(self, sink):
        self.sink = sink

    async def fetch(self, sql, *params):
        self.sink["fetch_sql"] = sql
        self.sink["fetch_params"] = params
        return []

    async def fetchval(self, sql, *params):
        self.sink["count_sql"] = sql
        return 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, sink):
        self.sink = sink

    def acquire(self):
        return _FakeCon(self.sink)


class TestListSessionsScoping:
    @pytest.mark.asyncio
    async def test_comum_escopa_por_dono(self, monkeypatch):
        from app.routes.workspace import list_sessions
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await list_sessions(user={"id": "u1", "role": "comum"})
        assert "owner_user_id = $1 OR owner_user_id IS NULL" in sink["fetch_sql"]
        assert sink["fetch_params"][0] == "u1"   # 1º param = o dono
        assert "owner_user_id" in sink["count_sql"]

    @pytest.mark.asyncio
    async def test_root_ve_todas(self, monkeypatch):
        from app.routes.workspace import list_sessions
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await list_sessions(user={"id": "root1", "role": "root"})
        assert "owner_user_id" not in sink["fetch_sql"]   # root: sem filtro de dono

    @pytest.mark.asyncio
    async def test_comum_com_agent_id_combina_filtros(self, monkeypatch):
        from app.routes.workspace import list_sessions
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await list_sessions(agent_id="ag-9", user={"id": "u1", "role": "comum"})
        assert "owner_user_id = $1" in sink["fetch_sql"]
        assert "agent_id = $2" in sink["fetch_sql"]
        assert sink["fetch_params"][:2] == ("u1", "ag-9")


class TestRoutesWired:
    def test_get_invocation_detail_gate(self):
        src = Path("app/routes/agents.py").read_text(encoding="utf-8")
        assert "async def get_invocation_detail(agent_id: str, interaction_id: str, request: Request)" in src
        assert "assert_can_access_interaction(interaction_id, getattr(request.state" in src

    def test_chat_precheck_e_stamp(self):
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        # pre-check aparece no chat E no chat_stream
        assert src.count("assert_can_access_interaction(data.session_id, user)") >= 2
        assert 'stamp_interaction_owner(iid, user.get("id"))' in src

    def test_chat_stream_stamp_do_resultado(self):
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert 'stamp_interaction_owner((_res or {}).get("interaction_id"), user.get("id"))' in src

    def test_list_sessions_exige_auth(self):
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert "async def list_sessions(agent_id: str = None, limit: int = 30, offset: int = 0," in src
        assert "user: dict = Depends(require_user)" in src
