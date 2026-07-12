"""P1: atribuição por-key na metadata da interação (F12).

A auditoria já registra api_key_id; aqui propagamos p/ a metadata da interação
(que a observabilidade/UI lê). Merge, best-effort, só p/ chamadas via key.
"""
from __future__ import annotations

import json

import pytest

from app.routes import pipelines as pl


class _State:
    pass


class _Req:
    def __init__(self, api_key_id=None, api_key_name=None):
        self.state = _State()
        if api_key_id is not None:
            self.state.api_key_id = api_key_id
        if api_key_name is not None:
            self.state.api_key_name = api_key_name


class TestAttribution:
    @pytest.mark.asyncio
    async def test_cookie_principal_no_update(self, monkeypatch):
        called = {"n": 0}

        async def _upd(*a, **k):
            called["n"] += 1

        monkeypatch.setattr(pl.interactions_repo, "update", _upd)
        await pl._attribute_interaction_to_key(None, None, "int1")  # sem api_key_id
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_no_interaction_id_no_update(self, monkeypatch):
        called = {"n": 0}

        async def _upd(*a, **k):
            called["n"] += 1

        monkeypatch.setattr(pl.interactions_repo, "update", _upd)
        await pl._attribute_interaction_to_key("k1", None, None)
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_key_writes_attribution(self, monkeypatch):
        captured = {}

        async def _find(_id):
            return {"id": _id, "metadata": "{}"}

        async def _upd(_id, changes):
            captured.update(changes)

        monkeypatch.setattr(pl.interactions_repo, "find_by_id", _find)
        monkeypatch.setattr(pl.interactions_repo, "update", _upd)
        await pl._attribute_interaction_to_key("k1", "frontend-x", "int1")
        md = json.loads(captured["metadata"])
        assert md["via"] == "api_key"
        assert md["api_key_id"] == "k1"
        assert md["api_key_name"] == "frontend-x"

    @pytest.mark.asyncio
    async def test_merge_preserves_existing_metadata(self, monkeypatch):
        captured = {}

        async def _find(_id):
            return {"id": _id, "metadata": json.dumps({"foo": "bar"})}

        async def _upd(_id, changes):
            captured.update(changes)

        monkeypatch.setattr(pl.interactions_repo, "find_by_id", _find)
        monkeypatch.setattr(pl.interactions_repo, "update", _upd)
        await pl._attribute_interaction_to_key("k1", None, "int1")
        md = json.loads(captured["metadata"])
        assert md["foo"] == "bar"  # não clobbera
        assert md["api_key_id"] == "k1"

    @pytest.mark.asyncio
    async def test_failure_is_swallowed(self, monkeypatch):
        async def _boom(_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(pl.interactions_repo, "find_by_id", _boom)
        # best-effort: NÃO pode levantar (o invoke já executou)
        await pl._attribute_interaction_to_key("k1", None, "int1")
