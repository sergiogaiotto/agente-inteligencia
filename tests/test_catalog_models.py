"""Testes dos Pydantic models — validação de input/output."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.catalog.models import (
    CapabilityDisclosure,
    CatalogEntry,
    CatalogEntryCreate,
    CatalogEntryUpdate,
    SubmissionDecision,
)
from app.catalog.urn import make_urn


# ─── CatalogEntryCreate ──────────────────────────────────────────


class TestCatalogEntryCreate:
    def test_minimal_valid(self):
        e = CatalogEntryCreate(name="Agente X", kind="agent")
        assert e.name == "Agente X"
        assert e.kind == "agent"
        assert e.version == "0.1.0"
        assert e.visibility == "private"
        assert e.adapter_type == "a2a"

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            CatalogEntryCreate(name="", kind="agent")

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            CatalogEntryCreate(name="X", kind="bogus")

    def test_rejects_non_semver_version(self):
        with pytest.raises(ValidationError):
            CatalogEntryCreate(name="X", kind="agent", version="1.0")
        with pytest.raises(ValidationError):
            CatalogEntryCreate(name="X", kind="agent", version="v1.0.0")

    def test_artifact_link_required_for_agent(self):
        e = CatalogEntryCreate(name="X", kind="agent")
        with pytest.raises(ValueError, match="vínculo"):
            e.require_artifact_link()

    def test_artifact_link_not_required_for_external_platform(self):
        e = CatalogEntryCreate(name="ChatGPT Enterprise", kind="external_platform")
        # Não levanta
        e.require_artifact_link()

    def test_artifact_link_satisfied(self):
        e = CatalogEntryCreate(
            name="X", kind="agent",
            artifact_type="agent", artifact_id="abc-123",
        )
        e.require_artifact_link()


# ─── CatalogEntryUpdate ──────────────────────────────────────────


class TestCatalogEntryUpdate:
    def test_all_fields_optional(self):
        e = CatalogEntryUpdate()
        assert e.name is None
        assert e.version is None

    def test_partial_update(self):
        e = CatalogEntryUpdate(description="nova desc")
        assert e.description == "nova desc"
        assert e.name is None

    def test_rejects_non_semver_version_when_provided(self):
        with pytest.raises(ValidationError):
            CatalogEntryUpdate(version="bad")

    def test_accepts_none_version(self):
        e = CatalogEntryUpdate(version=None)
        assert e.version is None


# ─── CatalogEntry (output) ───────────────────────────────────────


class TestCatalogEntry:
    def _base(self, **over):
        defaults = dict(
            id="abc-123",
            urn=make_urn("agent", "X", "1.0.0"),
            name="X",
            kind="agent",
            version="1.0.0",
            status="published",
            visibility="company",
            owner_user_id="user-1",
            adapter_type="a2a",
        )
        defaults.update(over)
        return defaults

    def test_valid_minimal(self):
        e = CatalogEntry(**self._base())
        assert e.name == "X"
        assert e.status == "published"

    def test_rejects_invalid_urn(self):
        with pytest.raises(ValidationError):
            CatalogEntry(**self._base(urn="not-a-urn"))

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            CatalogEntry(**self._base(status="bogus"))


# ─── CapabilityDisclosure ────────────────────────────────────────


class TestCapabilityDisclosure:
    def test_default_all_false(self):
        d = CapabilityDisclosure()
        assert d.reads_user_kb is False
        assert d.calls_external_apis is False
        assert d.processes_pii is False
        assert d.verification_method == "declared"

    def test_apis_list_consistent_with_flag(self):
        # Se declara que chama, precisa listar
        with pytest.raises(ValidationError, match="external_apis_list"):
            CapabilityDisclosure(calls_external_apis=True, external_apis_list=[])

    def test_apis_list_ok_when_flag_true(self):
        d = CapabilityDisclosure(
            calls_external_apis=True,
            external_apis_list=["https://api.openai.com"],
        )
        assert d.external_apis_list == ["https://api.openai.com"]

    def test_apis_list_empty_when_flag_false(self):
        # OK se não chama APIs externas, lista pode ficar vazia
        d = CapabilityDisclosure(calls_external_apis=False, external_apis_list=[])
        assert d.calls_external_apis is False

    def test_retention_days_non_negative(self):
        with pytest.raises(ValidationError):
            CapabilityDisclosure(stores_input=True, storage_retention_days=-1)

    def test_pii_processing_flag(self):
        d = CapabilityDisclosure(processes_pii=True)
        assert d.processes_pii is True


# ─── SubmissionDecision ──────────────────────────────────────────


class TestSubmissionDecision:
    def test_valid_decisions(self):
        for d in ("approved", "rejected", "changes_requested"):
            sd = SubmissionDecision(decision=d)
            assert sd.decision == d

    def test_rejects_pending(self):
        with pytest.raises(ValidationError):
            SubmissionDecision(decision="pending")

    def test_rejects_unknown(self):
        with pytest.raises(ValidationError):
            SubmissionDecision(decision="maybe")

    def test_default_empty_notes(self):
        sd = SubmissionDecision(decision="approved")
        assert sd.notes == ""
