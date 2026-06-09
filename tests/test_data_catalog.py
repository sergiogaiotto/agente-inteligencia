"""Catálogo de Dados (Onda Catálogo) — PR1 fundação.

Testes PUROS (sem DB, sem DuckDB) da camada de leitura/reconciliação:
- normalize_pii_category: enum fechado, fail-safe neutro.
- reconcile_catalog: "left join" por nome sobre o schema vivo (coração anti-alucinação).
- _decode_json_field: decode defensivo JSONB (string/None → estrutura certa).
- db_row_to_table_dict: expõe `catalog` reconciliado.

A ESCRITA (apply_catalog) e a UI vêm nos PRs 2/3; aqui só a fundação de dados.
"""
from __future__ import annotations

from app.data_tables.queries import (
    _decode_json_field,
    db_row_to_table_dict,
    reconcile_catalog,
)
from app.data_tables.types import PiiCategory, normalize_pii_category


_SCHEMA = [
    {"name": "cd_cliente", "type": "BIGINT", "nullable": False},
    {"name": "vr_limite_cheque_especial", "type": "BIGINT", "nullable": True},
    {"name": "nr_idade", "type": "BIGINT", "nullable": True},
]


# ── normalize_pii_category ────────────────────────────────────────

def test_normalize_pii_valid_and_case_insensitive():
    assert normalize_pii_category("financial") == "financial"
    assert normalize_pii_category("CPF") == "cpf"
    assert normalize_pii_category("  Email ") == "email"


def test_normalize_pii_invalid_is_none():
    for bad in ("xpto", "", None, 123, [], {"a": 1}):
        assert normalize_pii_category(bad) == PiiCategory.NONE.value


# ── reconcile_catalog ─────────────────────────────────────────────

def test_reconcile_left_join_marks_uncataloged_columns():
    catalog = {"columns": {
        "cd_cliente": {"description": "ID do cliente", "pii_category": "name", "source": "human"},
    }}
    out = reconcile_catalog(_SCHEMA, catalog, "Tabela de crédito")
    cols = {c["name"]: c for c in out["columns"]}
    # coluna catalogada
    assert cols["cd_cliente"]["description"] == "ID do cliente"
    assert cols["cd_cliente"]["pii_category"] == "name"
    assert cols["cd_cliente"]["source"] == "human"
    # colunas SEM entry → neutras
    assert cols["vr_limite_cheque_especial"] == {
        "name": "vr_limite_cheque_especial", "type": "BIGINT", "nullable": True,
        "description": "", "pii_category": "none", "source": None,
    }
    # ordem e cardinalidade seguem o schema vivo (3 colunas)
    assert [c["name"] for c in out["columns"]] == [c["name"] for c in _SCHEMA]
    assert out["table"]["description"] == "Tabela de crédito"


def test_reconcile_drops_orphan_catalog_entry():
    # entry de coluna que NÃO existe no schema (removida num re-promote) → ignorada
    catalog = {"columns": {
        "coluna_removida": {"description": "não existe mais", "pii_category": "cpf"},
        "nr_idade": {"description": "idade", "pii_category": "none", "source": "ai"},
    }}
    out = reconcile_catalog(_SCHEMA, catalog)
    names = [c["name"] for c in out["columns"]]
    assert "coluna_removida" not in names
    assert names == [c["name"] for c in _SCHEMA]


def test_reconcile_coerces_invalid_pii_to_none():
    catalog = {"columns": {"cd_cliente": {"pii_category": "SUPER_SECRETO"}}}
    out = reconcile_catalog(_SCHEMA, catalog)
    cd = next(c for c in out["columns"] if c["name"] == "cd_cliente")
    assert cd["pii_category"] == "none"


def test_reconcile_empty_catalog_is_all_neutral():
    out = reconcile_catalog(_SCHEMA, {}, "")
    assert all(c["pii_category"] == "none" and c["description"] == "" and c["source"] is None
               for c in out["columns"])
    assert out["table"]["source"] is None


def test_reconcile_table_provenance():
    catalog = {"table": {"description_source": "human", "curated_by": "u1", "curated_at": "2026-06-09"}}
    out = reconcile_catalog(_SCHEMA, catalog, "desc")
    assert out["table"]["source"] == "human"
    assert out["table"]["curated_by"] == "u1"


def test_reconcile_defends_against_garbage_types():
    # schema/catalog malformados não devem explodir
    assert reconcile_catalog(None, None)["columns"] == []
    assert reconcile_catalog("nope", {"columns": "nope"})["columns"] == []
    assert reconcile_catalog([{"name": "x", "type": "T", "nullable": True}, "lixo"], {})["columns"] == [
        {"name": "x", "type": "T", "nullable": True, "description": "", "pii_category": "none", "source": None},
    ]


# ── _decode_json_field ────────────────────────────────────────────

def test_decode_json_field_string_and_none():
    out = {"schema_json": '[{"name":"a"}]', "catalog_json": '{"columns":{}}'}
    _decode_json_field(out, "schema_json", [])
    _decode_json_field(out, "catalog_json", {})
    assert out["schema_json"] == [{"name": "a"}]
    assert out["catalog_json"] == {"columns": {}}

    none_case = {"catalog_json": None}
    _decode_json_field(none_case, "catalog_json", {})
    assert none_case["catalog_json"] == {}


def test_decode_json_field_invalid_string_falls_back():
    out = {"schema_json": "{not json", "catalog_json": "}{"}
    _decode_json_field(out, "schema_json", [])
    _decode_json_field(out, "catalog_json", {})
    assert out["schema_json"] == []     # fallback de LISTA
    assert out["catalog_json"] == {}    # fallback de OBJETO


# ── db_row_to_table_dict (reconciliação no row) ───────────────────

def test_db_row_exposes_reconciled_catalog_from_string_jsonb():
    import json as _json
    row = {
        "id": "t1",
        "description": "Crédito",
        "schema_json": _json.dumps(_SCHEMA),                       # string (legacy/mock)
        "catalog_json": _json.dumps({"columns": {
            "cd_cliente": {"description": "ID", "pii_category": "name", "source": "human"},
        }}),
    }
    out = db_row_to_table_dict(row)
    assert isinstance(out["schema_json"], list)   # decodado
    assert "catalog" in out
    cd = next(c for c in out["catalog"]["columns"] if c["name"] == "cd_cliente")
    assert cd["pii_category"] == "name" and cd["source"] == "human"
    assert out["catalog"]["table"]["description"] == "Crédito"


def test_db_row_without_catalog_json_is_neutral():
    # DB não-migrado: sem catalog_json → catálogo neutro, não quebra
    row = {"id": "t1", "description": "", "schema_json": _SCHEMA}
    out = db_row_to_table_dict(row)
    assert all(c["pii_category"] == "none" for c in out["catalog"]["columns"])
