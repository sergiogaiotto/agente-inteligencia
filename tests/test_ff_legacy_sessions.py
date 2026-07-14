"""FF7 (35.7.0) — sessões legadas ESTRITAS + claim cirúrgico pelo root.

Decisão do dono: o `OR owner_user_id IS NULL` do /sessions era o buraco
residual do IDOR — qualquer autenticado via títulos/PII das sessões antigas e,
reusando o session_id, SEQUESTRAVA a conversa (o stamp do 1º acesso a tomava).
Agora: (1) usuário comum vê/acessa SÓ as próprias sessões; legada sem dono é
root-only; (2) id INEXISTENTE continua passando (sessão nova que o caller
cunhou — nasce com dono via #595); (3) processo de CLAIM: o root verifica o
contexto e atribui o dono correto — só quando owner é NULL.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.interaction_access import assert_can_access_interaction


def _wire_row(monkeypatch, row):
    from app.core import database as db
    monkeypatch.setattr(db.interactions_repo, "find_by_id", AsyncMock(return_value=row))


class TestGateEstrito:
    @pytest.mark.asyncio
    async def test_id_inexistente_passa(self, monkeypatch):
        """Sessão NOVA (id cunhado pelo caller) não pode ser bloqueada — o
        invoke a criará já com dono (#595)."""
        _wire_row(monkeypatch, None)
        await assert_can_access_interaction("nova-123", {"id": "u1", "role": "comum"})

    @pytest.mark.asyncio
    async def test_legada_sem_dono_bloqueia_nao_root(self, monkeypatch):
        """ENDURECIMENTO: legada NULL era liberada a todos (leitura + sequestro
        via stamp). Agora 404 idêntico ao de conversa alheia."""
        _wire_row(monkeypatch, {"id": "leg-1", "owner_user_id": None})
        with pytest.raises(HTTPException) as ei:
            await assert_can_access_interaction("leg-1", {"id": "u1", "role": "comum"})
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_legada_sem_dono_root_passa(self, monkeypatch):
        _wire_row(monkeypatch, {"id": "leg-1", "owner_user_id": None})
        await assert_can_access_interaction("leg-1", {"id": "adm", "role": "root"})

    @pytest.mark.asyncio
    async def test_dono_passa_alheio_404(self, monkeypatch):
        _wire_row(monkeypatch, {"id": "s1", "owner_user_id": "u1"})
        await assert_can_access_interaction("s1", {"id": "u1", "role": "comum"})
        with pytest.raises(HTTPException):
            await assert_can_access_interaction("s1", {"id": "OUTRO", "role": "comum"})


class TestListagemEstrita:
    def test_sem_or_null_para_comum(self):
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert "OR owner_user_id IS NULL" not in src  # o buraco saiu
        assert 'conds.append(f"owner_user_id = ${len(params)}")' in src


class TestClaim:
    def _client(self, monkeypatch, *, session_row, role="root", update_res="UPDATE 1"):
        from app.routes.workspace import router
        from app.core import database as db
        # require_role resolve require_user por LOOKUP no módulo auth (não
        # Depends) — armadilha conhecida: monkeypatch, não dependency_overrides.
        from app.core import auth as auth_mod

        async def _fake_user(request=None):
            return {"id": "adm", "role": role}
        monkeypatch.setattr(auth_mod, "require_user", _fake_user)
        monkeypatch.setattr(db.interactions_repo, "find_by_id",
                            AsyncMock(return_value=session_row))
        monkeypatch.setattr(db.users_repo, "find_by_id",
                            AsyncMock(return_value={"id": "u-novo", "username": "x"}))
        monkeypatch.setattr(db.audit_repo, "create", AsyncMock())

        class _Con:
            async def execute(self, sql, *a):
                return update_res

        class _Pool:
            def acquire(self):
                con = _Con()

                class _Ctx:
                    async def __aenter__(self):
                        return con

                    async def __aexit__(self, *b):
                        return False
                return _Ctx()
        monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool())
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_root_atribui_legada(self, monkeypatch):
        c = self._client(monkeypatch, session_row={"id": "leg-1", "owner_user_id": None})
        r = c.post("/api/v1/workspace/sessions/leg-1/claim", json={"user_id": "u-novo"})
        assert r.status_code == 200
        assert r.json()["owner_user_id"] == "u-novo"

    def test_nao_root_403(self, monkeypatch):
        c = self._client(monkeypatch, session_row={"id": "leg-1", "owner_user_id": None},
                         role="comum")
        r = c.post("/api/v1/workspace/sessions/leg-1/claim", json={"user_id": "u-novo"})
        assert r.status_code == 403

    def test_ja_com_dono_409(self, monkeypatch):
        c = self._client(monkeypatch, session_row={"id": "s1", "owner_user_id": "u1"})
        r = c.post("/api/v1/workspace/sessions/s1/claim", json={"user_id": "u-novo"})
        assert r.status_code == 409

    def test_inexistente_404_e_sem_user_422(self, monkeypatch):
        c = self._client(monkeypatch, session_row=None)
        assert c.post("/api/v1/workspace/sessions/x/claim",
                      json={"user_id": "u-novo"}).status_code == 404
        assert c.post("/api/v1/workspace/sessions/x/claim",
                      json={}).status_code == 422

    def test_corrida_update_zero_409(self, monkeypatch):
        # entre o SELECT e o UPDATE alguém carimbou → WHERE IS NULL não afeta linha
        c = self._client(monkeypatch, session_row={"id": "leg-1", "owner_user_id": None},
                         update_res="UPDATE 0")
        r = c.post("/api/v1/workspace/sessions/leg-1/claim", json={"user_id": "u-novo"})
        assert r.status_code == 409
