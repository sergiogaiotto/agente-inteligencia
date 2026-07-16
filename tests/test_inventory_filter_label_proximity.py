"""Filtros tri-state do Inventário Regulatório — label COLADO no seu select.

QA E2E 2026-07-16: o label tinha `flex-1` e esticava a célula do grid inteira,
empurrando o select para a outra ponta — em monitor largo o campo ficava mais
perto do label da coluna VIZINHA que do próprio, e a associação visual se
perdia. Fix: par compacto `Label [select]` (gap-2, w-fit) dentro de um <label>
real — clicar no texto foca o select (alvo maior + a11y).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "pages" / "catalog_inventory.html").read_text(encoding="utf-8")


def _flag_block(html: str) -> str:
    i = html.index('x-for="f in flagFilters"')
    return html[i - 400: i + 700]


class TestLabelColadoNoSelect:
    def test_sem_flex1_esticando_o_par(self, html):
        """O flex-1 no span era o que separava label do select."""
        b = _flag_block(html)
        assert "flex-1" not in b, "flex-1 no par label/select reabre o distanciamento"

    def test_par_e_um_label_clicavel_e_compacto(self, html):
        b = _flag_block(html)
        assert "<label" in b, "par deve ser <label> (clicar no texto foca o select)"
        assert "w-fit" in b, "sem w-fit o par volta a esticar na célula do grid"
        assert "gap-2" in b

    def test_testid_por_filtro(self, html):
        assert "'flag-filter-' + f.key" in _flag_block(html)
