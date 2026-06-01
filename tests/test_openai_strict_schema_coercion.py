"""Bug user 2026-06-01 (segundo round, depois da PR #248):

Erro 400 OpenAI ao invocar _Categorizar Imagem:
    Invalid schema for response_format 'Sa_da_da_Categorizar_Imagem':
    In context=(), 'required' is required to be supplied and to be an
    array including every key in properties

Causa: strict mode da OpenAI exige
(https://platform.openai.com/docs/guides/structured-outputs):
- `required` listando TODAS as keys de `properties`
- `additionalProperties: false` em cada object

Skills raramente declaram isso manualmente. Fix: helper
`coerce_to_openai_strict_schema` adapta o schema antes de enviar.
"""
from __future__ import annotations

import pytest

from app.core.text_utils import coerce_to_openai_strict_schema


class TestCoercePropertiesRequired:
    def test_partial_required_becomes_full(self):
        """Caso central do bug: properties tem 2 keys mas required só lista 1."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["category"],
        })
        assert out["required"] == ["category", "confidence"]
        assert out["additionalProperties"] is False

    def test_missing_required_added(self):
        """Schema sem `required` ganha um com todas as keys."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {"a": {"type": "string"}},
        })
        assert out["required"] == ["a"]
        assert out["additionalProperties"] is False

    def test_already_strict_is_idempotent(self):
        """Schema já strict-compatible não muda."""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        out = coerce_to_openai_strict_schema(schema)
        assert out == schema

    def test_preserves_property_order(self):
        """Ordem das keys em required segue a ordem de properties."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {
                "z": {"type": "string"},
                "a": {"type": "string"},
                "m": {"type": "string"},
            },
            "required": [],
        })
        assert out["required"] == ["z", "a", "m"]


class TestCoerceNestedRecursion:
    def test_recurses_into_property_objects(self):
        """Objetos aninhados em properties também ganham strict mode."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {
                "meta": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "string"},
                        "source": {"type": "string"},
                    },
                    "required": ["version"],
                },
            },
        })
        meta = out["properties"]["meta"]
        assert meta["required"] == ["version", "source"]
        assert meta["additionalProperties"] is False

    def test_recurses_into_array_items(self):
        """Schema de items em array vira strict também."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "weight": {"type": "number"},
                        },
                    },
                },
            },
        })
        item_schema = out["properties"]["tags"]["items"]
        assert item_schema["required"] == ["name", "weight"]
        assert item_schema["additionalProperties"] is False

    def test_recurses_into_oneof_branches(self):
        """oneOf com branches object aplica coerção em cada branch."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {
                "result": {
                    "oneOf": [
                        {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                        {"type": "object", "properties": {"err": {"type": "string"}}},
                    ],
                },
            },
        })
        branches = out["properties"]["result"]["oneOf"]
        assert branches[0]["required"] == ["ok"]
        assert branches[1]["required"] == ["err"]
        assert all(b["additionalProperties"] is False for b in branches)

    def test_recurses_into_definitions(self):
        """`definitions` (Draft-07) recebe coerção em cada definition."""
        out = coerce_to_openai_strict_schema({
            "type": "object",
            "properties": {"x": {"$ref": "#/definitions/Tag"}},
            "definitions": {
                "Tag": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "id": {"type": "integer"}},
                },
            },
        })
        tag = out["definitions"]["Tag"]
        assert tag["required"] == ["name", "id"]
        assert tag["additionalProperties"] is False


class TestCoerceLeavesOtherSchemasAlone:
    def test_primitive_type_passes_through(self):
        """Schema de string/integer/etc não tem `properties` — não muda."""
        assert coerce_to_openai_strict_schema({"type": "string"}) == {"type": "string"}
        assert coerce_to_openai_strict_schema({"type": "integer", "minimum": 0}) == {
            "type": "integer", "minimum": 0
        }

    def test_object_without_properties_unchanged(self):
        """Objeto aberto (sem properties): não força additionalProperties=false.
        Strict mode aceita isso — só rejeita objetos parcialmente definidos."""
        out = coerce_to_openai_strict_schema({"type": "object"})
        assert "additionalProperties" not in out
        assert "required" not in out

    def test_non_dict_input_passthrough(self):
        """Defesa contra input inesperado."""
        assert coerce_to_openai_strict_schema(None) is None  # type: ignore[arg-type]
        assert coerce_to_openai_strict_schema("not a schema") == "not a schema"  # type: ignore[arg-type]
        assert coerce_to_openai_strict_schema(42) == 42  # type: ignore[arg-type]

    def test_does_not_mutate_input(self):
        """Helper devolve cópia — input original fica intacto."""
        original = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a-only"],
        }
        snapshot = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a-only"],
        }
        coerce_to_openai_strict_schema(original)
        assert original == snapshot, "input foi mutado"


class TestEndToEndWithBuildResponseFormat:
    """Smoke do source: engine.py importa e usa o helper antes do strict=True."""

    def test_engine_imports_coerce_helper(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(
            encoding="utf-8"
        )
        assert "coerce_to_openai_strict_schema" in src
        # Aplicado antes de virar payload do strict
        assert "strict_schema = coerce_to_openai_strict_schema(schema)" in src

    def test_verifier_imports_coerce_helper(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "verifier" / "runtime.py").read_text(
            encoding="utf-8"
        )
        assert "coerce_to_openai_strict_schema" in src


class TestRealUserSchema:
    """Reproduce o erro do user com um schema similar ao da skill
    _Categorizar Imagem e confirma que após coerce ele é strict-compatible."""

    def test_categorize_image_schema_becomes_strict(self):
        """Schema parecido com o reportado: properties parcialmente em
        required, sem additionalProperties."""
        user_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Saída da Categorizar Imagem",
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["paisagem", "retrato", "arquitetura", "outro"],
                },
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["category"],
        }
        out = coerce_to_openai_strict_schema(user_schema)

        # 1. required agora lista TODAS as keys de properties
        assert set(out["required"]) == {"category", "confidence", "rationale"}
        # 2. additionalProperties: false setado
        assert out["additionalProperties"] is False
        # 3. enum preservado (não-objeto, helper não mexe)
        assert out["properties"]["category"]["enum"] == [
            "paisagem", "retrato", "arquitetura", "outro"
        ]
        # 4. title preservado (helper de name é outra função)
        assert out["title"] == "Saída da Categorizar Imagem"
