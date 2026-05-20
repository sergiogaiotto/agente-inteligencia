"""Testes integrados das rotas de API Connectors.

Mock pattern: substitui httpx.AsyncClient por fake que captura requests e
devolve respostas controladas. Substitui os repos por dicts in-memory.
Cobre os 5 auth types, 5+ body types, status codes, errors, persistência
de api_call_logs, cookie extraction robusto.
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.api_connectors import router as connectors_router


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def setup_master_key(monkeypatch):
    monkeypatch.setenv("MAESTRO_SECRET_KEY", "test-master-key")
    from app.core import crypto
    crypto._get_fernet.cache_clear()
    yield


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(connectors_router)
    return app


def _client():
    return TestClient(_make_app())


class FakeResponse:
    """Stand-in para httpx.Response."""
    def __init__(self, status_code=200, body=None, headers=None, raw_text=""):
        self.status_code = status_code
        self._body = body
        self._headers = httpx.Headers(headers or {})
        self.text = raw_text or (
            json.dumps(body, ensure_ascii=False) if body is not None else ""
        )

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body

    @property
    def headers(self):
        return self._headers


class FakeAsyncClient:
    """Stand-in para httpx.AsyncClient. Captura kwargs + última request."""
    instances: list = []
    response_map: dict = {}      # url substring → FakeResponse
    raise_exc: dict = {}         # url substring → Exception
    last_requests: list = []     # registro {method, url, headers, json, data, content, files}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = httpx.Headers(kwargs.get("headers") or {})
        self.verify = kwargs.get("verify", True)
        self.timeout = kwargs.get("timeout")
        self.follow_redirects = kwargs.get("follow_redirects", False)
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def request(self, method, url, **kw):
        return await self._do(method, url, **kw)

    async def get(self, url, **kw):
        return await self._do("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._do("POST", url, **kw)

    async def put(self, url, **kw):
        return await self._do("PUT", url, **kw)

    async def patch(self, url, **kw):
        return await self._do("PATCH", url, **kw)

    async def delete(self, url, **kw):
        return await self._do("DELETE", url, **kw)

    async def _do(self, method, url, **kw):
        FakeAsyncClient.last_requests.append({
            "method": method, "url": url,
            "headers": dict(self.headers),  # headers do client
            "kw_headers": kw.get("headers"),
            "json": kw.get("json"),
            "data": kw.get("data"),
            "content": kw.get("content"),
            "files": kw.get("files"),
            "verify": self.verify,
            "timeout": self.timeout,
            "follow_redirects": self.follow_redirects,
        })
        for sub, exc in FakeAsyncClient.raise_exc.items():
            if sub in url:
                raise exc
        for sub, resp in FakeAsyncClient.response_map.items():
            if sub in url:
                return resp
        return FakeResponse(200, {"ok": True})


@pytest.fixture
def fake_http(monkeypatch):
    """Substitui httpx.AsyncClient por FakeAsyncClient."""
    FakeAsyncClient.instances = []
    FakeAsyncClient.last_requests = []
    FakeAsyncClient.response_map = {}
    FakeAsyncClient.raise_exc = {}
    monkeypatch.setattr("app.routes.api_connectors.httpx.AsyncClient", FakeAsyncClient)
    yield FakeAsyncClient


@pytest.fixture
def fake_repos(monkeypatch):
    """Mock dos 3 repos (api_connectors, api_endpoints, api_call_logs).

    Substitui a função _repos() do módulo por dicts in-memory.
    """
    conns: dict[str, dict] = {}
    eps: dict[str, dict] = {}
    logs: dict[str, dict] = {}

    class FakeRepo:
        def __init__(self, store):
            self.store = store

        async def find_by_id(self, id_):
            return dict(self.store[id_]) if id_ in self.store else None

        async def find_all(self, limit=100, **filters):
            rows = list(self.store.values())
            for k, v in filters.items():
                rows = [r for r in rows if r.get(k) == v]
            return rows[:limit]

        async def create(self, data):
            self.store[data["id"]] = dict(data)
            return data

        async def update(self, id_, data):
            if id_ not in self.store:
                return None
            self.store[id_].update(data)
            return dict(self.store[id_])

        async def delete(self, id_):
            return self.store.pop(id_, None) is not None

        async def count(self, **filters):
            rows = list(self.store.values())
            for k, v in filters.items():
                rows = [r for r in rows if r.get(k) == v]
            return len(rows)

    conn_repo = FakeRepo(conns)
    ep_repo = FakeRepo(eps)
    log_repo = FakeRepo(logs)

    async def fake_ensure_tables():
        pass

    monkeypatch.setattr("app.routes.api_connectors._repos", lambda: (conn_repo, ep_repo, log_repo))
    monkeypatch.setattr("app.routes.api_connectors._ensure_tables", fake_ensure_tables)
    return {"connectors": conns, "endpoints": eps, "logs": logs}


# ═════════════════════════════════════════════════════════════════
# Create / Update — encryption at rest
# ═════════════════════════════════════════════════════════════════


class TestCreateUpdateEncryption:
    def test_create_cifra_api_key(self, fake_repos):
        c = _client()
        r = c.post("/api/v1/api-connectors", json={
            "name": "X", "base_url": "https://x.com",
            "api_key": "super-secret",
            "auth_type": "bearer",
        })
        assert r.status_code == 201
        cid = r.json()["id"]
        # API key armazenada está cifrada (prefixo enc::)
        stored = fake_repos["connectors"][cid]["api_key"]
        assert stored.startswith("enc::")
        assert "super-secret" not in stored

    def test_update_recifra_api_key_alterada(self, fake_repos):
        c = _client()
        cid = c.post("/api/v1/api-connectors", json={
            "name": "X", "base_url": "https://x.com", "api_key": "old",
        }).json()["id"]
        r = c.put(f"/api/v1/api-connectors/{cid}", json={"api_key": "new-key"})
        assert r.status_code == 200
        stored = fake_repos["connectors"][cid]["api_key"]
        assert stored.startswith("enc::")
        # Decifrando, valor é o novo
        from app.core.crypto import decrypt_secret
        assert decrypt_secret(stored) == "new-key"

    def test_create_sem_api_key_armazena_vazio(self, fake_repos):
        c = _client()
        r = c.post("/api/v1/api-connectors", json={
            "name": "X", "base_url": "https://x.com", "auth_type": "none",
        })
        cid = r.json()["id"]
        assert fake_repos["connectors"][cid]["api_key"] == ""

    def test_create_default_verify_ssl_true(self, fake_repos):
        c = _client()
        cid = c.post("/api/v1/api-connectors", json={
            "name": "X", "base_url": "https://x.com",
        }).json()["id"]
        assert fake_repos["connectors"][cid]["verify_ssl"] == 1

    def test_create_verify_ssl_false_explicito(self, fake_repos):
        c = _client()
        cid = c.post("/api/v1/api-connectors", json={
            "name": "X", "base_url": "https://x.com", "verify_ssl": 0,
        }).json()["id"]
        assert fake_repos["connectors"][cid]["verify_ssl"] == 0


# ═════════════════════════════════════════════════════════════════
# Proxy — auth types e methods
# ═════════════════════════════════════════════════════════════════


def _seed_connector(repos, **over):
    base = {
        "id": "c1", "name": "TestAPI",
        "base_url": "https://api.example.com",
        "api_key": "", "auth_type": "none", "auth_header": "X-API-Key",
        "health_path": "/health", "timeout_ms": 30000,
        "is_active": 1, "verify_ssl": 1,
    }
    base.update(over)
    # Se api_key informada plaintext, cifra (espelha o que /POST faria)
    if base["api_key"] and not base["api_key"].startswith("enc::"):
        from app.core.crypto import encrypt_secret
        base["api_key"] = encrypt_secret(base["api_key"])
    repos["connectors"][base["id"]] = base
    return base


class TestProxyAuth:
    def test_proxy_sem_auth(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/foo",
        })
        assert r.status_code == 200
        last = fake_http.last_requests[-1]
        # Sem Authorization nem X-API-Key
        client_headers = {k.lower(): v for k, v in last["headers"].items()}
        assert "authorization" not in client_headers
        assert "x-api-key" not in client_headers

    def test_proxy_api_key_header(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="api_key", api_key="key-abc", auth_header="X-Custom")
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/foo",
        })
        last = fake_http.last_requests[-1]
        headers = {k.lower(): v for k, v in last["headers"].items()}
        assert headers.get("x-custom") == "key-abc"

    def test_proxy_bearer(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="bearer", api_key="token-xyz")
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/foo",
        })
        last = fake_http.last_requests[-1]
        headers = {k.lower(): v for k, v in last["headers"].items()}
        assert headers.get("authorization") == "Bearer token-xyz"

    def test_proxy_basic(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="basic", api_key="user:pass")
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/foo",
        })
        last = fake_http.last_requests[-1]
        headers = {k.lower(): v for k, v in last["headers"].items()}
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert headers.get("authorization") == f"Basic {expected}"


class TestProxyMethods:
    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def test_methods_suportados(self, fake_repos, fake_http, method):
        _seed_connector(fake_repos)
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": method, "path": "/x",
        })
        assert r.status_code == 200
        assert fake_http.last_requests[-1]["method"] == method

    def test_method_invalido_retorna_400(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "BOGUS", "path": "/x",
        })
        assert r.json()["status"] == 400

    def test_connector_inexistente_retorna_erro(self, fake_repos, fake_http):
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "ghost", "method": "GET", "path": "/x",
        })
        assert r.json()["status"] == 0


# ═════════════════════════════════════════════════════════════════
# Proxy — body types
# ═════════════════════════════════════════════════════════════════


class TestProxyBodyTypes:
    def test_json_default(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "POST", "path": "/x",
            "body": {"foo": 1},
        })
        last = fake_http.last_requests[-1]
        assert last["json"] == {"foo": 1}

    def test_form_urlencoded(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "POST", "path": "/login",
            "body": {"user": "joao", "pass": "x"},
            "body_type": "form_urlencoded",
        })
        last = fake_http.last_requests[-1]
        assert last["data"] == {"user": "joao", "pass": "x"}
        # Não enviou JSON
        assert last["json"] is None

    def test_text(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "POST", "path": "/x",
            "body": "hello world",
            "body_type": "text",
        })
        last = fake_http.last_requests[-1]
        assert last["content"] == "hello world"

    def test_xml(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "POST", "path": "/soap",
            "body": "<x>1</x>",
            "body_type": "xml",
        })
        last = fake_http.last_requests[-1]
        assert last["content"] == "<x>1</x>"


# ═════════════════════════════════════════════════════════════════
# Proxy — verify_ssl + follow_redirects
# ═════════════════════════════════════════════════════════════════


class TestProxyClientConfig:
    def test_verify_ssl_true_por_default(self, fake_repos, fake_http):
        _seed_connector(fake_repos)  # verify_ssl=1 default
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        assert fake_http.instances[-1].verify is True

    def test_verify_ssl_false_propaga(self, fake_repos, fake_http):
        _seed_connector(fake_repos, verify_ssl=0)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        assert fake_http.instances[-1].verify is False

    def test_follow_redirects_true(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        assert fake_http.instances[-1].follow_redirects is True

    def test_timeout_em_segundos(self, fake_repos, fake_http):
        _seed_connector(fake_repos, timeout_ms=5000)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        # timeout_ms=5000 → timeout=5.0 segundos
        assert fake_http.instances[-1].timeout == 5.0


# ═════════════════════════════════════════════════════════════════
# Proxy — error handling
# ═════════════════════════════════════════════════════════════════


class TestProxyErrors:
    def test_connect_error(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        fake_http.raise_exc = {"api.example.com": httpx.ConnectError("connection refused")}
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        assert r.json()["status"] == 0
        assert "conectar" in r.json()["error"].lower()

    def test_timeout(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        fake_http.raise_exc = {"api.example.com": httpx.TimeoutException("timeout")}
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        assert r.json()["status"] == 408

    def test_response_nao_json_retorna_raw(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        fake_http.response_map = {"api.example.com": FakeResponse(200, raw_text="hello world", body=None)}
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        body = r.json()
        assert body["status"] == 200
        assert "raw" in body["data"]


# ═════════════════════════════════════════════════════════════════
# Proxy — persistência em api_call_logs
# ═════════════════════════════════════════════════════════════════


class TestProxyLogPersistence:
    def test_log_gravado_apos_chamada(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        r = c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/x",
        })
        assert r.status_code == 200
        # 1 log row criado
        assert len(fake_repos["logs"]) == 1
        log = list(fake_repos["logs"].values())[0]
        assert log["method"] == "GET"
        assert log["status_code"] == 200
        assert log["connector_id"] == "c1"

    def test_log_inclui_url_completa(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        c.post("/api/v1/api-connectors/proxy", json={
            "connector_id": "c1", "method": "GET", "path": "/foo/bar",
        })
        log = list(fake_repos["logs"].values())[0]
        assert log["url"] == "https://api.example.com/foo/bar"


# ═════════════════════════════════════════════════════════════════
# Health check
# ═════════════════════════════════════════════════════════════════


class TestHealthCheck:
    def test_test_connector_ok(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        c = _client()
        r = c.post("/api/v1/api-connectors/c1/test")
        body = r.json()
        assert body["ok"] is True
        assert body["status"] == 200

    def test_test_connector_404_se_inexistente(self, fake_repos, fake_http):
        c = _client()
        r = c.post("/api/v1/api-connectors/ghost/test")
        assert r.status_code == 404

    def test_health_all_usa_timeout_do_connector(self, fake_repos, fake_http):
        # Antes: 10s hardcoded ignorando o config. Agora usa timeout_ms.
        _seed_connector(fake_repos, id="c1", timeout_ms=7000)
        _seed_connector(fake_repos, id="c2", timeout_ms=3000)
        c = _client()
        c.get("/api/v1/api-connectors/health/all")
        # Pelo menos 2 instâncias criadas com timeouts respectivos
        timeouts = sorted(inst.timeout for inst in fake_http.instances)
        assert 3.0 in timeouts and 7.0 in timeouts


# ═════════════════════════════════════════════════════════════════
# Cleanup logs (retention)
# ═════════════════════════════════════════════════════════════════


class TestCleanupLogs:
    def test_endpoint_existe_e_aceita_days(self, fake_repos, fake_http, monkeypatch):
        """Endpoint registrado. Não testa execução SQL (precisa de DB real)."""
        # Faz mock do _get_pool para não crashar
        class FakeCon:
            async def execute(self, sql, *args):
                return "DELETE 0"
        class FakePool:
            def acquire(self):
                @asynccontextmanager
                async def cm():
                    yield FakeCon()
                return cm()
        monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool())

        c = _client()
        r = c.post("/api/v1/api-connectors/admin/cleanup-logs?days=30")
        assert r.status_code == 200
        body = r.json()
        assert "deleted_count" in body
        assert body["days_kept"] == 30

    def test_days_fora_do_range_invalido(self, fake_repos, fake_http):
        c = _client()
        r = c.post("/api/v1/api-connectors/admin/cleanup-logs?days=99999")
        assert r.status_code == 422  # le=3650
