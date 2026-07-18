"""Cockpit OPA (62.0.0) — resolução do usuário atuante + helpers do cliente.

Trava o fix do engine: papel/status REAIS (antes HARDCODED "operator"/"active")
via resolve_opa_user + map_platform_role_to_opa; e o contrato dos helpers de
read/simulate que a rota de governança consome. Ver project_opa_cockpit_handoff.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core import opa_client


# ─── Mapa papel-plataforma → papel OPA ───────────────────────────────────────
class TestRoleMapping:
    def test_privilegiados_viram_admin(self):
        for r in ("root", "admin", "governanca", "ROOT", " Admin "):
            assert opa_client.map_platform_role_to_opa(r) == "admin", r

    def test_comum_vira_operator(self):
        assert opa_client.map_platform_role_to_opa("comum") == "operator"

    def test_default_nao_regressivo_operator(self):
        # ausente/desconhecido → "operator" (o que o engine assumia hardcoded).
        for r in (None, "", "  ", "viewer", "qualquer"):
            assert opa_client.map_platform_role_to_opa(r) == "operator", r


# ─── Resolução do usuário atuante (o coração do fix do engine) ───────────────
class _FakeUsers:
    def __init__(self, row):
        self._row = row
        self.calls = 0

    async def find_by_id(self, uid):
        self.calls += 1
        return self._row


class _BoomUsers:
    async def find_by_id(self, uid):
        raise RuntimeError("db down")


class TestResolveOpaUser:
    @pytest.mark.asyncio
    async def test_sem_owner_default_seguro(self):
        assert await opa_client.resolve_opa_user(None) == {"status": "active", "role": "operator", "clearance": "internal"}
        assert await opa_client.resolve_opa_user("   ") == {"status": "active", "role": "operator", "clearance": "internal"}

    @pytest.mark.asyncio
    async def test_owner_admin_destrava_high(self, monkeypatch):
        import app.core.database as DB
        fake = _FakeUsers({"id": "u1", "role": "admin", "status": "active"})
        monkeypatch.setattr(DB, "users_repo", fake)
        assert await opa_client.resolve_opa_user("u1") == {"status": "active", "role": "admin", "clearance": "internal"}
        assert fake.calls == 1  # 1 lookup por PK

    @pytest.mark.asyncio
    async def test_owner_com_clearance_no_row(self, monkeypatch):
        # 64.0.0: o clearance sai do MESMO lookup (sem query extra).
        import app.core.database as DB
        monkeypatch.setattr(DB, "users_repo", _FakeUsers({"id": "u9", "role": "comum", "status": "active", "clearance": "Confidential"}))
        r = await opa_client.resolve_opa_user("u9")
        assert r == {"status": "active", "role": "operator", "clearance": "confidential"}

    @pytest.mark.asyncio
    async def test_owner_comum_suspenso(self, monkeypatch):
        import app.core.database as DB
        monkeypatch.setattr(DB, "users_repo", _FakeUsers({"id": "u2", "role": "comum", "status": "Suspended"}))
        assert await opa_client.resolve_opa_user("u2") == {"status": "suspended", "role": "operator", "clearance": "internal"}

    @pytest.mark.asyncio
    async def test_owner_inexistente_default(self, monkeypatch):
        import app.core.database as DB
        monkeypatch.setattr(DB, "users_repo", _FakeUsers(None))
        assert await opa_client.resolve_opa_user("ghost") == {"status": "active", "role": "operator", "clearance": "internal"}

    @pytest.mark.asyncio
    async def test_lookup_falha_nao_propaga(self, monkeypatch):
        import app.core.database as DB
        monkeypatch.setattr(DB, "users_repo", _BoomUsers())
        assert await opa_client.resolve_opa_user("u3") == {"status": "active", "role": "operator", "clearance": "internal"}


# ─── Helpers HTTP do cockpit (health / list / simulate) ──────────────────────
class _FakeResp:
    def __init__(self, status_code=200, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, get_resp=None, post_resp=None, exc=None):
        self._get, self._post, self._exc = get_resp, post_resp, exc

    async def get(self, url):
        if self._exc:
            raise self._exc
        return self._get

    async def post(self, url, json=None):
        if self._exc:
            raise self._exc
        return self._post


def _patch_client(monkeypatch, client):
    async def _get():
        return client
    monkeypatch.setattr(opa_client, "_get_client", _get)


class TestClientHelpers:
    @pytest.mark.asyncio
    async def test_server_health_ok_e_falha(self, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(get_resp=_FakeResp(200)))
        assert await opa_client.server_health() is True
        _patch_client(monkeypatch, _FakeClient(exc=RuntimeError("net")))
        assert await opa_client.server_health() is False

    @pytest.mark.asyncio
    async def test_list_policies_ok_e_none(self, monkeypatch):
        payload = {"result": [{"id": "policies/interaction.rego", "raw": "package interaction"}]}
        _patch_client(monkeypatch, _FakeClient(get_resp=_FakeResp(200, payload)))
        pol = await opa_client.list_policies()
        assert pol and pol[0]["id"] == "policies/interaction.rego"
        _patch_client(monkeypatch, _FakeClient(exc=RuntimeError("net")))
        assert await opa_client.list_policies() is None

    @pytest.mark.asyncio
    async def test_simulate_allow_result_e_erro(self, monkeypatch):
        _patch_client(monkeypatch, _FakeClient(post_resp=_FakeResp(200, {"result": True})))
        d = await opa_client.simulate("tool_invocation", "allow", {})
        assert d["allow"] is True and d["source"] == "opa"
        # deny real (result=false) ≠ erro
        _patch_client(monkeypatch, _FakeClient(post_resp=_FakeResp(200, {"result": False})))
        d2 = await opa_client.simulate("tool_invocation", "allow", {})
        assert d2["allow"] is False and d2["source"] == "opa"
        # OPA fora do ar → source="error", allow=None (nunca propaga)
        _patch_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("net")))
        d3 = await opa_client.simulate("tool_invocation", "allow", {})
        assert d3["allow"] is None and d3["source"] == "error" and "error" in d3


class TestClientRebuildOnTimeout:
    """O timeout é fixado na construção do AsyncClient. Como o cockpit altera
    opa_timeout_seconds em runtime, _get_client precisa reconstruir o cliente
    quando o timeout muda — senão o valor novo só valeria após restart."""

    @pytest.mark.asyncio
    async def test_reconstroi_quando_timeout_muda(self, monkeypatch):
        import types
        await opa_client.close()  # estado limpo
        cfg = types.SimpleNamespace(opa_url="http://opa:8181", opa_timeout_seconds=2.0)
        monkeypatch.setattr(opa_client, "get_settings", lambda: cfg)
        try:
            c1 = await opa_client._get_client()
            assert opa_client._client_timeout == 2.0
            assert await opa_client._get_client() is c1  # timeout inalterado → reusa
            cfg.opa_timeout_seconds = 5.0
            c3 = await opa_client._get_client()
            assert c3 is not c1 and opa_client._client_timeout == 5.0
        finally:
            await opa_client.close()


# ─── Fiação dos call sites no engine (o fix, que o unit do helper NÃO cobre) ──
class TestEngineWiring:
    """Trava os 2 PEPs do OPA em app/agents/engine.py. Reverter para o hardcoded
    ('operator'/'active') — que reintroduz o bug de tools sensitivity:high
    filtradas em silêncio — deve QUEBRAR aqui. A revisão adversarial demonstrou
    que, sem esta guarda, esse revert passa com toda a suíte verde."""
    ENGINE = Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"

    def test_call_sites_usam_usuario_real(self):
        src = self.ENGINE.read_text(encoding="utf-8")
        assert "resolve_opa_user(owner_user_id)" in src  # resolve do dono da sessão
        assert 'user_role = _opa_user["role"]' in src     # PEP de tools usa papel real
        assert '"status": _opa_user["status"]' in src      # PolicyCheck usa status real

    def test_hardcoded_removido(self):
        src = self.ENGINE.read_text(encoding="utf-8")
        assert 'user_role = "operator"' not in src
        assert '"user": {"status": "active"}' not in src
