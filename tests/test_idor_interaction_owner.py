"""Onda 6 — IDOR do interaction_id (owner_user_id + gate de posse, 33.13.0).

Cobre o gate (dono/alheio/legado/root/sem-id), o carimbo, o schema (coluna no
DDL base, índice só no Alembic), a revisão 0003 e a aplicação nas rotas.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core import interaction_access as ia


class _FakeCon:
    def __init__(self, owner, sink):
        self._owner = owner
        self.sink = sink

    async def fetchval(self, sql, *params):
        self.sink["fetch_params"] = params
        return self._owner

    async def execute(self, sql, *params):
        self.sink["exec_sql"] = sql
        self.sink["exec_params"] = params

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, owner, sink):
        self._owner = owner
        self.sink = sink

    def acquire(self):
        return _FakeCon(self._owner, self.sink)


def _patch_pool(monkeypatch, owner):
    sink: dict = {}
    monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(owner, sink))
    return sink


class TestAccessGate:
    @pytest.mark.asyncio
    async def test_dono_passa(self, monkeypatch):
        _patch_pool(monkeypatch, owner="user-A")
        await ia.assert_can_access_interaction("int-1", {"id": "user-A"})  # não levanta

    @pytest.mark.asyncio
    async def test_alheio_404(self, monkeypatch):
        _patch_pool(monkeypatch, owner="user-A")
        with pytest.raises(HTTPException) as ei:
            await ia.assert_can_access_interaction("int-1", {"id": "user-B"})
        assert ei.value.status_code == 404  # 404 (não 403) — não confirma existência

    @pytest.mark.asyncio
    async def test_legada_sem_dono_passa(self, monkeypatch):
        _patch_pool(monkeypatch, owner=None)  # interaction sem owner (legada/inexistente)
        await ia.assert_can_access_interaction("int-1", {"id": "user-B"})  # não levanta

    @pytest.mark.asyncio
    async def test_root_bypassa(self, monkeypatch):
        _patch_pool(monkeypatch, owner="user-A")
        await ia.assert_can_access_interaction("int-1", {"id": "user-B", "role": "root"})

    @pytest.mark.asyncio
    async def test_admin_NAO_bypassa(self, monkeypatch):
        _patch_pool(monkeypatch, owner="user-A")
        with pytest.raises(HTTPException):
            await ia.assert_can_access_interaction("int-1", {"id": "user-B", "role": "admin"})

    @pytest.mark.asyncio
    async def test_sem_interaction_id_noop(self, monkeypatch):
        sink = _patch_pool(monkeypatch, owner="user-A")
        await ia.assert_can_access_interaction(None, {"id": "user-B"})
        await ia.assert_can_access_interaction("", {"id": "user-B"})
        assert sink == {}  # nem consulta o banco


class TestStamp:
    @pytest.mark.asyncio
    async def test_carimba_update_por_id_null(self, monkeypatch):
        sink = _patch_pool(monkeypatch, owner=None)
        await ia.stamp_interaction_owner("int-1", "user-A")
        assert "UPDATE interactions SET owner_user_id" in sink["exec_sql"]
        assert "owner_user_id IS NULL" in sink["exec_sql"]  # não sobrescreve dono
        assert sink["exec_params"] == ("user-A", "int-1")

    @pytest.mark.asyncio
    async def test_noop_sem_id_ou_user(self, monkeypatch):
        sink = _patch_pool(monkeypatch, owner=None)
        await ia.stamp_interaction_owner(None, "user-A")
        await ia.stamp_interaction_owner("int-1", None)
        assert sink == {}


class TestSchema:
    def test_ddl_base_tem_owner(self):
        from app.core.database import SCHEMA
        assert "owner_user_id TEXT" in SCHEMA

    def test_indice_owner_so_no_alembic(self):
        from app.core.database import SCHEMA, _IDEMPOTENT_MIGRATIONS
        assert "CREATE INDEX IF NOT EXISTS idx_interactions_owner" not in SCHEMA
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        assert "interactions ADD COLUMN IF NOT EXISTS owner_user_id" not in migs


def _load_rev0003():
    p = Path("alembic/versions/0003_interactions_owner_user_id.py")
    spec = importlib.util.spec_from_file_location("rev0003_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAlembic0003:
    def test_chain_0002_para_0003(self):
        mod = _load_rev0003()
        assert mod.revision == "0003_interactions_owner_user_id"
        assert mod.down_revision == "0002_verifications_gold_case_id"

    def test_upgrade_downgrade(self, monkeypatch):
        import alembic.op
        calls: list[str] = []
        monkeypatch.setattr(alembic.op, "execute", lambda sql: calls.append(sql))
        mod = _load_rev0003()
        mod.upgrade()
        up = "\n".join(calls)
        assert "ADD COLUMN IF NOT EXISTS owner_user_id" in up
        assert "CREATE INDEX IF NOT EXISTS idx_interactions_owner" in up
        calls.clear()
        mod.downgrade()
        down = "\n".join(calls)
        assert "DROP INDEX IF EXISTS idx_interactions_owner" in down
        assert "DROP COLUMN IF EXISTS owner_user_id" in down


class TestRoutesWired:
    def test_invoke_pipeline_gate_e_carimbo(self):
        src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        assert "assert_can_access_interaction(data.session_id, user)" in src
        assert 'stamp_interaction_owner(result.get("interaction_id"), user.get("id"))' in src

    def test_invoke_agent_gate_e_carimbo(self):
        src = Path("app/routes/agents.py").read_text(encoding="utf-8")
        assert "assert_can_access_interaction(data.session_id, _caller)" in src
        assert "stamp_interaction_owner(pipe_result.get(\"interaction_id\"), _caller.get(\"id\"))" in src
        assert "stamp_interaction_owner(result.get(\"interaction_id\"), _caller.get(\"id\"))" in src

    def test_get_session_autentica_e_checa(self):
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert "async def get_session(session_id: str, user: dict = Depends(require_user))" in src
        assert "assert_can_access_interaction(session_id, user)" in src
