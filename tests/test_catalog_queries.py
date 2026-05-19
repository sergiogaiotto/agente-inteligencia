"""Testes de funções puras de queries — visibility e parsing de row."""

from __future__ import annotations

import json

from app.catalog.queries import (
    _user_domains,
    can_user_see,
    db_row_to_entry_dict,
    is_root,
)


# ─── is_root ──────────────────────────────────────────────────────


class TestIsRoot:
    def test_root_user(self):
        assert is_root({"role": "root"})

    def test_root_uppercase(self):
        assert is_root({"role": "ROOT"})

    def test_comum_user(self):
        assert not is_root({"role": "comum"})

    def test_admin_user(self):
        # admin não é root (Onda 1 — só Root tem privilégio total)
        assert not is_root({"role": "admin"})

    def test_no_role(self):
        assert not is_root({})

    def test_none_role(self):
        assert not is_root({"role": None})


# ─── _user_domains ────────────────────────────────────────────────


class TestUserDomains:
    def test_list_passthrough(self):
        assert _user_domains({"domains": ["fiscal", "rh"]}) == ["fiscal", "rh"]

    def test_json_string(self):
        assert _user_domains({"domains": '["fiscal","rh"]'}) == ["fiscal", "rh"]

    def test_empty_string(self):
        assert _user_domains({"domains": ""}) == []

    def test_none(self):
        assert _user_domains({"domains": None}) == []

    def test_missing(self):
        assert _user_domains({}) == []

    def test_malformed_json(self):
        assert _user_domains({"domains": "{not json}"}) == []

    def test_non_list_json(self):
        # se vier dict ou string, retorna []
        assert _user_domains({"domains": '{"a":1}'}) == []


# ─── can_user_see ─────────────────────────────────────────────────


def _entry(**over):
    base = {
        "owner_user_id": "u-owner",
        "status": "published",
        "visibility": "company",
        "visibility_scope": None,
    }
    base.update(over)
    return base


class TestCanUserSee:
    def test_root_sees_anything(self):
        u = {"id": "u-root", "role": "root"}
        assert can_user_see(u, _entry(status="draft", visibility="private"))
        assert can_user_see(u, _entry(status="archived"))

    def test_owner_sees_own_draft(self):
        u = {"id": "u-owner", "role": "comum"}
        assert can_user_see(u, _entry(status="draft", visibility="private"))

    def test_owner_sees_own_archived(self):
        u = {"id": "u-owner", "role": "comum"}
        assert can_user_see(u, _entry(status="archived", visibility="private"))

    def test_nonowner_blocked_on_draft(self):
        u = {"id": "u-other", "role": "comum"}
        assert not can_user_see(u, _entry(status="draft", visibility="company"))

    def test_nonowner_blocked_on_submitted(self):
        u = {"id": "u-other", "role": "comum"}
        assert not can_user_see(u, _entry(status="submitted", visibility="company"))

    def test_nonowner_blocked_on_approved(self):
        # approved ainda não foi published — não vaza
        u = {"id": "u-other", "role": "comum"}
        assert not can_user_see(u, _entry(status="approved", visibility="company"))

    def test_nonowner_sees_published_company(self):
        u = {"id": "u-other", "role": "comum"}
        assert can_user_see(u, _entry(status="published", visibility="company"))

    def test_nonowner_sees_deprecated_company(self):
        u = {"id": "u-other", "role": "comum"}
        assert can_user_see(u, _entry(status="deprecated", visibility="company"))

    def test_nonowner_blocked_on_private(self):
        u = {"id": "u-other", "role": "comum"}
        assert not can_user_see(u, _entry(status="published", visibility="private"))

    def test_department_match(self):
        u = {"id": "u-other", "role": "comum", "domains": '["fiscal","rh"]'}
        e = _entry(visibility="department", visibility_scope="fiscal")
        assert can_user_see(u, e)

    def test_department_no_match(self):
        u = {"id": "u-other", "role": "comum", "domains": '["compras"]'}
        e = _entry(visibility="department", visibility_scope="fiscal")
        assert not can_user_see(u, e)

    def test_department_empty_user_domains(self):
        u = {"id": "u-other", "role": "comum"}
        e = _entry(visibility="department", visibility_scope="fiscal")
        assert not can_user_see(u, e)

    def test_department_no_scope_on_entry(self):
        # entry malformada: department sem scope → bloqueia
        u = {"id": "u-other", "role": "comum", "domains": '["fiscal"]'}
        e = _entry(visibility="department", visibility_scope=None)
        assert not can_user_see(u, e)


# ─── db_row_to_entry_dict ─────────────────────────────────────────


class TestDbRowToEntryDict:
    def test_parses_tags_json(self):
        row = {"id": "x", "tags": '["a","b"]', "adapter_config": "{}"}
        out = db_row_to_entry_dict(row)
        assert out["tags"] == ["a", "b"]

    def test_parses_adapter_config_json(self):
        row = {"id": "x", "tags": "[]", "adapter_config": '{"foo":"bar"}'}
        out = db_row_to_entry_dict(row)
        assert out["adapter_config"] == {"foo": "bar"}

    def test_handles_malformed_json(self):
        row = {"id": "x", "tags": "{not json}", "adapter_config": "[broken"}
        out = db_row_to_entry_dict(row)
        assert out["tags"] == []
        assert out["adapter_config"] == {}

    def test_handles_empty_strings(self):
        row = {"id": "x", "tags": "", "adapter_config": ""}
        out = db_row_to_entry_dict(row)
        assert out["tags"] == []
        assert out["adapter_config"] == {}

    def test_leaves_non_json_fields_intact(self):
        row = {"id": "x", "name": "Foo", "version": "1.0.0", "tags": "[]", "adapter_config": "{}"}
        out = db_row_to_entry_dict(row)
        assert out["id"] == "x"
        assert out["name"] == "Foo"
        assert out["version"] == "1.0.0"

    def test_preserves_already_parsed(self):
        # Se já vier como lista/dict (não string), não tenta parsear
        row = {"tags": ["a"], "adapter_config": {"x": 1}}
        out = db_row_to_entry_dict(row)
        assert out["tags"] == ["a"]
        assert out["adapter_config"] == {"x": 1}
