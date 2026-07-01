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


# ─── Orientação das features de args (D1–D4) ─────────────────────────────────

_PG = Path("app/templates/pages/mesh_playground.html")


def test_playground_expoe_selo_do_contrato():
    """O Playground mostra o selo do contrato e avisa de drift (o comportamento
    'publicado valida contra o selo, não o skill vivo')."""
    pg = _PG.read_text(encoding="utf-8")
    assert "get sealInfo()" in pg
    assert 'data-testid="pg-seal"' in pg and 'data-testid="pg-drift"' in pg
    assert "Contrato selado" in pg and "Alterações não publicadas" in pg
    # auto-carrega o schema ao trocar de pipeline (pro selo aparecer sem clicar)
    assert "_resetInputsHelper(); _loadInputsSchema()" in pg


def test_ajuda_playground_cobre_args_e_contrato():
    help_ = HELP.read_text(encoding="utf-8")
    assert "exato" in help_ and "interpretar" in help_          # faixas dos args
    assert "Contrato selado" in help_
    assert "valida contra o CONTRATO SELADO" in help_          # a pegadinha do drift


def test_guia_modulos_tem_invoke_args():
    guide = GUIDE.read_text(encoding="utf-8")
    assert "id: 'invoke_args'" in guide
    assert "Parâmetros do invoke e contrato selado" in guide


# ─── Revisão do Guia Interativo: descoberta cruzada + limpeza ───────────────

def test_ajuda_mesh_cobre_selo_e_roteamento_por_valor():
    """Revisão: o help do 'Fluxo de agentes' cruza p/ o selo do contrato e o
    roteamento determinístico por parâmetro exato (inputs.X)."""
    help_ = HELP.read_text(encoding="utf-8")
    assert "SELA o contrato de entrada" in help_
    assert "inputs.tier == 'gold'" in help_        # roteamento por valor no mesh
    assert "Parâmetro exato" in help_              # card da Galeria citado


def test_ajuda_skills_explica_x_uso():
    """Revisão: quem edita a skill em /skills descobre como marcar campo exato."""
    help_ = HELP.read_text(encoding="utf-8")
    assert '"x-uso": "param"' in help_


def test_pagepathmap_sem_chaves_mortas():
    """Revisão: limpeza — rotas inexistentes removidas do _pagePathMap."""
    base = BASE.read_text(encoding="utf-8")
    assert "'/dashboard':" not in base            # rota não existe; '/' já mapeia
    assert "'/api_connectors':" not in base       # variante underscore morta (real é -)
