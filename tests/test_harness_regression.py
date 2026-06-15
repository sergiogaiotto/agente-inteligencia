"""Harness §9.5 — detecção de regressão por dimensão (_dim_regressed).

Bugs pegos na auditoria adversarial dos comboboxes (2026-06-15):
- `baseline_val == 0.0` era descartado como se fosse ausente (`if baseline_val`,
  e 0.0 é falsy em Python) → dimensão silenciosamente não checada.
- A escolha do baseline de referência (mesmo release, mesmo gold_version,
  status='completed') é feita no SQL e não é coberta aqui (precisa de DB).

Convenção do projeto (cf. test_grounding_by_default.py): função pura, sem DB/LLM.
"""
from __future__ import annotations

from app.harness.evaluator import _dim_regressed


def test_queda_acima_do_threshold_regride():
    # baseline 0.80 → current 0.60 = queda de 25% > 5%
    regressed, pct = _dim_regressed(0.80, 0.60, 5.0)
    assert regressed is True
    assert round(pct, 1) == 25.0


def test_queda_dentro_do_threshold_nao_regride():
    # baseline 4.0 → 3.9 = 2.5% < 5%
    regressed, pct = _dim_regressed(4.0, 3.9, 5.0)
    assert regressed is False
    assert round(pct, 1) == 2.5


def test_melhora_nao_regride():
    # current > baseline → pct negativo
    regressed, pct = _dim_regressed(0.50, 0.80, 5.0)
    assert regressed is False
    assert pct < 0


def test_baseline_zero_e_base_valida_nao_some():
    # ARMADILHA: baseline 0.0 é base válida. current 0.5 → melhora (pct negativo),
    # mas o ponto é NÃO ser descartado como ausência (não levanta exceção, retorna pct).
    regressed, pct = _dim_regressed(0.0, 0.5, 5.0)
    assert regressed is False
    assert pct is not None


def test_baseline_zero_current_zero():
    regressed, pct = _dim_regressed(0.0, 0.0, 5.0)
    assert regressed is False
    assert round(pct, 1) == 0.0


def test_baseline_ausente_nao_regride():
    assert _dim_regressed(None, 0.5, 5.0) == (False, None)


def test_current_ausente_nao_regride():
    # dimensão sem judge (ex: factuality None) → sem sinal, não regride
    assert _dim_regressed(0.8, None, 5.0) == (False, None)
