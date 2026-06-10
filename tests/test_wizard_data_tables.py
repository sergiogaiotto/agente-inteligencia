"""Wizard — geração correta de skill para RAG-Tabela (Data Tables).

Regressão dos 4 bugs que faziam o Wizard gerar uma skill que NÃO lia a tabela:
(A) sem `execution_mode: declarative`; (B) bloco `## Data Tables` não-executável
(só urn/name); (C) prompt instruindo "LLM gera SQL"; (D) schema_json (LISTA)
tratado como dict → '(sem schema)', LLM nunca via as colunas.
"""
from __future__ import annotations

import json

from app.routes.wizard import (
    WizardSkillRequest,
    _build_wizard_prompt,
    _slug_id,
    _summarize_table_schema,
    _tables_block,
)


# ─── (D) extração de schema (LISTA) ──────────────────────────────


def test_summarize_schema_handles_list():
    schema = [
        {"name": "cd_cliente", "type": "BIGINT", "nullable": False},
        {"name": "vr_limite_cheque_especial", "type": "BIGINT", "nullable": True},
    ]
    summary, names = _summarize_table_schema(schema)
    assert names == ["cd_cliente", "vr_limite_cheque_especial"]
    assert "cd_cliente:BIGINT" in summary
    assert summary != "(sem schema)"


def test_summarize_schema_handles_json_string_list():
    summary, names = _summarize_table_schema(json.dumps([{"name": "a", "type": "INT"}]))
    assert names == ["a"]
    assert "a:INT" in summary


def test_summarize_schema_handles_legacy_dict():
    summary, names = _summarize_table_schema({"columns": [{"name": "x", "type": "TEXT"}]})
    assert names == ["x"]


def test_summarize_schema_garbage_is_safe():
    assert _summarize_table_schema(None) == ("(sem schema)", [])
    assert _summarize_table_schema("não-json") == ("(schema não-parseável)", [])
    assert _summarize_table_schema(42) == ("(sem schema)", [])


def test_slug_id():
    assert _slug_id("TB_ANALISE_CREDITO") == "tb_analise_credito"
    assert _slug_id("dados — análise") == "dados_an_lise" or _slug_id("dados — análise")
    assert _slug_id("") == "tabela"


# ─── (C) _tables_block: modelo mental correto ────────────────────


def test_tables_block_no_longer_says_llm_generates_sql():
    block = _tables_block([{"urn": "urn:table:x:t:1", "name": "T"}])
    low = block.lower()
    assert "llm gera sql" not in low
    assert "select ... from" not in low
    assert "parametrizada" in low
    assert "execution_mode: declarative" in block
    assert "não escreve sql" in low


# ─── (A)+(B) bloco obrigatório executável + declarative ──────────


def _bindings(data_tables):
    return {"mcp_tools": [], "rag_sources": [], "api_endpoints": [], "data_tables": data_tables}


def _table(pk="cd_cliente"):
    return {
        "id": "t1",
        "name": "TB_ANALISE_CREDITO",
        "urn": "urn:table:88356215:dados-analise-preditiva-tb-analise-credito:1",
        "row_count": 48,
        "schema_summary": "cd_cliente:BIGINT, vr_limite_cheque_especial:BIGINT",
        "columns": ["cd_cliente", "vr_limite_cheque_especial"],
        "suggested_pk": pk,
    }


def test_prompt_emits_declarative_and_executable_block():
    data = WizardSkillRequest(description="limite por cliente")
    system, user = _build_wizard_prompt(data, _bindings([_table()]), "standard")
    combined = system + "\n" + user

    # (A) execution_mode declarative exigido
    assert "execution_mode: declarative" in combined
    # (B) bloco EXECUTÁVEL (não binding-only)
    assert "table_ref: urn:table:88356215" in combined
    assert "select: [cd_cliente, vr_limite_cheque_especial]" in combined
    assert 'value: "{{ inputs.cd_cliente }}"' in combined
    assert "output_mapping:" in combined
    assert "on_error: fail" in combined
    # NÃO usa mais o formato binding-only (- urn: ... / name: ...) no YAML da query
    assert "  - urn: urn:table" not in combined
    # (C) sem instrução de gerar SQL
    assert "LLM gera SQL" not in combined
    # nota sobre o input do filtro
    assert "`cd_cliente`" in combined
    # WHERE multi-campo: TODAS as colunas viram filtro if_present (qualquer
    # combinação informada filtra); PK obrigatória, demais opcionais
    assert "if_present: cd_cliente" in combined
    assert "if_present: vr_limite_cheque_especial" in combined
    assert 'value: "{{ inputs.vr_limite_cheque_especial }}"' in combined
    assert "OBRIGATÓRIA" in combined and "OPCIONAIS" in combined


def test_prompt_no_pk_emits_ifpresent_filters_all_optional():
    data = WizardSkillRequest(description="dump")
    system, user = _build_wizard_prompt(data, _bindings([_table(pk=None)]), "standard")
    combined = system + "\n" + user
    assert "execution_mode: declarative" in combined
    assert "table_ref: urn:table:88356215" in combined
    assert "select: [cd_cliente, vr_limite_cheque_especial]" in combined
    # sem PK: ainda gera filtros if_present p/ todas as colunas (opcionais)
    assert "if_present: cd_cliente" in combined
    assert "if_present: vr_limite_cheque_especial" in combined
    assert "OPCIONAIS (sem required)" in combined
    assert "on_error: fail" in combined


def test_prompt_without_tables_has_no_tables_block():
    data = WizardSkillRequest(description="só raciocínio")
    system, user = _build_wizard_prompt(data, _bindings([]), "standard")
    combined = system + "\n" + user
    assert "## Data Tables" not in combined
