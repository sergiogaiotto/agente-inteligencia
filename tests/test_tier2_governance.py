"""Tier 2 — gates determinísticos do Catálogo (PR2).

Materializa o ``pii_category`` (antes puro metadata) como allow-list +
mascaramento célula-inteira + bloqueio de predicado. Postura FAIL-SAFE (deny):
na dúvida, NEGA. 100% puro — sem DB, sem LLM.
"""
from __future__ import annotations

import pytest

from app.data_tables.governance import (
    allowed_cols_from_catalog,
    is_column_sensitive,
    is_predicate_blocked,
    mask_rows_by_catalog,
)
from app.data_tables.types import (
    PII_PLACEHOLDERS,
    PiiCategory,
    SqlOperator,
    UNCATALOGED_PLACEHOLDER,
)


def _reconciled(cols: list[tuple]) -> dict:
    """Monta um catálogo no formato de reconcile_catalog.

    `cols`: lista de (name, pii_category, source) ou (name, pii, source,
    output_treatment). source=None = não catalogada. treatment ausente = None
    (governança resolve o default da categoria).
    """
    columns = []
    for t in cols:
        entry = {
            "name": t[0], "type": "VARCHAR", "nullable": True,
            "description": "", "pii_category": t[1], "source": t[2],
        }
        if len(t) > 3:
            entry["output_treatment"] = t[3]
        columns.append(entry)
    return {"table": {}, "columns": columns}


# ─── PII_PLACEHOLDERS: completude do mapa fechado ────────────────


def test_placeholders_cover_all_categories_except_none():
    """Guard: se alguém adicionar uma PiiCategory nova sem placeholder, falha."""
    expected = {p.value for p in PiiCategory} - {PiiCategory.NONE.value}
    assert set(PII_PLACEHOLDERS.keys()) == expected
    assert PiiCategory.NONE.value not in PII_PLACEHOLDERS


# ─── is_column_sensitive: o coração fail-safe ────────────────────


def test_human_none_is_not_sensitive():
    col = {"name": "idade", "pii_category": "none", "source": "human"}
    assert is_column_sensitive(col) is False


def test_human_pii_is_sensitive():
    col = {"name": "cpf", "pii_category": "cpf", "source": "human"}
    assert is_column_sensitive(col) is True


def test_uncatalogued_is_sensitive():
    # categoria 'none' mas SEM source (não catalogada) → sensível (desconhecida)
    col = {"name": "x", "pii_category": "none", "source": None}
    assert is_column_sensitive(col) is True


def test_ai_suggested_none_is_sensitive():
    # sugestão de IA não curada não é confiável p/ liberar
    col = {"name": "x", "pii_category": "none", "source": "ai"}
    assert is_column_sensitive(col) is True


def test_unknown_category_coerced_then_treated():
    # categoria fora do enum → normalize coage p/ 'none'; sem source humano → sensível
    col = {"name": "x", "pii_category": "marciano", "source": "human"}
    # 'marciano' -> 'none' (coerção); source humano → confiável → NÃO sensível
    assert is_column_sensitive(col) is False
    col2 = {"name": "y", "pii_category": "marciano", "source": None}
    assert is_column_sensitive(col2) is True


def test_malformed_col_is_sensitive():
    assert is_column_sensitive(None) is True
    assert is_column_sensitive("nope") is True
    assert is_column_sensitive({}) is True  # sem source → sensível


# ─── allow-list (Gate 3) ─────────────────────────────────────────


def test_allowed_cols_only_human_none():
    cat = _reconciled([
        ("pub", "none", "human"),
        ("cpf", "cpf", "human"),
        ("ghost", "none", None),
        ("ai_none", "none", "ai"),
    ])
    assert allowed_cols_from_catalog(cat) == ["pub"]


def test_allowed_cols_preserves_order():
    cat = _reconciled([("a", "none", "human"), ("b", "none", "human")])
    assert allowed_cols_from_catalog(cat) == ["a", "b"]


def test_allowed_cols_includes_approved_pii():
    cat = _reconciled([("pub", "none", "human"), ("cpf", "cpf", "human")])
    assert allowed_cols_from_catalog(cat) == ["pub"]
    assert allowed_cols_from_catalog(cat, pii_columns_allowed=["cpf"]) == ["pub", "cpf"]


def test_allowed_cols_empty_catalog_is_failsafe():
    assert allowed_cols_from_catalog({}) == []
    assert allowed_cols_from_catalog(None) == []
    assert allowed_cols_from_catalog({"columns": "lixo"}) == []


# ─── predicado (Gate 4) ──────────────────────────────────────────


def test_predicate_human_none_allowed():
    cat = _reconciled([("pub", "none", "human")])
    assert is_predicate_blocked("pub", "=", cat) is False
    assert is_predicate_blocked("pub", "LIKE", cat) is False


def test_predicate_pii_blocked_by_default():
    cat = _reconciled([("cpf", "cpf", "human")])
    assert is_predicate_blocked("cpf", "=", cat) is True
    assert is_predicate_blocked("cpf", "LIKE", cat) is True


def test_predicate_approved_pii_only_exact_eq():
    cat = _reconciled([("cpf", "cpf", "human")])
    # aprovado + igualdade exata → liberado
    assert is_predicate_blocked("cpf", "=", cat, pii_columns_allowed=["cpf"]) is False
    assert is_predicate_blocked("cpf", SqlOperator.EQ, cat, pii_columns_allowed=["cpf"]) is False
    # aprovado mas range/LIKE/IN → continua bloqueado (fecha oracle)
    assert is_predicate_blocked("cpf", "LIKE", cat, pii_columns_allowed=["cpf"]) is True
    assert is_predicate_blocked("cpf", SqlOperator.ILIKE, cat, pii_columns_allowed=["cpf"]) is True
    assert is_predicate_blocked("cpf", ">", cat, pii_columns_allowed=["cpf"]) is True


def test_predicate_uncatalogued_blocked():
    cat = _reconciled([("ghost", "none", None)])
    assert is_predicate_blocked("ghost", "=", cat) is True


def test_predicate_unknown_column_blocked():
    cat = _reconciled([("pub", "none", "human")])
    assert is_predicate_blocked("inexistente", "=", cat) is True


# ─── mascaramento (Gate 6) ───────────────────────────────────────


def test_mask_covers_all_pii_categories():
    for cat_val, placeholder in PII_PLACEHOLDERS.items():
        catalog = _reconciled([("c", cat_val, "human")])
        masked = mask_rows_by_catalog([{"c": "valor-sensivel"}], ["c"], catalog)
        assert masked == [{"c": placeholder}], cat_val


def test_mask_human_none_passes_intact():
    catalog = _reconciled([("c", "none", "human")])
    assert mask_rows_by_catalog([{"c": "mantem"}], ["c"], catalog) == [{"c": "mantem"}]


def test_mask_uncatalogued_uses_uncatalogued_placeholder():
    catalog = _reconciled([("c", "none", None)])
    assert mask_rows_by_catalog([{"c": "secreto"}], ["c"], catalog) == [
        {"c": UNCATALOGED_PLACEHOLDER}
    ]


def test_mask_handles_none_and_int_cells():
    catalog = _reconciled([("c", "cpf", "human")])
    masked = mask_rows_by_catalog([{"c": None}, {"c": 12345}, {"c": 3.14}], ["c"], catalog)
    assert masked == [{"c": "[CPF]"}, {"c": "[CPF]"}, {"c": "[CPF]"}]


def test_mask_column_absent_from_catalog_is_masked():
    # coluna no resultado mas fora do catálogo → desconhecida → mascarada
    catalog = _reconciled([("known", "none", "human")])
    masked = mask_rows_by_catalog([{"known": "ok", "ghost": "leak"}], ["known"], catalog)
    assert masked == [{"known": "ok", "ghost": UNCATALOGED_PLACEHOLDER}]


def test_mask_does_not_mutate_input():
    catalog = _reconciled([("id", "none", "human"), ("cpf", "cpf", "human")])
    rows = [{"id": 1, "cpf": "111.222.333-44"}]
    masked = mask_rows_by_catalog(rows, ["id", "cpf"], catalog)
    assert masked == [{"id": 1, "cpf": "[CPF]"}]
    assert rows == [{"id": 1, "cpf": "111.222.333-44"}]  # original intacto


def test_mask_columns_none_still_masks_by_keys():
    catalog = _reconciled([("cpf", "cpf", "human")])
    assert mask_rows_by_catalog([{"cpf": "x"}], None, catalog) == [{"cpf": "[CPF]"}]


def test_mask_empty_catalog_masks_everything():
    # fail-safe: sem catálogo, tudo é desconhecido → mascarado
    assert mask_rows_by_catalog([{"x": "v"}], ["x"], {}) == [{"x": UNCATALOGED_PLACEHOLDER}]


def test_mask_tolerates_non_dict_rows():
    catalog = _reconciled([("c", "cpf", "human")])
    out = mask_rows_by_catalog([{"c": "x"}, "linha-ruim", None], ["c"], catalog)
    assert out == [{"c": "[CPF]"}, "linha-ruim", None]


# ─── apply_display_treatment (exibição: Exibir/Mascarar/Suprimir) ─


def test_display_default_preserves_old_behavior():
    # SEM override: não-PII exibe, PII mascara (= comportamento de fábrica)
    from app.data_tables.governance import apply_display_treatment
    cat = _reconciled([("cpf", "cpf", "human"), ("ghost", "none", None), ("id", "none", "human")])
    rows = [{"cpf": "111.222.333-44", "ghost": "passa", "id": 7}]
    out = apply_display_treatment(rows, cat)
    assert out == [{"cpf": "[CPF]", "ghost": "passa", "id": 7}]
    assert rows == [{"cpf": "111.222.333-44", "ghost": "passa", "id": 7}]  # não muta


def test_display_show_override_reveals_pii_value():
    # O CASO DO USUÁRIO: financial + Exibir → MOSTRA o valor (sem mentir na categoria)
    from app.data_tables.governance import apply_display_treatment
    cat = _reconciled([("vr_limite", "financial", "human", "show")])
    assert apply_display_treatment([{"vr_limite": 3600}], cat) == [{"vr_limite": 3600}]


def test_display_mask_override_on_non_pii():
    # none + Mascarar → placeholder genérico [PROTEGIDO]
    from app.data_tables.governance import apply_display_treatment
    cat = _reconciled([("obs", "none", "human", "mask")])
    assert apply_display_treatment([{"obs": "texto"}], cat) == [{"obs": "[PROTEGIDO]"}]


def test_display_suppress_removes_column():
    from app.data_tables.governance import apply_display_treatment, display_columns
    cat = _reconciled([("id", "none", "human"), ("segredo", "financial", "human", "suppress")])
    out = apply_display_treatment([{"id": 1, "segredo": 999}], cat)
    assert out == [{"id": 1}]                                  # coluna removida da linha
    assert display_columns(cat, ["id", "segredo"]) == ["id"]   # e do cabeçalho


def test_display_empty_catalog_unchanged():
    from app.data_tables.governance import apply_display_treatment
    assert apply_display_treatment([{"x": 1}], {}) == [{"x": 1}]
    assert apply_display_treatment([{"x": 1}], None) == [{"x": 1}]


def test_display_default_masks_all_categories():
    from app.data_tables.governance import apply_display_treatment
    for cat_val, placeholder in PII_PLACEHOLDERS.items():
        cat = _reconciled([("c", cat_val, "human")])      # sem override → default mask
        assert apply_display_treatment([{"c": "v"}], cat) == [{"c": placeholder}], cat_val


def test_treatment_helpers():
    from app.data_tables.types import (
        OutputTreatment, default_treatment_for, effective_treatment, normalize_output_treatment,
    )
    assert default_treatment_for("none") == "show"
    assert default_treatment_for("financial") == "mask"
    assert normalize_output_treatment("show") == "show"
    assert normalize_output_treatment("EXIBIR") is None       # fora do enum → None
    assert normalize_output_treatment(None) is None
    # override vence; ausente herda o default da categoria
    assert effective_treatment("financial", "show") == OutputTreatment.SHOW.value
    assert effective_treatment("financial", None) == OutputTreatment.MASK.value
    assert effective_treatment("none", None) == OutputTreatment.SHOW.value


# ─── integração com reconcile_catalog real ───────────────────────


def test_integration_with_reconcile_catalog():
    """Usa o reconcile_catalog REAL (não o mock) p/ garantir compatibilidade de
    formato: schema vivo + catalog_json humano → gates corretos."""
    from app.data_tables.queries import reconcile_catalog

    schema = [
        {"name": "id", "type": "BIGINT", "nullable": False},
        {"name": "cpf", "type": "VARCHAR", "nullable": True},
        {"name": "nome", "type": "VARCHAR", "nullable": True},  # NÃO catalogada
    ]
    catalog_json = {
        "version": 1,
        "table": {"description_source": "human"},
        "columns": {
            "id": {"pii_category": "none", "source": "human"},
            "cpf": {"pii_category": "cpf", "source": "human"},
            # 'nome' ausente de propósito → não catalogada
        },
    }
    cat = reconcile_catalog(schema, catalog_json)

    # id liberado; cpf (PII) e nome (não catalogada) sensíveis
    assert allowed_cols_from_catalog(cat) == ["id"]
    assert is_predicate_blocked("cpf", "=", cat) is True
    assert is_predicate_blocked("nome", "=", cat) is True

    masked = mask_rows_by_catalog(
        [{"id": 7, "cpf": "111.222.333-44", "nome": "Maria"}],
        ["id", "cpf", "nome"],
        cat,
    )
    assert masked == [{"id": 7, "cpf": "[CPF]", "nome": UNCATALOGED_PLACEHOLDER}]
