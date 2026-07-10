"""PR8c — federação consumer/egress + guarda SSRF.

Cobre: `app/core/ssrf.py` (bloqueia loopback/privado/link-local/metadata/mixed/
http/esquema/unresolvable), o módulo `federation_egress` (pull/sync/invoke — com
round-trip assinatura egress→ingress) e as rotas sync (root-only) e remote-invoke.
Sem rede/Postgres: socket.getaddrinfo, _get_json, pool e repos monkeypatchados.
"""
from __future__ import annotations

import asyncio
import socket as _socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.catalog.federation_egress as egress
import app.core.ssrf as ssrf
from app.a2a.protocol import Envelope
from app.core.auth import require_user
from app.core.crypto import encrypt_secret
from app.core.ssrf import SSRFError
from app.routes import federation as fed_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _patch_resolve(monkeypatch, ips):
    def fake(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake)


# ── SSRF guard ──
class TestSSRF:
    def test_public_https_ok(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        assert ssrf.validate_public_url("https://peer.example/x") == "https://peer.example/x"

    def test_loopback_blocked(self, monkeypatch):
        _patch_resolve(monkeypatch, ["127.0.0.1"])
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("https://peer.example")

    def test_private_blocked(self, monkeypatch):
        for ip in ("10.0.0.1", "192.168.1.5", "172.16.0.9"):
            _patch_resolve(monkeypatch, [ip])
            with pytest.raises(SSRFError):
                ssrf.validate_public_url("https://peer.example")

    def test_metadata_link_local_blocked(self, monkeypatch):
        _patch_resolve(monkeypatch, ["169.254.169.254"])
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("https://peer.example")

    def test_ipv6_loopback_and_mapped_blocked(self, monkeypatch):
        for ip in ("::1", "::ffff:127.0.0.1", "fe80::1", "fc00::1", "::ffff:169.254.169.254"):
            _patch_resolve(monkeypatch, [ip])
            with pytest.raises(SSRFError):
                ssrf.validate_public_url("https://peer.example")

    def test_ipv6_public_ok(self, monkeypatch):
        _patch_resolve(monkeypatch, ["2606:4700:4700::1111"])  # público
        assert ssrf.validate_public_url("https://peer.example")

    def test_mixed_ips_blocked(self, monkeypatch):
        # um público + um privado → bloqueia (defesa contra DNS misto)
        _patch_resolve(monkeypatch, ["93.184.216.34", "10.0.0.1"])
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("https://peer.example")

    def test_http_blocked_by_default_allowed_with_flag(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("http://peer.example")
        assert ssrf.validate_public_url("http://peer.example", allow_http=True)

    def test_bad_scheme(self):
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("ftp://peer.example")
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("file:///etc/passwd")

    def test_no_host(self):
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("https://")

    def test_unresolvable(self, monkeypatch):
        def boom(*a, **k):
            raise _socket.gaierror()
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", boom)
        with pytest.raises(SSRFError):
            ssrf.validate_public_url("https://nope.invalid")


# ── pull_manifest ──
class TestPullManifest:
    def test_valid(self, monkeypatch):
        monkeypatch.setattr(egress, "_get_json", _async({"capabilities": [], "workspace": "r"}))
        monkeypatch.setattr(egress, "_dev_allow_http", _async(False))
        res = asyncio.run(egress.pull_manifest({"base_url": "https://peer.example"}))
        assert res["capabilities"] == []

    def test_no_base_url(self):
        with pytest.raises(SSRFError):
            asyncio.run(egress.pull_manifest({"base_url": ""}))

    def test_invalid_shape(self, monkeypatch):
        monkeypatch.setattr(egress, "_get_json", _async({"nope": 1}))
        monkeypatch.setattr(egress, "_dev_allow_http", _async(False))
        with pytest.raises(ValueError):
            asyncio.run(egress.pull_manifest({"base_url": "https://peer.example"}))


# ── sync_remote_entries ──
class TestSyncRemoteEntries:
    def _fake_pool(self, monkeypatch):
        captured = []

        class C:
            async def execute(self, sql, *p):
                captured.append(p)

        class A:
            def __init__(s, c): s.c = c
            async def __aenter__(s): return s.c
            async def __aexit__(s, *a): return False

        class P:
            def __init__(s, c): s.c = c
            def acquire(s): return A(s.c)

        monkeypatch.setattr(egress, "_get_pool", lambda: P(C()))
        monkeypatch.setattr(egress, "local_workspace", _async("local"))
        return captured

    def test_registers_valid_skips_invalid(self, monkeypatch):
        captured = self._fake_pool(monkeypatch)
        manifest = {"capabilities": [
            {"urn": "urn:maestro:remote:pipeline:good:1.0.0", "name": "Good", "kind": "pipeline", "version": "1.0.0"},
            {"urn": "garbage", "name": "BadUrn", "kind": "pipeline"},
            {"urn": "urn:maestro:local:pipeline:self:1.0.0", "name": "Self", "kind": "pipeline"},  # próprio ws
            {"urn": "urn:maestro:remote:pipeline:evil:1.0.0", "name": "BadKind", "kind": "malware"},
        ]}
        monkeypatch.setattr(egress, "pull_manifest", _async(manifest))
        res = asyncio.run(egress.sync_remote_entries({"id": "p1", "workspace": "remote"}, "owner1"))
        assert res["registered"] == 1 and res["skipped"] == 3
        assert len(captured) == 1  # apenas 1 INSERT (a capability válida)


# ── invoke_remote (assinatura egress aceita pelo ingress) ──
class TestInvokeRemote:
    def test_signs_and_posts_valid(self, monkeypatch):
        captured = {}

        async def fake_get_json(method, url, *, allow_http, json_body=None):
            captured.update(method=method, url=url, body=json_body)
            return {"status": "completed", "output": "remote ok", "total_cost_usd": 0.02}

        monkeypatch.setattr(egress, "_get_json", fake_get_json)
        monkeypatch.setattr(egress, "local_workspace", _async("local"))
        monkeypatch.setattr(egress, "_dev_allow_http", _async(False))
        secret = "peer-secret-123"
        peer = {"id": "p1", "base_url": "https://peer.example", "shared_secret": encrypt_secret(secret), "workspace": "remote"}
        entry = {"id": "e1", "remote_urn": "urn:maestro:remote:pipeline:x:1.0.0", "urn": "urn:maestro:remote:pipeline:x:1.0.0"}
        res = asyncio.run(egress.invoke_remote(entry, "oi remoto", peer))
        assert res["output"] == "remote ok"
        assert captured["method"] == "POST" and captured["url"].endswith("/api/v1/federation/invoke")
        # a assinatura enviada DEVE verificar com o segredo do peer (round-trip ingress)
        body = captured["body"]
        env = Envelope.from_dict(body["envelope"])
        assert env.verify_hmac(secret, body["signature"])
        assert env.context["user_input"] == "oi remoto"
        assert env.origin_workspace == "local"
        assert env.target_skill_urn == "urn:maestro:remote:pipeline:x:1.0.0"

    def test_no_base_url_raises(self):
        peer = {"id": "p1", "base_url": "", "shared_secret": encrypt_secret("s")}
        with pytest.raises(SSRFError):
            asyncio.run(egress.invoke_remote({"urn": "u", "remote_urn": "u"}, "hi", peer))

    def test_no_secret_raises(self, monkeypatch):
        monkeypatch.setattr(egress, "local_workspace", _async("local"))
        peer = {"id": "p1", "base_url": "https://peer.example", "shared_secret": ""}
        with pytest.raises(ValueError):
            asyncio.run(egress.invoke_remote({"urn": "u", "remote_urn": "u"}, "hi", peer))


# ── _get_json: não-200 inclui o detail do peer na razão (A2A-2) ──
class TestGetJsonErrorReason:
    def _fake_httpx(self, monkeypatch, status, body: bytes):
        import httpx as _httpx

        class FakeResp:
            status_code = status

            async def aiter_bytes(self):
                yield body

        class FakeStream:
            async def __aenter__(self):
                return FakeResp()

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def __init__(self, **kw): pass
            def stream(self, method, url, json=None): return FakeStream()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        monkeypatch.setattr(egress, "validate_public_url",
                            lambda url, allow_http=False: url)
        monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    def test_non_200_includes_peer_detail(self, monkeypatch):
        self._fake_httpx(
            monkeypatch, 503,
            b'{"detail": "Federa\xc3\xa7\xc3\xa3o indispon\xc3\xadvel (MAESTRO_SECRET_KEY ausente)"}')
        with pytest.raises(ValueError) as exc:
            asyncio.run(egress._get_json("POST", "https://peer.example/x", allow_http=False))
        assert "peer respondeu HTTP 503" in str(exc.value)
        assert "MAESTRO_SECRET_KEY" in str(exc.value)

    def test_non_200_body_not_json_keeps_status_only(self, monkeypatch):
        self._fake_httpx(monkeypatch, 500, b"<html>Internal Server Error</html>")
        with pytest.raises(ValueError) as exc:
            asyncio.run(egress._get_json("GET", "https://peer.example/x", allow_http=False))
        assert str(exc.value) == "peer respondeu HTTP 500"

    def test_non_200_long_detail_truncated(self, monkeypatch):
        import json as _json
        self._fake_httpx(monkeypatch, 503,
                         _json.dumps({"detail": "x" * 400}).encode())
        with pytest.raises(ValueError) as exc:
            asyncio.run(egress._get_json("GET", "https://peer.example/x", allow_http=False))
        msg = str(exc.value)
        assert "x" * egress._MAX_ERROR_DETAIL_CHARS in msg          # detail presente…
        assert "x" * (egress._MAX_ERROR_DETAIL_CHARS + 1) not in msg  # …mas capado

    def test_non_200_detail_ctrl_chars_flattened(self, monkeypatch):
        # detail do peer vai para log/UI: caracteres de controle (newline, ESC)
        # viram espaço — conteúdo textual preservado, sem quebrar linha
        self._fake_httpx(
            monkeypatch, 503,
            b'{"detail": "linha real\\n2026-07-10 FAKE forjada\\u001b[31mansi"}')
        with pytest.raises(ValueError) as exc:
            asyncio.run(egress._get_json("GET", "https://peer.example/x", allow_http=False))
        msg = str(exc.value)
        assert "\n" not in msg and "\x1b" not in msg
        assert "linha real" in msg and "forjada" in msg

    def test_non_200_detail_non_string_ignored(self, monkeypatch):
        # detail array (422 de validação FastAPI) não vira lixo na mensagem
        self._fake_httpx(
            monkeypatch, 422,
            b'{"detail": [{"loc": ["body"], "msg": "field required"}]}')
        with pytest.raises(ValueError) as exc:
            asyncio.run(egress._get_json("GET", "https://peer.example/x", allow_http=False))
        assert str(exc.value) == "peer respondeu HTTP 422"


# ── rota sync (root-only) ──
class TestSyncRoute:
    def _client(self, user):
        app = FastAPI()
        app.include_router(fed_routes.peers_router)
        app.dependency_overrides[require_user] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    def test_non_root_403(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        r = self._client({"id": "u", "role": "member"}).post("/api/v1/federation/peers/p1/sync")
        assert r.status_code == 403

    def test_disabled_409(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(False))
        r = self._client({"id": "r", "role": "root"}).post("/api/v1/federation/peers/p1/sync")
        assert r.status_code == 409

    def test_peer_not_found_404(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id", _async(None))
        r = self._client({"id": "r", "role": "root"}).post("/api/v1/federation/peers/p1/sync")
        assert r.status_code == 404

    def test_success(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active", "workspace": "r"}))
        monkeypatch.setattr(fed_routes.egress, "sync_remote_entries", _async({"registered": 2, "skipped": 1}))
        monkeypatch.setattr(fed_routes.audit_repo, "create", _async(None))
        r = self._client({"id": "r", "role": "root"}).post("/api/v1/federation/peers/p1/sync")
        assert r.status_code == 200 and r.json()["registered"] == 2

    def test_ssrf_400(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active", "workspace": "r"}))
        async def boom(*a, **k):
            raise SSRFError("bad host")
        monkeypatch.setattr(fed_routes.egress, "sync_remote_entries", boom)
        r = self._client({"id": "r", "role": "root"}).post("/api/v1/federation/peers/p1/sync")
        assert r.status_code == 400

    def test_502_sync_surfaces_known_reason(self, monkeypatch):
        """A2A-2 também no sync: peer != 200 / manifesto inválido surfa a causa."""
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active", "workspace": "r"}))
        async def boom(*a, **k):
            raise ValueError("peer respondeu HTTP 500: erro interno")
        monkeypatch.setattr(fed_routes.egress, "sync_remote_entries", boom)
        r = self._client({"id": "r", "role": "root"}).post("/api/v1/federation/peers/p1/sync")
        assert r.status_code == 502
        assert "HTTP 500" in r.json()["detail"]


# ── rota remote-invoke ──
class TestRemoteInvokeRoute:
    def _client(self, user):
        app = FastAPI()
        app.include_router(fed_routes.router)
        app.dependency_overrides[require_user] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    def _happy(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes, "secret_key_present", lambda: True)
        entry = {"id": "e1", "federated": True, "remote_peer_id": "p1",
                 "remote_urn": "urn:maestro:remote:pipeline:x:1.0.0"}
        monkeypatch.setattr(fed_routes.catalog_entries_repo, "find_by_id", _async(entry))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active", "workspace": "r",
                                    "base_url": "https://peer.example", "shared_secret": "enc::x"}))
        monkeypatch.setattr(fed_routes.egress, "invoke_remote",
                            _async({"status": "completed", "output": "ok", "total_cost_usd": 0.01}))
        monkeypatch.setattr(fed_routes.audit_repo, "create", _async(None))
        import app.catalog.queries as q
        monkeypatch.setattr(q, "record_invocation_cost", _async({}))

    def test_happy(self, monkeypatch):
        self._happy(monkeypatch)
        r = self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"})
        assert r.status_code == 200 and r.json()["output"] == "ok"

    def test_disabled_409(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(False))
        assert self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"}).status_code == 409

    def test_503_no_secret_key(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes, "secret_key_present", lambda: False)
        assert self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"}).status_code == 503

    def test_404_entry_missing(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes.catalog_entries_repo, "find_by_id", _async(None))
        assert self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"}).status_code == 404

    def test_422_not_federated(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes.catalog_entries_repo, "find_by_id",
                            _async({"id": "e1", "federated": False}))
        assert self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"}).status_code == 422

    def test_400_empty_input(self, monkeypatch):
        self._happy(monkeypatch)
        assert self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "   "}).status_code == 400

    def test_409_peer_gone(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id", _async(None))
        assert self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"}).status_code == 409

    def test_502_on_egress_error(self, monkeypatch):
        self._happy(monkeypatch)
        async def boom(*a, **k):
            raise RuntimeError("peer down")
        monkeypatch.setattr(fed_routes.egress, "invoke_remote", boom)
        r = self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"})
        # erro DESCONHECIDO continua genérico (não vaza traceback/detalhe interno)
        assert r.status_code == 502
        assert r.json()["detail"] == "Falha ao invocar o peer"

    def test_502_surfaces_known_reason(self, monkeypatch):
        """A2A-2: a razão conhecida (status+detail do peer) chega à UI — o 502
        mudo escondia p.ex. o 503 fail-closed do peer (MAESTRO_SECRET_KEY)."""
        self._happy(monkeypatch)
        async def boom(*a, **k):
            raise ValueError(
                "peer respondeu HTTP 503: Federação indisponível (MAESTRO_SECRET_KEY ausente)")
        monkeypatch.setattr(fed_routes.egress, "invoke_remote", boom)
        r = self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"})
        assert r.status_code == 502
        assert "HTTP 503" in r.json()["detail"]
        assert "MAESTRO_SECRET_KEY" in r.json()["detail"]

    def test_502_surfaces_transport_error(self, monkeypatch):
        import httpx
        self._happy(monkeypatch)
        async def boom(*a, **k):
            raise httpx.ConnectTimeout("timed out")
        monkeypatch.setattr(fed_routes.egress, "invoke_remote", boom)
        r = self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"})
        assert r.status_code == 502
        assert "ConnectTimeout" in r.json()["detail"]

    def test_502_transport_error_without_text_names_class(self, monkeypatch):
        """str() vazio (ex.: ConnectError sem texto) não pode virar detail vazio."""
        import httpx
        self._happy(monkeypatch)
        async def boom(*a, **k):
            raise httpx.ConnectError("")
        monkeypatch.setattr(fed_routes.egress, "invoke_remote", boom)
        r = self._client({"id": "u", "role": "member"}).post(
            "/api/v1/federation/remote/e1/invoke", json={"input": "oi"})
        assert r.status_code == 502
        assert r.json()["detail"] == "Falha ao invocar o peer: ConnectError"


class TestExecutePipelineRejectsFederated:
    """S1: o /execute-pipeline local DEVE recusar entries federadas (capability
    remota não tem snapshot local) — guarda explícita, não só efeito colateral."""

    def test_federated_entry_422(self, monkeypatch):
        import app.routes.catalog as cat
        app = FastAPI()
        app.include_router(cat.router)
        app.dependency_overrides[require_user] = lambda: {"id": "u", "role": "member"}
        fed_entry = {
            "id": "e1", "kind": "pipeline", "federated": True, "status": "published",
            "visibility": "company", "urn": "urn:maestro:remote:pipeline:x:1.0.0", "owner_user_id": "x",
        }
        monkeypatch.setattr(cat.catalog_entries_repo, "find_by_id", _async(fed_entry))
        monkeypatch.setattr(cat, "can_user_see", lambda u, e: True)
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/v1/catalog/entries/e1/execute-pipeline", json={"input": "x"})
        assert r.status_code == 422
        assert "federada" in r.json()["detail"].lower()
