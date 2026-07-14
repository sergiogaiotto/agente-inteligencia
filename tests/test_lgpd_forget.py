"""Arco LGPD-2 (35.9.0) — direito ao esquecimento por titular.

Decisão do dono: pivô = `customer_ref` explícito → hash na criação (revive a
coluna customer_hash morta). Endpoint POST /privacy/forget (root/admin) apaga
todas as conversas do titular reusando o delete+scrub da retenção (LGPD-1).
Pseudonimização: só o HASH é guardado, nunca o ref cru — nem na auditoria.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import retention
from app.core import interaction_access as ia


class TestHashCustomerRef:
    def test_deterministico_e_normalizado(self):
        h1 = retention.hash_customer_ref("  Cliente@X.com ")
        h2 = retention.hash_customer_ref("cliente@x.com")
        assert h1 == h2 and len(h1) == 64  # sha256 hex, trim+lower

    def test_vazio_e_none(self):
        assert retention.hash_customer_ref("") is None
        assert retention.hash_customer_ref(None) is None
        assert retention.hash_customer_ref("   ") is None

    def test_pivo_contextvar_na_criacao(self):
        ia.set_interaction_customer_for_creation("cpf-123")
        assert ia.interaction_customer_hash_for_creation() == retention.hash_customer_ref("cpf-123")
        ia.set_interaction_customer_for_creation(None)
        assert ia.interaction_customer_hash_for_creation() is None


class FakeCon:
    def __init__(self, batches):
        # batches: lista de listas de ids (uma por iteração do while)
        self._batches = list(batches)
        self.calls = []

    async def fetch(self, sql, *a):
        self.calls.append(("fetch", sql, a))
        return [{"id": i} for i in (self._batches.pop(0) if self._batches else [])]

    async def execute(self, sql, *a):
        self.calls.append(("execute", sql, a))
        if "DELETE FROM interactions" in sql:
            return f"DELETE {len(a[0])}"
        if "UPDATE verifications" in sql:
            return f"UPDATE {len(a[0])}"
        return "DELETE 0"

    def sql(self, frag):
        return [c for c in self.calls if frag in c[1]]


class FakePool:
    def __init__(self, con):
        self._con = con

    def acquire(self):
        con = self._con

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class TestForgetCustomer:
    @pytest.mark.asyncio
    async def test_hash_vazio_noop(self, monkeypatch):
        con = FakeCon([])
        monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool(con))
        out = await retention.forget_customer("")
        assert out == {"deleted": 0, "scrubbed_verifications": 0, "batches": 0}
        assert con.calls == []

    @pytest.mark.asyncio
    async def test_varre_todos_os_lotes(self, monkeypatch):
        # 1º lote cheio (500) → continua; 2º lote parcial (2) → para
        big = [f"i{n}" for n in range(retention._PURGE_BATCH)]
        con = FakeCon([big, ["a", "b"]])
        monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool(con))
        out = await retention.forget_customer("hash-xyz")
        assert out["batches"] == 2
        assert out["deleted"] == retention._PURGE_BATCH + 2
        # busca escopada por customer_hash
        assert con.sql("WHERE customer_hash = $1")
        # reusa o miolo: scrub das verifications antes do delete
        assert con.sql("UPDATE verifications") and con.sql("DELETE FROM interactions")
        # FinOps intocado
        assert not con.sql("invocation_costs")


def _client(monkeypatch, *, role="root", forget_result=None):
    from app.core.auth import require_user
    from app.routes.privacy import router
    from app.core import auth as auth_mod

    async def _fake_user(request=None):
        return {"id": "adm", "role": role}
    monkeypatch.setattr(auth_mod, "require_user", _fake_user)
    from app.core import database as db
    monkeypatch.setattr(db.audit_repo, "create", AsyncMock())
    monkeypatch.setattr("app.core.retention.forget_customer",
                        AsyncMock(return_value=forget_result or
                                  {"deleted": 3, "scrubbed_verifications": 5, "batches": 1}))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestEndpoint:
    def test_root_esquece_e_devolve_contadores(self, monkeypatch):
        c = _client(monkeypatch)
        r = c.post("/api/v1/privacy/forget", json={"customer_ref": "cpf-999"})
        assert r.status_code == 200
        body = r.json()
        assert body["deleted_interactions"] == 3
        assert body["scrubbed_verifications"] == 5
        assert len(body["customer_hash_prefix"]) == 16  # só o prefixo do hash

    def test_admin_tambem_pode(self, monkeypatch):
        c = _client(monkeypatch, role="admin")
        assert c.post("/api/v1/privacy/forget",
                      json={"customer_ref": "x"}).status_code == 200

    def test_comum_403(self, monkeypatch):
        c = _client(monkeypatch, role="comum")
        assert c.post("/api/v1/privacy/forget",
                      json={"customer_ref": "x"}).status_code == 403

    def test_ref_vazio_422(self, monkeypatch):
        c = _client(monkeypatch)
        assert c.post("/api/v1/privacy/forget", json={"customer_ref": "  "}).status_code == 422

    def test_auditoria_nao_grava_o_ref_cru(self, monkeypatch):
        from app.core import database as db
        audit = AsyncMock()
        monkeypatch.setattr(db.audit_repo, "create", audit)
        c = _client(monkeypatch)
        monkeypatch.setattr(db.audit_repo, "create", audit)  # após _client re-set
        c.post("/api/v1/privacy/forget", json={"customer_ref": "SEGREDO-CPF-123"})
        blob = str(audit.call_args)
        assert "SEGREDO-CPF-123" not in blob  # nunca re-introduz o dado apagado


class TestFiacao:
    def test_customer_ref_no_contrato(self):
        from app.models.schemas import PipelineInvokeRequest
        assert "customer_ref" in PipelineInvokeRequest.model_fields

    def test_pontos_de_criacao_incluem_customer_hash(self):
        fsm = Path("app/agents/state_machine.py").read_text(encoding="utf-8")
        eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert '**({"customer_hash": _chash} if _chash else {})' in fsm
        assert '**({"customer_hash": _chash} if _chash else {})' in eng

    def test_engine_threading_e_rotas(self):
        eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert eng.count("set_interaction_customer_for_creation(customer_ref)") == 2
        rotas = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        assert rotas.count("customer_ref=data.customer_ref") == 2  # sync + stream
        assert '"customer_ref": data.customer_ref' in rotas  # async payload
        jobs = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert 'customer_ref=req.get("customer_ref")' in jobs

    def test_router_registrado_e_indice(self):
        main = Path("app/main.py").read_text(encoding="utf-8")
        assert "privacy_router" in main
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        assert any("idx_interactions_customer_hash" in m for m in _IDEMPOTENT_MIGRATIONS)
