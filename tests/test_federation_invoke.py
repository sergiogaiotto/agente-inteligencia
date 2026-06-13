"""PR8b3 — ingress assinado de federação (`POST /api/v1/federation/invoke`).

Cobre: `Envelope.from_dict` (round-trip do wire preserva a superfície assinada),
os helpers de verificação/replay/lookup, e TODA rejeição do endpoint (gate off,
fail-closed, peer/assinatura inválida, replay por tempo e por nonce, alvo
inexistente/não-exponível, sem snapshot selável, input ausente/grande) + o caminho
feliz. Sem Postgres: pool fake p/ SQL, monkeypatch das deps. Async via asyncio.run.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.catalog.federation as fed
import app.catalog.executor as executor_mod
from app.a2a.protocol import Budget, Envelope, IntentDescriptor
from app.routes import federation as fed_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ── pool fake p/ helpers que tocam SQL ──
class _Conn:
    def __init__(self, fetchrow=None, execute_seq=None):
        self._fetchrow = fetchrow
        self._execute_seq = list(execute_seq or [])

    async def fetchrow(self, sql, *p):
        return self._fetchrow

    async def execute(self, sql, *p):
        return self._execute_seq.pop(0) if self._execute_seq else "INSERT 0 1"


class _Acquire:
    def __init__(self, c): self.c = c
    async def __aenter__(self): return self.c
    async def __aexit__(self, *a): return False


class _Pool:
    def __init__(self, c): self.c = c
    def acquire(self): return _Acquire(self.c)


# ── Envelope.from_dict ──
class TestEnvelopeFromDict:
    def _signed_wire(self, secret="s"):
        env = Envelope(
            envelope_id="n1", origin_workspace="acme",
            target_skill_urn="urn:maestro:local:pipeline:x:1.0.0",
            skill_ref="urn:maestro:local:pipeline:x:1.0.0",
            intent=IntentDescriptor(domain="fiscal", actor="u1"),
            context={"user_input": "oi", "k": [1, 2]},
            budget_remaining=Budget(tokens=1000, wall_ms=5000, usd=0.5),
            deadline="2026-12-31T23:59:59", created_at="2026-01-01T00:00:00",
        )
        sig = env.sign_hmac(secret)
        wire = {
            "envelope_id": env.envelope_id, "origin_workspace": env.origin_workspace,
            "target_skill_urn": env.target_skill_urn, "skill_ref": env.skill_ref,
            "intent": {"domain": "fiscal", "actor": "u1"},
            "context": env.context,
            "budget_remaining": {"tokens": 1000, "wall_ms": 5000, "usd": 0.5},
            "deadline": env.deadline, "created_at": env.created_at,
        }
        return wire, sig, secret

    def test_roundtrip_preserves_signature(self):
        wire, sig, secret = self._signed_wire()
        rebuilt = Envelope.from_dict(wire)
        assert rebuilt.verify_hmac(secret, sig)  # superfície assinada idêntica

    def test_nested_intent_and_budget_reconstructed(self):
        wire, _, _ = self._signed_wire()
        e = Envelope.from_dict(wire)
        assert isinstance(e.intent, IntentDescriptor) and e.intent.domain == "fiscal"
        assert isinstance(e.budget_remaining, Budget) and e.budget_remaining.tokens == 1000

    def test_ignores_unknown_keys(self):
        e = Envelope.from_dict({"origin_workspace": "acme", "evil": "x", "id": "y"})
        assert e.origin_workspace == "acme"
        assert not hasattr(e, "evil")

    def test_tampered_wire_fails_verify(self):
        wire, sig, secret = self._signed_wire()
        wire["target_skill_urn"] = "urn:maestro:local:pipeline:evil:1.0.0"
        assert not Envelope.from_dict(wire).verify_hmac(secret, sig)

    def test_to_dict_roundtrip_verifies(self):
        # B1 regressão: to_dict emite intent/context/budget como STRINGS JSON;
        # from_dict DEVE parseá-las e a assinatura tem de bater (sem crash).
        env = Envelope(
            envelope_id="n9", origin_workspace="acme",
            target_skill_urn="urn:maestro:local:pipeline:x:1.0.0",
            intent=IntentDescriptor(domain="fiscal", actor="u1"),
            context={"user_input": "oi", "n": 5},
            budget_remaining=Budget(tokens=10, wall_ms=20, usd=0.1),
            created_at="2026-01-01T00:00:00",
        )
        sig = env.sign_hmac("s")
        rebuilt = Envelope.from_dict(env.to_dict())  # strings → objetos
        assert rebuilt.verify_hmac("s", sig)

    def test_string_intent_parsed_not_crash(self):
        e = Envelope.from_dict({"origin_workspace": "a", "intent": '{"domain":"x"}'})
        assert isinstance(e.intent, IntentDescriptor) and e.intent.domain == "x"

    def test_bad_type_fields_raise(self):
        # B1: tipos não-dict/não-JSON DEVEM levantar (caller → 400), nunca passar
        # adiante p/ verify_hmac (que faria asdict() de um não-dict → 500).
        for bad in ({"intent": 123}, {"intent": [1, 2]}, {"intent": "nao-json"},
                    {"budget_remaining": "xyz"}, {"context": 99}):
            with pytest.raises((ValueError, TypeError)):
                Envelope.from_dict({"origin_workspace": "a", **bad})

    def test_non_dict_envelope_raises(self):
        with pytest.raises(ValueError):
            Envelope.from_dict("not-an-object")

    def test_default_created_at_is_utc(self):
        # S2: created_at default em UTC → dentro da janela vs utcnow()
        import datetime as _dt
        env = Envelope(origin_workspace="a")
        assert fed.within_replay_window(env.created_at, _dt.datetime.utcnow(), window_s=120)


# ── helpers ──
class TestReplayWindow:
    def test_in_window(self):
        now = __import__("datetime").datetime(2026, 1, 1, 12, 0, 0)
        assert fed.within_replay_window("2026-01-01T12:02:00", now, window_s=300)

    def test_out_of_window(self):
        now = __import__("datetime").datetime(2026, 1, 1, 12, 0, 0)
        assert not fed.within_replay_window("2026-01-01T12:30:00", now, window_s=300)

    def test_bad_format(self):
        now = __import__("datetime").datetime(2026, 1, 1, 12, 0, 0)
        assert not fed.within_replay_window("not-a-date", now)
        assert not fed.within_replay_window("", now)


class TestNonce:
    def test_new_nonce_true(self, monkeypatch):
        monkeypatch.setattr(fed, "_get_pool", lambda: _Pool(_Conn(execute_seq=["DELETE 0", "INSERT 0 1"])))
        assert asyncio.run(fed.check_and_record_nonce("n1", "acme")) is True

    def test_replay_nonce_false(self, monkeypatch):
        monkeypatch.setattr(fed, "_get_pool", lambda: _Pool(_Conn(execute_seq=["DELETE 0", "INSERT 0 0"])))
        assert asyncio.run(fed.check_and_record_nonce("n1", "acme")) is False

    def test_empty_nonce_false(self, monkeypatch):
        assert asyncio.run(fed.check_and_record_nonce("", "acme")) is False


class TestVerifyInbound:
    def test_valid_secret_returns_peer(self, monkeypatch):
        env = Envelope(origin_workspace="acme", target_skill_urn="urn:maestro:local:pipeline:x:1.0.0")
        sig = env.sign_hmac("the-secret")
        monkeypatch.setattr(fed._peers, "get_active_peer_by_workspace", _async({"workspace": "acme"}))
        monkeypatch.setattr(fed._peers, "peer_secrets", lambda p: ["the-secret"])
        assert asyncio.run(fed.verify_inbound_envelope(env, sig)) == {"workspace": "acme"}

    def test_wrong_secret_returns_none(self, monkeypatch):
        env = Envelope(origin_workspace="acme")
        sig = env.sign_hmac("the-secret")
        monkeypatch.setattr(fed._peers, "get_active_peer_by_workspace", _async({"workspace": "acme"}))
        monkeypatch.setattr(fed._peers, "peer_secrets", lambda p: ["other"])
        assert asyncio.run(fed.verify_inbound_envelope(env, sig)) is None

    def test_unknown_peer_returns_none(self, monkeypatch):
        env = Envelope(origin_workspace="ghost")
        monkeypatch.setattr(fed._peers, "get_active_peer_by_workspace", _async(None))
        assert asyncio.run(fed.verify_inbound_envelope(env, "sig")) is None

    def test_overlap_window_old_secret_accepted(self, monkeypatch):
        env = Envelope(origin_workspace="acme")
        sig_old = env.sign_hmac("old-secret")
        monkeypatch.setattr(fed._peers, "get_active_peer_by_workspace", _async({"workspace": "acme"}))
        monkeypatch.setattr(fed._peers, "peer_secrets", lambda p: ["new-secret", "old-secret"])
        assert asyncio.run(fed.verify_inbound_envelope(env, sig_old)) == {"workspace": "acme"}


# ── rota /api/v1/federation/invoke ──
class TestInvokeRoute:
    EXPOSABLE = {
        "id": "e1", "urn": "urn:maestro:local:pipeline:x:1.0.0", "kind": "pipeline",
        "status": "published", "visibility": "company", "name": "X",
    }

    def _envelope(self, **over):
        e = {
            "origin_workspace": "acme",
            "target_skill_urn": "urn:maestro:local:pipeline:x:1.0.0",
            "envelope_id": "n1", "created_at": "2026-01-01T00:00:00",
            "context": {"user_input": "oi federada"},
        }
        e.update(over)
        return e

    def _body(self, envelope=None):
        return {"envelope": envelope or self._envelope(), "signature": "sig"}

    def _happy(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes, "secret_key_present", lambda: True)
        monkeypatch.setattr(fed, "verify_inbound_envelope", _async({"workspace": "acme"}))
        monkeypatch.setattr(fed, "within_replay_window", lambda *a, **k: True)
        monkeypatch.setattr(fed, "check_and_record_nonce", _async(True))
        monkeypatch.setattr(fed, "get_entry_by_urn", _async(dict(self.EXPOSABLE)))
        monkeypatch.setattr(fed, "resolve_federated_exec", _async(("root1", {"root1", "a2"})))
        monkeypatch.setattr(fed_routes, "create_execution", _async({"id": "exec1"}))
        monkeypatch.setattr(executor_mod, "execute_pipeline_entry", _async({"output": "resposta federada"}))
        monkeypatch.setattr(fed_routes, "get_execution",
                            _async({"status": "completed", "total_cost_usd": 0.01, "total_latency_ms": 1200}))
        monkeypatch.setattr(fed_routes.audit_repo, "create", _async(None))

    def _client(self):
        app = FastAPI()
        app.include_router(fed_routes.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_happy_path(self, monkeypatch):
        self._happy(monkeypatch)
        r = self._client().post("/api/v1/federation/invoke", json=self._body())
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["execution_id"] == "exec1"
        assert b["status"] == "completed"
        assert b["output"] == "resposta federada"
        assert b["workspace"] == "acme"

    def test_404_when_disabled(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(False))
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 404

    def test_503_without_secret_key(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed_routes, "secret_key_present", lambda: False)
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 503

    def test_400_incomplete_envelope(self, monkeypatch):
        self._happy(monkeypatch)
        body = self._body(self._envelope(origin_workspace="", target_skill_urn=""))
        assert self._client().post("/api/v1/federation/invoke", json=body).status_code == 400

    def test_403_bad_signature(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed, "verify_inbound_envelope", _async(None))
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 403

    def test_401_replay_window(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed, "within_replay_window", lambda *a, **k: False)
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 401

    def test_409_replay_nonce(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed, "check_and_record_nonce", _async(False))
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 409

    def test_404_unknown_target(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed, "get_entry_by_urn", _async(None))
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 404

    def test_404_target_not_exposable(self, monkeypatch):
        self._happy(monkeypatch)
        draft = dict(self.EXPOSABLE, status="draft")  # is_federation_exposable real → False
        monkeypatch.setattr(fed, "get_entry_by_urn", _async(draft))
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 404

    def test_422_no_sealable_snapshot(self, monkeypatch):
        self._happy(monkeypatch)
        monkeypatch.setattr(fed, "resolve_federated_exec", _async((None, set())))
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 422

    def test_400_missing_user_input(self, monkeypatch):
        self._happy(monkeypatch)
        body = self._body(self._envelope(context={}))
        assert self._client().post("/api/v1/federation/invoke", json=body).status_code == 400

    def test_413_user_input_too_long(self, monkeypatch):
        self._happy(monkeypatch)
        body = self._body(self._envelope(context={"user_input": "x" * 100_001}))
        assert self._client().post("/api/v1/federation/invoke", json=body).status_code == 413

    def test_504_on_timeout(self, monkeypatch):
        self._happy(monkeypatch)
        async def _timeout(*a, **k):
            raise asyncio.TimeoutError()
        monkeypatch.setattr(executor_mod, "execute_pipeline_entry", _timeout)
        assert self._client().post("/api/v1/federation/invoke", json=self._body()).status_code == 504

    def test_413_oversized_body(self, monkeypatch):
        self._happy(monkeypatch)
        big = {"envelope": self._envelope(context={"user_input": "x" * 300_000}), "signature": "sig"}
        assert self._client().post("/api/v1/federation/invoke", json=big).status_code == 413

    def test_nonce_recorded_only_after_auth(self, monkeypatch):
        # T2: peer inválido → nonce NÃO deve ser consumido (anti-flood pré-auth)
        self._happy(monkeypatch)
        monkeypatch.setattr(fed, "verify_inbound_envelope", _async(None))
        called = {"n": 0}
        async def _spy(*a, **k):
            called["n"] += 1
            return True
        monkeypatch.setattr(fed, "check_and_record_nonce", _spy)
        r = self._client().post("/api/v1/federation/invoke", json=self._body())
        assert r.status_code == 403 and called["n"] == 0

    def test_malformed_body_400(self, monkeypatch):
        self._happy(monkeypatch)
        c = self._client()
        # corpo sem 'signature'
        assert c.post("/api/v1/federation/invoke", json={"envelope": self._envelope()}).status_code == 400
        # intent de tipo inválido → from_dict levanta → 400 (não 500)
        bad = {"envelope": self._envelope(intent=123), "signature": "sig"}
        assert c.post("/api/v1/federation/invoke", json=bad).status_code == 400
