"""Testes do runner de pré-checks (run_prechecks)."""

from __future__ import annotations

from app.catalog.prechecks import run_prechecks


def _entry(**over):
    base = {
        "id": "e1",
        "name": "Agente Fiscal",
        "description": "Classifica notas fiscais por CFOP usando regras vigentes",
        "version": "1.0.0",
        "owner_user_id": "u-owner",
        "visibility": "private",
        "adapter_type": "a2a",
        "artifact_type": "agent",
        "artifact_id": "agent-123",
    }
    base.update(over)
    return base


def _active_user():
    return {"id": "u-owner", "status": "active"}


def _check_by_name(report, name):
    for c in report["checks"]:
        if c["name"] == name:
            return c
    return None


class TestRunPrechecks:
    def test_all_pass_happy_path(self):
        r = run_prechecks(_entry(), disclosure={"entry_id": "e1"}, owner=_active_user())
        assert r["passed"] is True
        assert r["errors_count"] == 0
        # Disclosure presente → warning não aparece
        cap = _check_by_name(r, "capability_disclosure_present")
        assert cap and cap["passed"] is True

    def test_short_name_is_error(self):
        r = run_prechecks(_entry(name="X"), disclosure={"entry_id": "e1"}, owner=_active_user())
        chk = _check_by_name(r, "name_length")
        assert chk and not chk["passed"]
        assert chk["severity"] == "error"
        assert r["passed"] is False

    def test_short_description_is_warning(self):
        r = run_prechecks(_entry(description="curta"), disclosure={"entry_id": "e1"}, owner=_active_user())
        chk = _check_by_name(r, "description_length")
        assert chk and not chk["passed"]
        assert chk["severity"] == "warning"
        # Warnings não derrubam passed
        assert r["passed"] is True
        assert r["warnings_count"] >= 1

    def test_bad_version_is_error(self):
        r = run_prechecks(_entry(version="1.0"), disclosure={"entry_id": "e1"}, owner=_active_user())
        chk = _check_by_name(r, "version_semver")
        assert chk and not chk["passed"]
        assert r["passed"] is False

    def test_missing_owner_is_error(self):
        r = run_prechecks(_entry(), disclosure={"entry_id": "e1"}, owner=None)
        chk = _check_by_name(r, "owner_exists")
        assert chk and not chk["passed"]
        assert r["passed"] is False

    def test_inactive_owner_is_warning(self):
        r = run_prechecks(_entry(), disclosure={"entry_id": "e1"}, owner={"status": "inactive"})
        chk = _check_by_name(r, "owner_active")
        assert chk and not chk["passed"]
        assert chk["severity"] == "warning"

    def test_missing_disclosure_is_error(self):
        # A partir do PR 4 (CRUD entregue), ausência de disclosure é error,
        # não warning — sinaliza claramente para Root que falta governança.
        r = run_prechecks(_entry(), disclosure=None, owner=_active_user())
        chk = _check_by_name(r, "capability_disclosure_present")
        assert chk and not chk["passed"]
        assert chk["severity"] == "error"
        # Error faz precheck_passed=False (mas submit segue — Root decide)
        assert r["passed"] is False

    def test_department_without_scope_is_error(self):
        r = run_prechecks(
            _entry(visibility="department", visibility_scope=None),
            disclosure={"entry_id": "e1"},
            owner=_active_user(),
        )
        chk = _check_by_name(r, "visibility_scope_for_department")
        assert chk and not chk["passed"]
        assert r["passed"] is False

    def test_department_with_scope_passes(self):
        r = run_prechecks(
            _entry(visibility="department", visibility_scope="fiscal"),
            disclosure={"entry_id": "e1"},
            owner=_active_user(),
        )
        chk = _check_by_name(r, "visibility_scope_for_department")
        assert chk and chk["passed"]

    def test_a2a_without_artifact_is_error(self):
        r = run_prechecks(
            _entry(artifact_type=None, artifact_id=None),
            disclosure={"entry_id": "e1"},
            owner=_active_user(),
        )
        chk = _check_by_name(r, "a2a_has_artifact")
        assert chk and not chk["passed"]
        assert r["passed"] is False

    def test_non_a2a_skips_artifact_check(self):
        # external_platform usa adapter http/openai — não exige artifact
        r = run_prechecks(
            _entry(adapter_type="http", artifact_type=None, artifact_id=None),
            disclosure={"entry_id": "e1"},
            owner=_active_user(),
        )
        chk = _check_by_name(r, "a2a_has_artifact")
        assert chk and chk["passed"]  # n/a → passed

    def test_report_aggregates_counts(self):
        # Entry com 2 erros (name curto + version ruim) e 1 warning (desc curta)
        r = run_prechecks(
            _entry(name="X", description="x", version="bad"),
            disclosure={"entry_id": "e1"},
            owner=_active_user(),
        )
        assert r["errors_count"] >= 2
        assert r["warnings_count"] >= 1
        assert r["passed"] is False


# ─── External Platforms metadata check (Onda 2) ──────────────────


class TestExternalMetadataCheck:
    def test_skipped_for_internal_kinds(self):
        # kind != external_platform → check passes como n/a
        for k in ("agent", "skill", "recipe"):
            e = _entry(kind=k)
            r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user())
            chk = _check_by_name(r, "external_metadata_present")
            assert chk and chk["passed"]

    def test_warning_when_missing_for_external(self):
        # kind=external_platform sem metadata → warning
        e = _entry(kind="external_platform", adapter_type="http",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          external_metadata=None)
        chk = _check_by_name(r, "external_metadata_present")
        assert chk and not chk["passed"]
        assert chk["severity"] == "warning"
        # Warning não derruba passed
        assert r["passed"] is True

    def test_passes_when_metadata_with_vendor(self):
        e = _entry(kind="external_platform", adapter_type="http",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          external_metadata={"vendor": "OpenAI"})
        chk = _check_by_name(r, "external_metadata_present")
        assert chk and chk["passed"]

    def test_warning_when_metadata_present_but_no_vendor(self):
        # Caso edge: row existe mas vendor está vazio (não deveria acontecer,
        # mas defensivo)
        e = _entry(kind="external_platform", adapter_type="http",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          external_metadata={"vendor": None})
        chk = _check_by_name(r, "external_metadata_present")
        assert chk and not chk["passed"]
        assert chk["severity"] == "warning"


# ─── Recipe checks (Onda 3) ───────────────────────────────────────


class TestRecipeChecks:
    def test_recipe_kind_skips_a2a_artifact_check(self):
        # Recipe é a2a mas não tem artifact — a2a_has_artifact deve passar
        e = _entry(kind="recipe", adapter_type="a2a",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          recipe={"steps": [{"order": 1, "target_entry_id": "t1"}]})
        chk = _check_by_name(r, "a2a_has_artifact")
        assert chk and chk["passed"]

    def test_recipe_without_steps_is_error(self):
        e = _entry(kind="recipe", adapter_type="a2a",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          recipe=None)
        chk = _check_by_name(r, "recipe_has_steps")
        assert chk and not chk["passed"]
        assert chk["severity"] == "error"
        assert r["passed"] is False

    def test_recipe_with_empty_steps_is_error(self):
        e = _entry(kind="recipe", adapter_type="a2a",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          recipe={"steps": []})
        chk = _check_by_name(r, "recipe_has_steps")
        assert chk and not chk["passed"]

    def test_recipe_with_steps_passes(self):
        e = _entry(kind="recipe", adapter_type="a2a",
                   artifact_type=None, artifact_id=None)
        r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user(),
                          recipe={"steps": [{"order": 1, "target_entry_id": "t1"}]})
        chk = _check_by_name(r, "recipe_has_steps")
        assert chk and chk["passed"]

    def test_non_recipe_skips_recipe_check(self):
        for k in ("agent", "skill", "external_platform"):
            e = _entry(kind=k)
            r = run_prechecks(e, disclosure={"entry_id": "e1"}, owner=_active_user())
            chk = _check_by_name(r, "recipe_has_steps")
            assert chk and chk["passed"]


class TestCheckMessagesAreResultAware:
    """C7 (achado E2E 2026-06-23): checks que PASSAM exibiam o texto de FALHA
    (ex.: '1.0.0' válido mostrando "version '1.0.0' não é semver" com passed=true).
    Agora: passou → "ok"/"n/a"; falhou → o motivo real."""

    def test_passed_checks_never_show_failure_text(self):
        r = run_prechecks(_entry(), disclosure={"entry_id": "e1"}, owner=_active_user())
        assert r["passed"] is True
        for c in r["checks"]:
            assert c["passed"] is True
            assert c["message"] in ("ok", "n/a"), f"{c['name']} passou mas mostra {c['message']!r}"
        assert "não é semver" not in str(r["checks"])
        assert "ausente" not in str(r["checks"])

    def test_failed_check_still_shows_reason(self):
        r = run_prechecks(_entry(version="v1"), disclosure={"entry_id": "e1"}, owner=_active_user())
        vs = _check_by_name(r, "version_semver")
        assert vs["passed"] is False and "não é semver" in vs["message"]

    def test_missing_disclosure_shows_reason(self):
        r = run_prechecks(_entry(), disclosure=None, owner=_active_user())
        cap = _check_by_name(r, "capability_disclosure_present")
        assert cap["passed"] is False and "ausente" in cap["message"]
