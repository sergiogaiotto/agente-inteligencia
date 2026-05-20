"""Testes de integração do engine declarativo ↔ API Connectors.

Cobre o fluxo end-to-end: SKILL.md.api_bindings → resolve connector
(via repo) → renderiza templates Jinja2 → chama HTTP (via http_auth) →
aplica output_mapping → persiste api_call_logs + binding_executions
linkados por interaction_id/call_id.

Mock pattern: igual a test_api_connectors_routes.py (FakeAsyncClient
captura kwargs httpx; FakeRepo guarda rows em dict). Substitui no
módulo `app.agents.declarative_engine`.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from app.agents import declarative_engine as de
from app.skill_parser.parser import ParsedSkill, SkillFrontmatter


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def setup_master_key(monkeypatch):
    """Master key fixa pra crypto — testes de auth dependem de decrypt
    determinístico (api_key pode estar cifrada no connector mock)."""
    monkeypatch.setenv("MAESTRO_SECRET_KEY", "test-master-key")
    from app.core import crypto
    crypto._get_fernet.cache_clear()
    yield


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body, ensure_ascii=False) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeAsyncClient:
    """Stand-in para httpx.AsyncClient. Captura kwargs + requests."""
    instances: list = []
    response_queue: list = []   # FakeResponses retornados em FIFO
    response_map: dict = {}     # url substring → FakeResponse (fallback)
    raise_exc: dict = {}        # url substring → Exception
    requests: list = []         # registros {method, url, headers, json, data, content, params, ...}

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
        FakeAsyncClient.requests.append({
            "method": method,
            "url": url,
            "client_headers": dict(self.headers),
            "kw_headers": kw.get("headers"),
            "json": kw.get("json"),
            "data": kw.get("data"),
            "content": kw.get("content"),
            "files": kw.get("files"),
            "params": kw.get("params"),
            "verify": self.verify,
            "timeout": self.timeout,
            "follow_redirects": self.follow_redirects,
        })
        for sub, exc in FakeAsyncClient.raise_exc.items():
            if sub in url:
                raise exc
        if FakeAsyncClient.response_queue:
            return FakeAsyncClient.response_queue.pop(0)
        for sub, resp in FakeAsyncClient.response_map.items():
            if sub in url:
                return resp
        return FakeResponse(200, {"ok": True})


@pytest.fixture
def fake_http(monkeypatch):
    FakeAsyncClient.instances = []
    FakeAsyncClient.requests = []
    FakeAsyncClient.response_queue = []
    FakeAsyncClient.response_map = {}
    FakeAsyncClient.raise_exc = {}
    monkeypatch.setattr("app.agents.declarative_engine.httpx.AsyncClient", FakeAsyncClient)
    # Backoff zero pra testes não dormirem em retries
    monkeypatch.setattr("app.agents.declarative_engine._sleep_backoff",
                        lambda mode, attempt: asyncio.sleep(0))
    # Limpa circuit breakers entre testes (estado in-memory global)
    de._BREAKERS.clear()
    yield FakeAsyncClient


class FakeRepo:
    def __init__(self, store: dict):
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


@pytest.fixture
def fake_repos(monkeypatch):
    """Substitui os 4 repos usados pelo engine por dicts in-memory."""
    connectors: dict = {}
    api_logs: dict = {}
    interactions: dict = {}
    bindings: dict = {}

    monkeypatch.setattr(de, "api_connectors_repo", FakeRepo(connectors))
    monkeypatch.setattr(de, "api_call_logs_repo", FakeRepo(api_logs))
    monkeypatch.setattr(de, "interactions_repo", FakeRepo(interactions))
    monkeypatch.setattr(de, "binding_executions_repo", FakeRepo(bindings))

    return {
        "connectors": connectors,
        "api_call_logs": api_logs,
        "interactions": interactions,
        "binding_executions": bindings,
    }


# ─── Helpers ──────────────────────────────────────────────────────


def _seed_connector(repos, *, id="c1", name="TestAPI", **over):
    base = {
        "id": id,
        "name": name,
        "base_url": "https://api.example.com",
        "api_key": "",
        "auth_type": "none",
        "auth_header": "X-API-Key",
        "timeout_ms": 30000,
        "is_active": 1,
        "verify_ssl": 1,
    }
    base.update(over)
    # api_key plaintext é OK — decrypt_secret trata como legacy
    repos["connectors"][id] = base
    return base


def _make_skill(bindings: list[dict]) -> ParsedSkill:
    return ParsedSkill(
        frontmatter=SkillFrontmatter(id="skill-test", version="1.0.0", execution_mode="declarative"),
        api_bindings_parsed=bindings,
    )


def _make_agent(id="agent-1", name="Agent X") -> dict:
    return {"id": id, "name": name}


def _run(coro):
    return asyncio.run(coro)


# ═════════════════════════════════════════════════════════════════
# Connector resolution
# ═════════════════════════════════════════════════════════════════


class TestConnectorResolution:
    def test_resolve_by_name(self, fake_repos, fake_http):
        _seed_connector(fake_repos, id="c-uuid", name="TestAPI")
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "completed"
        assert fake_http.requests[0]["url"] == "https://api.example.com/x"

    def test_resolve_by_id(self, fake_repos, fake_http):
        _seed_connector(fake_repos, id="c-uuid", name="OtherName")
        skill = _make_skill([{
            "id": "b1", "connector": "c-uuid", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "completed"

    def test_connector_not_found_fails_binding(self, fake_repos, fake_http):
        skill = _make_skill([{
            "id": "b1", "connector": "ghost", "method": "GET", "path": "/x",
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "failed"
        assert any("ghost" in e and "não encontrado" in e for e in out["errors"])
        assert len(fake_http.requests) == 0  # Nenhuma chamada HTTP


# ═════════════════════════════════════════════════════════════════
# Auth — usa app.core.http_auth.build_auth_headers
# ═════════════════════════════════════════════════════════════════


class TestAuth:
    def test_bearer_token_no_authorization_header(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="bearer", api_key="token-abc")
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        headers = {k.lower(): v for k, v in fake_http.requests[0]["client_headers"].items()}
        assert headers.get("authorization") == "Bearer token-abc"

    def test_basic_auth_base64(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="basic", api_key="user:pass")
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        headers = {k.lower(): v for k, v in fake_http.requests[0]["client_headers"].items()}
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert headers.get("authorization") == f"Basic {expected}"

    def test_api_key_decifrada_em_runtime(self, fake_repos, fake_http):
        """api_key cifrada at-rest deve ser decifrada no momento do request."""
        from app.core.crypto import encrypt_secret
        ciphered = encrypt_secret("plain-secret-xyz")
        assert ciphered.startswith("enc::")
        _seed_connector(fake_repos, auth_type="api_key", api_key=ciphered, auth_header="X-Custom")
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        headers = {k.lower(): v for k, v in fake_http.requests[0]["client_headers"].items()}
        # Header recebe o plaintext decifrado, não o token cifrado
        assert headers.get("x-custom") == "plain-secret-xyz"
        assert "enc::" not in (headers.get("x-custom") or "")

    def test_none_auth_sem_authorization(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="none")
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        headers = {k.lower(): v for k, v in fake_http.requests[0]["client_headers"].items()}
        assert "authorization" not in headers
        assert "x-api-key" not in headers


# ═════════════════════════════════════════════════════════════════
# Body types — usa app.core.http_auth.prepare_request_body
# ═════════════════════════════════════════════════════════════════


class TestBodyTypes:
    def test_json_default(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI",
            "method": "POST", "path": "/x",
            "idempotency_key": "k1",
            "body": {"foo": "bar"},
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        req = fake_http.requests[0]
        assert req["json"] == {"foo": "bar"}
        assert req["data"] is None and req["content"] is None

    def test_form_urlencoded(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI",
            "method": "POST", "path": "/login",
            "idempotency_key": "k1",
            "body": {"user": "joao", "pass": "x"},
            "body_type": "form_urlencoded",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        req = fake_http.requests[0]
        assert req["data"] == {"user": "joao", "pass": "x"}
        assert req["json"] is None  # não vai como JSON

    def test_text_body(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI",
            "method": "POST", "path": "/x",
            "idempotency_key": "k1",
            "body": "hello world",
            "body_type": "text",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        req = fake_http.requests[0]
        assert req["content"] == "hello world"


# ═════════════════════════════════════════════════════════════════
# Templating Jinja2 — inputs + context
# ═════════════════════════════════════════════════════════════════


class TestTemplating:
    def test_path_renderiza_inputs(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI",
            "method": "GET", "path": "/users/{{ inputs.user_id }}",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill, inputs={"user_id": "42"}))
        assert fake_http.requests[0]["url"] == "https://api.example.com/users/42"

    def test_body_renderiza_inputs_recursivo(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI",
            "method": "POST", "path": "/x",
            "idempotency_key": "k1",
            "body": {"query": "{{ inputs.q }}", "nested": {"limit": "{{ inputs.lim }}"}},
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill, inputs={"q": "search", "lim": "10"}))
        req = fake_http.requests[0]
        assert req["json"] == {"query": "search", "nested": {"limit": "10"}}


# ═════════════════════════════════════════════════════════════════
# output_mapping (JSONPath → context)
# ═════════════════════════════════════════════════════════════════


class TestOutputMapping:
    def test_jsonpath_grava_em_context(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        fake_http.response_queue.append(FakeResponse(200, {"data": {"id": "u-123", "email": "x@y.com"}}))
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/u",
            "output_mapping": [
                {"from": "$.data.id", "to": "user.id"},
                {"from": "$.data.email", "to": "user.email"},
            ],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "completed"
        assert out["context"]["user"]["id"] == "u-123"
        assert out["context"]["user"]["email"] == "x@y.com"

    def test_jsonpath_sem_match_loga_erro_mas_completes_se_outros_funcionam(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        fake_http.response_queue.append(FakeResponse(200, {"only_this": "x"}))
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.missing", "to": "context.foo"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        # 200 OK mas output_mapping não achou nada → erro registrado
        assert any("não encontrou" in e for e in out["errors"])


# ═════════════════════════════════════════════════════════════════
# Retry + backoff
# ═════════════════════════════════════════════════════════════════


class TestRetry:
    def test_retry_5xx_ate_max_e_aceita_2xx_apos(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        fake_http.response_queue.extend([
            FakeResponse(503, None, text="down"),
            FakeResponse(503, None, text="down"),
            FakeResponse(200, {"ok": True}),
        ])
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "resilience": {"retry": {"max": 2, "on": ["5xx"], "backoff": "fixed"}},
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "completed"
        assert len(fake_http.requests) == 3  # 2 retries + sucesso

    def test_retry_5xx_esgota_marca_failed(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        for _ in range(3):
            fake_http.response_queue.append(FakeResponse(500, None, text="boom"))
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "resilience": {"retry": {"max": 2, "on": ["5xx"], "backoff": "fixed"}},
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "failed"
        assert len(fake_http.requests) == 3  # max+1


# ═════════════════════════════════════════════════════════════════
# Circuit breaker — estado in-memory por binding_id
# ═════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_breaker_abre_apos_threshold_e_pula_proxima(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        # 2 falhas fatais (sem retry); threshold=2 → 3ª chamada deve ser suprimida
        skill_falha = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "resilience": {"circuit_breaker": {"threshold": 2, "cooldown_s": 30}},
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        # Falha 1
        fake_http.response_queue.append(FakeResponse(500, None, text="boom"))
        _run(de.execute_declarative(_make_agent(), skill_falha))
        # Falha 2 — abre o breaker
        fake_http.response_queue.append(FakeResponse(500, None, text="boom"))
        _run(de.execute_declarative(_make_agent(), skill_falha))
        # 3ª invocação: breaker bloqueia, não chega a fazer HTTP
        before = len(fake_http.requests)
        out = _run(de.execute_declarative(_make_agent(), skill_falha))
        assert len(fake_http.requests) == before  # nenhuma nova chamada
        assert any("circuit breaker aberto" in e for e in out["errors"])


# ═════════════════════════════════════════════════════════════════
# Persistência cruzada: api_call_logs + binding_executions
# ═════════════════════════════════════════════════════════════════


class TestPersistenceCrossLink:
    def test_call_log_e_binding_execution_compartilham_interaction_id(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        trace_id = out["interaction_id"]
        # api_call_logs criou 1 row
        logs = list(fake_repos["api_call_logs"].values())
        assert len(logs) == 1
        assert logs[0]["interaction_id"] == trace_id
        call_id = logs[0]["id"]
        # binding_executions criou 1 row linkado pelo call_id
        bexes = list(fake_repos["binding_executions"].values())
        assert len(bexes) == 1
        assert bexes[0]["interaction_id"] == trace_id
        assert bexes[0]["call_id"] == call_id
        assert bexes[0]["binding_id"] == "b1"
        assert bexes[0]["status_code"] == 200
        assert bexes[0]["is_compensation"] is False

    def test_request_headers_persistidos_com_auth_redacted(self, fake_repos, fake_http):
        _seed_connector(fake_repos, auth_type="bearer", api_key="super-secret-token")
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        _run(de.execute_declarative(_make_agent(), skill))
        log = list(fake_repos["api_call_logs"].values())[0]
        headers_serialized = log["request_headers"]
        # Token plaintext NÃO deve aparecer em log persistido
        assert "super-secret-token" not in headers_serialized
        # Mas o nome do header sim
        assert "Authorization" in headers_serialized or "authorization" in headers_serialized

    def test_interaction_persistida_com_state_completed(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        skill = _make_skill([{
            "id": "b1", "connector": "TestAPI", "method": "GET", "path": "/x",
            "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
        }])
        out = _run(de.execute_declarative(_make_agent(), skill))
        itx = fake_repos["interactions"][out["interaction_id"]]
        assert itx["state"] == "completed"
        assert itx["agent_id"] == "agent-1"


# ═════════════════════════════════════════════════════════════════
# DAG: depends_on + compensação
# ═════════════════════════════════════════════════════════════════


class TestDAGAndCompensation:
    def test_depends_on_passa_context_entre_niveis(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        # Level 0: retorna id; Level 1: usa context.user_id no path
        fake_http.response_queue.append(FakeResponse(200, {"id": "u-99"}))
        fake_http.response_queue.append(FakeResponse(200, {"name": "Alice"}))
        skill = _make_skill([
            {
                "id": "fetch_id", "connector": "TestAPI",
                "method": "GET", "path": "/me",
                "output_mapping": [{"from": "$.id", "to": "user_id"}],
            },
            {
                "id": "fetch_detail", "connector": "TestAPI",
                "method": "GET", "path": "/users/{{ context.user_id }}",
                "depends_on": ["fetch_id"],
                "output_mapping": [{"from": "$.name", "to": "user_name"}],
            },
        ])
        out = _run(de.execute_declarative(_make_agent(), skill))
        assert out["final_state"] == "completed"
        assert out["context"]["user_name"] == "Alice"
        # Level 1 vai com user_id renderizado a partir do context populado por level 0
        assert fake_http.requests[1]["url"] == "https://api.example.com/users/u-99"

    def test_compensate_dispara_em_falha_e_marca_is_compensation(self, fake_repos, fake_http):
        _seed_connector(fake_repos)
        # 1º request (b1): falha. 2º request (rollback): sucesso.
        fake_http.response_queue.append(FakeResponse(500, None, text="boom"))
        fake_http.response_queue.append(FakeResponse(200, {"rolled_back": True}))
        skill = _make_skill([
            {
                "id": "b1", "connector": "TestAPI",
                "method": "POST", "path": "/charge", "idempotency_key": "k1",
                "body": {"amount": 100},
                "on_failure": {"compensate": "rollback"},
                "output_mapping": [{"from": "$.ok", "to": "context.ok"}],
            },
            {
                "id": "rollback", "connector": "TestAPI",
                "method": "POST", "path": "/refund", "idempotency_key": "k2",
                "body": {"amount": 100},
                "output_mapping": [{"from": "$.rolled_back", "to": "context.rolled_back"}],
            },
        ])
        out = _run(de.execute_declarative(_make_agent(), skill))
        # Compensação disparou
        assert "rollback" in out["compensations_fired"]
        # binding_executions tem 2 rows; o do rollback marcado como compensation
        bexes = list(fake_repos["binding_executions"].values())
        rollback_bex = [b for b in bexes if b["binding_id"] == "rollback"]
        assert len(rollback_bex) == 1
        assert rollback_bex[0]["is_compensation"] is True
