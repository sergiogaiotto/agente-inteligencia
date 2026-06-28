"""Rename UI: "Fluxograma de agentes" → "Fluxo de agentes" (melhoria de nomenclatura).

A página `/mesh/flow` é nomeada por `PAGES[...]["title"]`, que o `base.html` injeta
TANTO no `<title>` da aba QUANTO no `<h1>` do cabeçalho (`{{ title }}`). O link da
sidebar tem o rótulo próprio. Este teste trava o novo nome nesses pontos visíveis.
Mantém-se o atalho "Fluxograma" (palavra solta) em prosa/links — fora do escopo deste
rename, que troca só a FRASE "Fluxograma de agentes".
"""
from pathlib import Path

from app.routes.frontend import PAGES

BASE = Path("app/templates/layouts/base.html")


def test_pages_title_renomeado():
    # título da página = cabeçalho (h1) + aba do navegador, ambos via {{ title }}
    assert PAGES["/mesh/flow"]["title"] == "Fluxo de agentes"
    assert PAGES["/mesh/flow"]["section"] == "mesh"


def test_nav_sidebar_renomeado():
    base = BASE.read_text(encoding="utf-8")
    assert "<span>Fluxo de agentes</span>" in base        # rótulo no submenu AI Mesh
    assert "Fluxograma de agentes" not in base            # frase antiga sumiu do nav/tour
