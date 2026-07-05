"""P1: rate-limit POR-KEY + gate published-only no invoke via API Key.

- _client_identity: uma request com X-API-Key/Bearer ganha balde PRÓPRIO
  (key:<hash>) — dois frontends no mesmo IP não competem (F5).
- _guard_api_key_published_only: com o toggle ligado, key só invoca publicado;
  cookie/UI e toggle-off não gateiam.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


class _State:
    pass


class _FakeReq:
    def __init__(self, headers=None, api_key_id=None, client_host="1.2.3.4"):
        self.headers = headers or {}
        self.cookies = {}
        self.state = _State()
        if api_key_id is not None:
            self.state.api_key_id = api_key_id

        class _C:
            host = client_host

        self.client = _C()


# ── rate-limit por-key ──
class TestClientIdentityPerKey:
    def test_x_api_key_gives_own_bucket(self):
        from app.core.ratelimit import _client_identity
        ident = _client_identity(_FakeReq(headers={"x-api-key": "ag_live_ABC"}))
        assert ident.startswith("key:")

    def test_bearer_key_gives_own_bucket(self):
        from app.core.ratelimit import _client_identity
        ident = _client_identity(_FakeReq(headers={"authorization": "Bearer ag_live_XYZ"}))
        assert ident.startswith("key:")

    def test_different_keys_different_buckets(self):
        from app.core.ratelimit import _client_identity
        a = _client_identity(_FakeReq(headers={"x-api-key": "ag_live_A"}))
        b = _client_identity(_FakeReq(headers={"x-api-key": "ag_live_B"}))
        assert a != b and a.startswith("key:") and b.startswith("key:")

    def test_no_key_falls_to_ip(self):
        from app.core.ratelimit import _client_identity
        assert _client_identity(_FakeReq(client_host="9.9.9.9")) == "ip:9.9.9.9"


# ── gate published-only ──
class TestPublishedOnlyGuard:
    def _settings(self, monkeypatch, on: bool):
        class _S:
            api_key_invoke_published_only = on
        monkeypatch.setattr("app.core.config.get_settings", lambda: _S())

    def test_cookie_principal_never_gated(self, monkeypatch):
        from app.routes.pipelines import _guard_api_key_published_only
        self._settings(monkeypatch, True)
        # sem api_key_id (cookie) → não gateia nem rascunho
        _guard_api_key_published_only(_FakeReq(), {"status": "rascunho", "name": "x"})

    def test_key_published_ok(self, monkeypatch):
        from app.routes.pipelines import _guard_api_key_published_only
        self._settings(monkeypatch, True)
        _guard_api_key_published_only(_FakeReq(api_key_id="k1"), {"status": "publicado", "name": "x"})

    def test_key_draft_toggle_off_ok(self, monkeypatch):
        from app.routes.pipelines import _guard_api_key_published_only
        self._settings(monkeypatch, False)
        _guard_api_key_published_only(_FakeReq(api_key_id="k1"), {"status": "rascunho", "name": "x"})

    def test_key_draft_toggle_on_forbidden(self, monkeypatch):
        from app.routes.pipelines import _guard_api_key_published_only
        self._settings(monkeypatch, True)
        with pytest.raises(HTTPException) as e:
            _guard_api_key_published_only(_FakeReq(api_key_id="k1"), {"status": "rascunho", "name": "x"})
        assert e.value.status_code == 403
        assert e.value.detail["error"] == "pipeline_not_published"
