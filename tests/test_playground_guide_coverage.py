"""Guia Interativo cobre o Playground — os 3 subsistemas + botão "?".

User pediu: o submenu Playground precisa do botão "Ajuda desta página" (como os
outros submenus do AI Mesh), e o Guia Interativo deve ser revisado/complementado.

Garante que o Playground tem: botão "?" (openHelp), entry de Ajuda (help-content),
passo no Tour e módulo no Guia dos Módulos — e que a rota mapeia pra ajuda PRÓPRIA.
"""
from __future__ import annotations

import re
from pathlib import Path

BASE = Path("app/templates/layouts/base.html")
HELP = Path("app/static/js/help-content.js")
GUIDE = Path("app/static/js/module-guide.js")


def test_botao_ajuda_no_submenu_playground():
    base = BASE.read_text(encoding="utf-8")
    assert "openHelp('playground')" in base
    # mapa rota→key aponta pra ajuda PRÓPRIA (não mais a do Fluxo de agentes)
    assert "'/mesh/playground': 'playground'" in base


def test_ajuda_da_pagina_playground_existe():
    keys = set(re.findall(r'^  ([a-z_]+):\s*\{', HELP.read_text(encoding="utf-8"), re.M))
    assert "playground" in keys


def test_tour_cobre_playground():
    base = BASE.read_text(encoding="utf-8")
    assert 'id="tour-nav-playground"' in base            # a nav tem o id pro highlight do tour
    assert "{el:'tour-nav-playground'" in base           # e o passo no tourSteps


def test_modulo_playground_no_guia_dos_modulos():
    guide = GUIDE.read_text(encoding="utf-8")
    assert "id: 'playground'" in guide
    assert "Playground (console de API)" in guide


def test_revisao_renomeou_fluxograma_no_guia():
    """Revisão: o atalho 'Fluxograma' (página renomeada p/ 'Fluxo de agentes')
    não deve mais aparecer no conteúdo do Guia."""
    assert "Fluxograma" not in HELP.read_text(encoding="utf-8")
    assert "Fluxograma" not in GUIDE.read_text(encoding="utf-8")
