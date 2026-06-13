"""Fundação da federação A2A (PR8a).

Cobre: identidade de workspace (`local_workspace`/`federation_enabled`), URN
local×remoto (`is_local_urn`/`is_remote_urn`) com backward-compat, e a assinatura
HMAC de Envelope (`sign_hmac`/`verify_hmac`).

Tudo é função pura OU `local_workspace` com `settings_store.get` monkeypatchado —
sem Postgres. Partes async rodam via `asyncio.run` (padrão do projeto), evitando
dependência de plugin de teste async.
"""
from __future__ import annotations

import asyncio

import pytest

from app.a2a.protocol import Budget, Envelope, IntentDescriptor
from app.catalog.urn import (
    DEFAULT_WORKSPACE,
    is_local_urn,
    is_remote_urn,
    make_urn,
    parse_urn,
)
from app.core import federation_identity as fed
from app.core.database import settings_store


def _patch_setting(monkeypatch, value, *, raises=False):
    """Substitui settings_store.get por um stub async (objeto singleton)."""

    async def fake_get(key, default=""):
        if raises:
            raise RuntimeError("pool ausente")
        if key == fed.WORKSPACE_SETTING_KEY:
            return value
        return default

    monkeypatch.setattr(settings_store, "get", fake_get)


class TestLocalWorkspace:
    def test_default_when_unset(self, monkeypatch):
        _patch_setting(monkeypatch, DEFAULT_WORKSPACE)
        assert asyncio.run(fed.local_workspace()) == "default"

    def test_override(self, monkeypatch):
        _patch_setting(monkeypatch, "acme")
        assert asyncio.run(fed.local_workspace()) == "acme"

    def test_invalid_charset_falls_back(self, monkeypatch):
        _patch_setting(monkeypatch, "Acme Corp!")  # maiúscula + espaço + '!'
        assert asyncio.run(fed.local_workspace()) == "default"

    def test_blank_falls_back(self, monkeypatch):
        _patch_setting(monkeypatch, "   ")
        assert asyncio.run(fed.local_workspace()) == "default"

    def test_settings_failure_falls_back(self, monkeypatch):
        _patch_setting(monkeypatch, "x", raises=True)
        assert asyncio.run(fed.local_workspace()) == "default"


class TestFederationEnabled:
    @pytest.mark.parametrize(
        "val,expected",
        [
            ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
            ("", False), ("0", False), ("false", False), ("nope", False),
        ],
    )
    def test_enabled_parsing(self, monkeypatch, val, expected):
        async def fake_get(key, default=""):
            return val if key == fed.ENABLED_SETTING_KEY else default

        monkeypatch.setattr(settings_store, "get", fake_get)
        assert asyncio.run(fed.federation_enabled()) is expected


class TestIsValidWorkspace:
    def test_valid(self):
        assert fed.is_valid_workspace("default")
        assert fed.is_valid_workspace("acme-corp")
        assert fed.is_valid_workspace("ws123")

    def test_invalid(self):
        for bad in ("", "Acme", "a b", "a_b", "a.b", "WS"):
            assert not fed.is_valid_workspace(bad), bad


class TestLocalRemoteUrn:
    def test_local_urn_default(self):
        urn = make_urn("pipeline", "Fluxo X", "1.0.0")  # workspace default
        assert is_local_urn(urn, "default")
        assert not is_remote_urn(urn, "default")

    def test_remote_urn(self):
        urn = make_urn("pipeline", "Fluxo X", "1.0.0", workspace="acme")
        assert is_remote_urn(urn, "default")
        assert not is_local_urn(urn, "default")
        # da perspectiva da própria 'acme', o mesmo URN é local
        assert is_local_urn(urn, "acme")

    def test_malformed_is_neither(self):
        for bad in ("garbage", "", "urn:other:x:y:z:1.0.0"):
            assert not is_local_urn(bad, "default")
            assert not is_remote_urn(bad, "default")

    def test_default_arg_is_default_workspace(self):
        urn = make_urn("agent", "X", "1.0.0")
        assert is_local_urn(urn)  # sem 2º arg → DEFAULT_WORKSPACE

    def test_backward_compat_default_unchanged(self):
        # Sem workspace explícito → idêntico ao pré-federação.
        assert make_urn("agent", "X", "1.0.0") == "urn:maestro:default:agent:x:1.0.0"
        assert parse_urn(make_urn("agent", "X", "1.0.0"))["workspace"] == "default"


class TestEnvelopeHmac:
    def _env(self):
        return Envelope(
            origin_workspace="acme",
            target_agent_id="agent-123",
            target_skill_urn="urn:maestro:default:pipeline:fluxo-x:1.0.0",
            skill_ref="urn:maestro:default:pipeline:fluxo-x:1.0.0",
            intent=IntentDescriptor(domain="fiscal", process_candidate="apurar", actor="user-1"),
            context={"a": 1, "b": [1, 2, 3]},
            budget_remaining=Budget(tokens=1000, wall_ms=5000, usd=0.5),
            deadline="2026-12-31T23:59:59",
        )

    def test_valid_signature_verifies(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        assert sig and len(sig) == 64  # hex de sha256
        assert env.verify_hmac("peer-secret")          # usa self.signature
        assert env.verify_hmac("peer-secret", sig)      # assinatura explícita

    def test_wrong_secret_fails(self):
        env = self._env()
        env.sign_hmac("peer-secret")
        assert not env.verify_hmac("other-secret")

    def test_tampered_target_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.target_skill_urn = "urn:maestro:default:pipeline:evil:1.0.0"
        assert not env.verify_hmac("peer-secret", sig)

    def test_tampered_context_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.context = {"a": 999}
        assert not env.verify_hmac("peer-secret", sig)

    def test_tampered_budget_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.budget_remaining = Budget(tokens=999999, wall_ms=5000, usd=0.5)
        assert not env.verify_hmac("peer-secret", sig)

    def test_tampered_origin_workspace_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.origin_workspace = "evilcorp"
        assert not env.verify_hmac("peer-secret", sig)

    def test_tampered_target_agent_id_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.target_agent_id = "agent-evil"
        assert not env.verify_hmac("peer-secret", sig)

    def test_tampered_intent_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.intent = IntentDescriptor(domain="fiscal", process_candidate="exfiltrar", actor="user-1")
        assert not env.verify_hmac("peer-secret", sig)

    def test_tampered_deadline_fails(self):
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.deadline = "2099-12-31T23:59:59"
        assert not env.verify_hmac("peer-secret", sig)

    def test_transport_metadata_is_NOT_signed(self):
        # trace_id/span_id/parent_span_id/state_pointer/status são correlação/
        # transporte — não autorizam nada e mudam por hop; mutá-los NÃO invalida.
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        env.trace_id = "outro-trace"
        env.span_id = "outro-span"
        env.parent_span_id = "outro-parent"
        env.state_pointer = "ptr://novo"
        env.status = "completed"
        assert env.verify_hmac("peer-secret", sig)

    def test_roundtrip_reconstruction_verifies(self):
        # Caso de uso real cross-instância: o receptor reconstrói o Envelope com
        # os MESMOS valores transmitidos e a assinatura confere.
        env = self._env()
        sig = env.sign_hmac("peer-secret")
        rebuilt = Envelope(
            envelope_id=env.envelope_id,
            origin_workspace=env.origin_workspace,
            target_agent_id=env.target_agent_id,
            target_skill_urn=env.target_skill_urn,
            skill_ref=env.skill_ref,
            intent=env.intent,
            context=env.context,
            budget_remaining=env.budget_remaining,
            deadline=env.deadline,
            created_at=env.created_at,
        )
        assert rebuilt.verify_hmac("peer-secret", sig)
        # Reconstrução com created_at divergente (campo assinado) NÃO confere.
        rebuilt.created_at = "1999-01-01T00:00:00"
        assert not rebuilt.verify_hmac("peer-secret", sig)

    def test_empty_secret_rejected_on_sign(self):
        env = self._env()
        with pytest.raises(ValueError):
            env.sign_hmac("")

    def test_empty_secret_or_sig_returns_false(self):
        env = self._env()
        env.sign_hmac("peer-secret")
        assert not env.verify_hmac("")              # segredo vazio
        env2 = self._env()
        assert not env2.verify_hmac("peer-secret")  # nunca assinado (signature="")

    def test_differs_from_legacy_sign(self):
        env = self._env()
        env.sign()  # digest legado sem segredo (16 chars)
        legacy = env.signature
        env.sign_hmac("peer-secret")
        assert env.signature != legacy
        assert len(env.signature) == 64 and len(legacy) == 16

    def test_canonical_payload_stable_field_order(self):
        # Mesma identidade lógica → mesma assinatura, independente da ordem das
        # chaves no context (canonicalização por sort_keys).
        common = dict(
            origin_workspace="acme",
            target_skill_urn="urn:maestro:default:pipeline:x:1.0.0",
            envelope_id="fixed-id",
            created_at="2026-01-01T00:00:00",
            budget_remaining=Budget(1, 2, 3),
        )
        e1 = Envelope(context={"a": 1, "b": 2}, **common)
        e2 = Envelope(context={"b": 2, "a": 1}, **common)
        assert e1.sign_hmac("s") == e2.sign_hmac("s")
