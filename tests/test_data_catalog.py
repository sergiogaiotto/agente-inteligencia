"""Catálogo de Dados (Onda Catálogo) — PR1 fundação.

Testes PUROS (sem DB, sem DuckDB) da camada de leitura/reconciliação:
- normalize_pii_category: enum fechado, fail-safe neutro.
- reconcile_catalog: "left join" por nome sobre o schema vivo (coração anti-alucinação).
- _decode_json_field: decode defensivo JSONB (string/None → estrutura certa).
- db_row_to_table_dict: expõe `catalog` reconciliado.

A ESCRITA (apply_catalog) e a UI vêm nos PRs 2/3; aqui só a fundação de dados.
"""
from __future__ import annotations

import json as _json

import pytest

from app.data_tables import catalog as catalog_service
from app.data_tables.queries import (
    _decode_json_field,
    db_row_to_table_dict,
    reconcile_catalog,
)
from app.data_tables.types import PiiCategory, normalize_pii_category
from app.evidence.tabular import TabularError


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


# ── apply_catalog (curadoria humana — PR2) ────────────────────────

_ROW = {"id": "t1", "schema_json": _SCHEMA, "description": ""}


class _CaptureRepo:
    """Captura o patch passado ao update (sem DB)."""
    def __init__(self):
        self.updated = None

    async def update(self, table_id, patch):
        self.updated = (table_id, patch)
        return True


@pytest.mark.asyncio
async def test_apply_catalog_builds_human_provenance_and_dumps_jsonb(monkeypatch):
    repo = _CaptureRepo()
    monkeypatch.setattr(catalog_service, "data_tables_repo", repo)

    async def fake_find(tid):
        return {"id": tid, "catalog": "reconciled"}
    monkeypatch.setattr(catalog_service, "find_by_id_with_ks", fake_find)

    cols = [
        {"name": "cd_cliente", "description": "ID do cliente", "pii_category": "name"},
        {"name": "vr_limite_cheque_especial", "description": "Limite BRL", "pii_category": "financial"},
    ]
    out = await catalog_service.apply_catalog(_ROW, "Crédito do cliente", cols, {"id": "u1"})

    # retorna o row reconciliado (via find_by_id_with_ks)
    assert out == {"id": "t1", "catalog": "reconciled"}

    tid, patch = repo.updated
    assert tid == "t1"
    # ARMADILHA JSONB: catalog_json gravado como STRING (json.dumps), não dict
    assert isinstance(patch["catalog_json"], str)
    cat = _json.loads(patch["catalog_json"])
    assert cat["table"]["description_source"] == "human"
    assert cat["table"]["curated_by"] == "u1"
    assert cat["columns"]["cd_cliente"]["pii_category"] == "name"
    assert cat["columns"]["cd_cliente"]["source"] == "human"
    assert cat["columns"]["vr_limite_cheque_especial"]["pii_category"] == "financial"
    # coluna do schema NÃO citada no payload não entra no catálogo
    assert "nr_idade" not in cat["columns"]
    assert patch["description"] == "Crédito do cliente"


@pytest.mark.asyncio
async def test_apply_catalog_rejects_unknown_column(monkeypatch):
    monkeypatch.setattr(catalog_service, "data_tables_repo", _CaptureRepo())
    with pytest.raises(TabularError) as ei:
        await catalog_service.apply_catalog(
            _ROW, "", [{"name": "coluna_fantasma", "pii_category": "none"}], {"id": "u1"}
        )
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_apply_catalog_rejects_invalid_pii(monkeypatch):
    # diferente da sugestão da IA (que coage), a curadoria humana REJEITA pii inválida
    monkeypatch.setattr(catalog_service, "data_tables_repo", _CaptureRepo())
    with pytest.raises(TabularError) as ei:
        await catalog_service.apply_catalog(
            _ROW, "", [{"name": "cd_cliente", "pii_category": "ultra_secreto"}], {"id": "u1"}
        )
    assert ei.value.status_code == 400


# ── Sugestão por IA (PR3 — IA sugere, NÃO persiste) ───────────────

def test_build_suggestion_messages_lists_columns_and_omits_empty_sample():
    msgs = catalog_service.build_suggestion_messages("TB", _SCHEMA, [])
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    user = msgs[1]["content"]
    assert "cd_cliente" in user and "vr_limite_cheque_especial" in user
    assert "AMOSTRA" not in user                      # sem amostra → sem bloco
    msgs2 = catalog_service.build_suggestion_messages("TB", _SCHEMA, [{"cd_cliente": 1}])
    assert "AMOSTRA" in msgs2[1]["content"]           # com amostra → inclui


def test_parse_suggestion_reconciles_against_schema():
    content = _json.dumps({
        "table_description": "Crédito",
        "columns": [
            {"name": "cd_cliente", "description": "ID", "pii_category": "name"},
            {"name": "coluna_inventada", "description": "x", "pii_category": "cpf"},  # descartada
            {"name": "nr_idade", "description": "idade", "pii_category": "ULTRA"},     # pii coagida
        ],
    })
    out = catalog_service.parse_suggestion(content, _SCHEMA)
    assert out["table_description"] == "Crédito"
    names = [c["name"] for c in out["columns"]]
    assert names == [c["name"] for c in _SCHEMA]       # só schema vivo, na ordem
    cols = {c["name"]: c for c in out["columns"]}
    assert cols["cd_cliente"]["pii_category"] == "name"
    assert cols["nr_idade"]["pii_category"] == "none"  # ULTRA coagido p/ none
    assert cols["vr_limite_cheque_especial"]["description"] == ""   # faltou → neutro
    assert "coluna_inventada" not in names             # inventada descartada


def test_parse_suggestion_handles_fenced_and_invalid_json():
    fenced = "```json\n" + _json.dumps({"table_description": "T", "columns": []}) + "\n```"
    assert catalog_service.parse_suggestion(fenced, _SCHEMA)["table_description"] == "T"
    # JSON inválido → neutro, NÃO levanta
    out = catalog_service.parse_suggestion("isso não é json", _SCHEMA)
    assert out["table_description"] == ""
    assert all(c["pii_category"] == "none" for c in out["columns"])


@pytest.mark.asyncio
async def test_generate_suggestion_uses_injected_llm_and_does_not_persist(monkeypatch):
    # se a sugestão persistisse, precisaria de repo — garantimos que NÃO toca o repo
    repo = _CaptureRepo()
    monkeypatch.setattr(catalog_service, "data_tables_repo", repo)

    captured = {}
    async def fake_complete(messages):
        captured["messages"] = messages
        return _json.dumps({"table_description": "Auto", "columns": [
            {"name": "cd_cliente", "description": "ID", "pii_category": "name"}]})

    row = {"id": "t1", "name": "TB", "schema_json": _SCHEMA}
    out = await catalog_service.generate_catalog_suggestion(row, [], fake_complete)
    assert out["table_description"] == "Auto"
    assert [c["name"] for c in out["columns"]] == [c["name"] for c in _SCHEMA]
    assert next(c for c in out["columns"] if c["name"] == "cd_cliente")["pii_category"] == "name"
    assert captured["messages"][0]["role"] == "system"   # build foi usado
    assert repo.updated is None                          # NÃO persistiu
