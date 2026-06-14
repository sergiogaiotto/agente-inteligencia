"""Legendas de papel (Dashboard + Observabilidade): cor canônica + nome pt-BR.

Padrão (ver [[reference_ptbr_ui_glossary]]): Maestro (AOBD)=slate, Triagem (AR)=
orange, Especialista (SA)=teal. Estes cards usam bolinhas ESTÁTICAS (não os
ternários cobertos por test_ui_kind_color_standard), então têm guard próprio:
o AR estava azul (bg-brand-400) e os cards mostravam só o código.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "app" / "templates" / "pages"


@pytest.mark.parametrize("page", ["dashboard.html", "observability.html"])
def test_role_labels_have_ptbr_names(page):
    txt = (PAGES / page).read_text(encoding="utf-8")
    assert "Maestro (AOBD)" in txt
    assert "Triagem (AR)" in txt
    assert "Especialista (SA)" in txt


@pytest.mark.parametrize("page", ["dashboard.html", "observability.html"])
def test_no_blue_role_dot(page):
    """Nenhuma bolinha de papel pode ser azul (bg-brand-400) — AR é orange."""
    txt = (PAGES / page).read_text(encoding="utf-8")
    assert 'rounded-full bg-brand-400"></div>' not in txt, (
        f"{page}: bolinha de papel ainda azul (bg-brand-400) — deve ser orange (Triagem)"
    )
    # cores canônicas presentes (AR=orange, AOBD=slate, SA=teal)
    assert "rounded-full bg-orange-400" in txt
    assert "rounded-full bg-slate-700" in txt
    assert "rounded-full bg-teal-400" in txt
