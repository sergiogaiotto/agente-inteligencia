"""Testes do URN helper — slugify, make_urn, parse_urn, is_valid_urn."""

from __future__ import annotations

import pytest

from app.catalog.urn import (
    DEFAULT_WORKSPACE,
    is_valid_urn,
    make_urn,
    parse_urn,
    slugify,
)


class TestSlugify:
    def test_simple_lowercase(self):
        # Acentos são TRANSLITERADOS (não removidos): 'Análise' → 'analise'.
        assert slugify("Análise Fiscal") == "analise-fiscal"

    def test_transliterates_accents(self):
        # Regressão do bug em que 'Órbita' virava 'rbita' (acento sumia).
        assert slugify("Maestro Órbita") == "maestro-orbita"
        assert slugify("Coração & Ação") == "coracao-acao"
        assert slugify("Inteligência Ártificial João") == "inteligencia-artificial-joao"

    def test_collapses_spaces(self):
        assert slugify("agente   de   compras") == "agente-de-compras"

    def test_strips_special_chars(self):
        assert slugify("agente@2026!#$") == "agente-2026"

    def test_empty_returns_empty(self):
        assert slugify("") == ""
        assert slugify("   ") == ""

    def test_strips_leading_trailing_hyphens(self):
        assert slugify("---hello---") == "hello"

    def test_keeps_digits(self):
        assert slugify("v2-final-draft") == "v2-final-draft"


class TestMakeUrn:
    def test_canonical_form(self):
        urn = make_urn("agent", "Consulta Fiscal", "1.0.0")
        assert urn == f"urn:maestro:{DEFAULT_WORKSPACE}:agent:consulta-fiscal:1.0.0"

    def test_transliterates_accents(self):
        # "Maestro Órbita" gerava 'maestro-rbita' (acento perdido) — agora 'maestro-orbita'.
        urn = make_urn("agent", "Maestro Órbita", "1.0.0")
        assert urn == f"urn:maestro:{DEFAULT_WORKSPACE}:agent:maestro-orbita:1.0.0"

    def test_custom_workspace(self):
        urn = make_urn("skill", "Helper", "0.1.0", workspace="finance")
        assert urn == "urn:maestro:finance:skill:helper:0.1.0"

    def test_rejects_invalid_kind(self):
        with pytest.raises(ValueError, match="kind inválido"):
            make_urn("bogus", "X", "1.0.0")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="name vazio"):
            make_urn("agent", "", "1.0.0")
        with pytest.raises(ValueError, match="name vazio"):
            make_urn("agent", "   ", "1.0.0")

    def test_rejects_non_semver(self):
        with pytest.raises(ValueError, match="semver"):
            make_urn("agent", "X", "1.0")
        with pytest.raises(ValueError, match="semver"):
            make_urn("agent", "X", "v1.0.0")
        with pytest.raises(ValueError, match="semver"):
            make_urn("agent", "X", "1.0.0-beta")

    def test_rejects_name_with_only_special_chars(self):
        with pytest.raises(ValueError, match="slug"):
            make_urn("agent", "@@@!!!", "1.0.0")

    def test_rejects_workspace_with_uppercase(self):
        with pytest.raises(ValueError, match="workspace"):
            make_urn("agent", "X", "1.0.0", workspace="Finance")

    def test_accepts_all_valid_kinds(self):
        for kind in ("agent", "skill", "application", "recipe", "external_platform"):
            urn = make_urn(kind, "X", "1.0.0")
            assert f":{kind}:" in urn


class TestParseUrn:
    def test_roundtrip(self):
        original = make_urn("agent", "Consulta Fiscal", "2.3.4")
        parsed = parse_urn(original)
        assert parsed is not None
        assert parsed["workspace"] == DEFAULT_WORKSPACE
        assert parsed["kind"] == "agent"
        assert parsed["slug"] == "consulta-fiscal"
        assert parsed["version"] == "2.3.4"

    def test_returns_none_for_bogus(self):
        assert parse_urn("not-a-urn") is None
        assert parse_urn("urn:other:x:y:z:1.0.0") is None
        assert parse_urn("") is None
        assert parse_urn("urn:maestro:default:agent:x:not-semver") is None

    def test_returns_none_for_uppercase_slug(self):
        assert parse_urn("urn:maestro:default:agent:UPPER:1.0.0") is None


class TestIsValidUrn:
    def test_valid_returns_true(self):
        assert is_valid_urn(make_urn("agent", "X", "1.0.0"))

    def test_invalid_returns_false(self):
        assert not is_valid_urn("garbage")
        assert not is_valid_urn("")
