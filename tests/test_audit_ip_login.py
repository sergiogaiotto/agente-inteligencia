"""Auditoria com IP + login/logout auditados (35.11.0, médios do roadmap).

- audit_log ganha coluna `ip` (SCHEMA + migração idempotente).
- AuditRepository injeta ip (contextvar com a resolução anti-spoof #560) e
  actor-fallback (user_id_var) em TODOS os ~37 call sites sem tocar nenhum;
  tolerante (re-tenta sem as keys injetadas se o INSERT falhar).
- login_success / login_failed (anti-enumeração: sem username tentado, sem
  distinguir inexistente×senha-errada) / logout auditados, best-effort.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.logging_setup import client_ip_var, user_id_var


class TestAuditRepositoryInjection:
    def _repo(self):
        from app.core.database import AuditRepository
        return AuditRepository("audit_log")

    @pytest.mark.asyncio
    async def test_injeta_ip_e_actor_do_contexto(self, monkeypatch):
        repo = self._repo()
        captured = {}

        async def fake_create(self, data):
            captured.update(data)
            return data
        from app.core.database import Repository
        monkeypatch.setattr(Repository, "create", fake_create)
        t1 = client_ip_var.set("203.0.113.7")
        t2 = user_id_var.set("u-ctx")
        try:
            await repo.create({"entity_type": "x", "entity_id": "1", "action": "a"})
        finally:
            client_ip_var.reset(t1)
            user_id_var.reset(t2)
        assert captured["ip"] == "203.0.113.7"
        assert captured["actor"] == "u-ctx"  # fallback do contexto

    @pytest.mark.asyncio
    async def test_nao_sobrescreve_actor_explicito_nem_inventa_ip(self, monkeypatch):
        repo = self._repo()
        captured = {}

        async def fake_create(self, data):
            captured.update(data)
            return data
        from app.core.database import Repository
        monkeypatch.setattr(Repository, "create", fake_create)
        # fora de request: contextvars no default ("")
        await repo.create({"entity_type": "x", "entity_id": "1",
                           "action": "a", "actor": "quem-chamou"})
        assert "ip" not in captured          # não inventa IP em background
        assert captured["actor"] == "quem-chamou"  # explícito vence

    @pytest.mark.asyncio
    async def test_fallback_sem_keys_injetadas_quando_insert_falha(self, monkeypatch):
        """Migração fail-open pode deixar DB velho sem a coluna ip — o evento
        NUNCA se perde: re-tenta sem o contexto injetado."""
        repo = self._repo()
        calls = []

        async def fake_create(self, data):
            calls.append(dict(data))
            if "ip" in data:
                raise RuntimeError("column ip does not exist")
            return data
        from app.core.database import Repository
        monkeypatch.setattr(Repository, "create", fake_create)
        t1 = client_ip_var.set("203.0.113.7")
        try:
            await repo.create({"entity_type": "x", "entity_id": "1", "action": "a"})
        finally:
            client_ip_var.reset(t1)
        assert len(calls) == 2
        assert "ip" in calls[0] and "ip" not in calls[1]


def _login_client(monkeypatch, *, user=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.users import router
    from app.core import database as db
    monkeypatch.setattr(db.users_repo, "find_all",
                        AsyncMock(return_value=[user] if user else []))
    audit = AsyncMock()
    monkeypatch.setattr(db.audit_repo, "create", audit)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), audit


class TestLoginLogoutAuditados:
    def test_login_failed_anti_enumeracao(self, monkeypatch):
        c, audit = _login_client(monkeypatch, user=None)
        r = c.post("/api/v1/users/login", json={"username": "alvo-sondado", "password": "x"})
        assert r.status_code == 401
        blob = str(audit.call_args)
        assert "login_failed" in blob
        # anti-enumeração: o username tentado NÃO entra no log de auditoria
        assert "alvo-sondado" not in blob
        assert "invalid_credentials" in blob

    def test_login_success_auditado(self, monkeypatch):
        from app.core.auth import hash_password
        user = {"id": "u1", "username": "sergio", "status": "active",
                "password_hash": hash_password("s3nh4")}
        c, audit = _login_client(monkeypatch, user=user)
        r = c.post("/api/v1/users/login", json={"username": "sergio", "password": "s3nh4"})
        assert r.status_code == 200
        blob = str(audit.call_args)
        assert "login_success" in blob and "'actor': 'u1'" in blob

    def test_logout_auditado_mesmo_sem_cookie(self, monkeypatch):
        c, audit = _login_client(monkeypatch)
        r = c.post("/api/v1/users/logout")
        assert r.status_code == 200
        assert "logout" in str(audit.call_args)

    def test_falha_de_auditoria_nao_derruba_o_login(self, monkeypatch):
        from app.core.auth import hash_password
        user = {"id": "u1", "username": "sergio", "status": "active",
                "password_hash": hash_password("s3nh4")}
        c, audit = _login_client(monkeypatch, user=user)
        audit.side_effect = RuntimeError("db down")
        r = c.post("/api/v1/users/login", json={"username": "sergio", "password": "s3nh4"})
        assert r.status_code == 200  # auth sobrevive à auditoria quebrada


class TestFiacao:
    def test_coluna_ip_schema_e_migracao(self):
        from app.core.database import SCHEMA, _IDEMPOTENT_MIGRATIONS
        assert "ip TEXT" in SCHEMA
        assert any("audit_log ADD COLUMN IF NOT EXISTS ip" in m
                   for m in _IDEMPOTENT_MIGRATIONS)

    def test_audit_repo_e_a_subclasse(self):
        from app.core.database import audit_repo, AuditRepository
        assert isinstance(audit_repo, AuditRepository)

    def test_middleware_seta_e_reseta_o_ip(self):
        src = Path("app/core/request_context.py").read_text(encoding="utf-8")
        assert "client_ip_var.set(resolve_client_ip(request)" in src
        assert "client_ip_var.reset(ip_tok)" in src

    def test_alias_publico_da_resolucao(self):
        from app.core.ratelimit import resolve_client_ip, _resolve_client_ip
        assert resolve_client_ip is _resolve_client_ip
