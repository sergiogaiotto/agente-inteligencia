"""Fluxograma de agentes — o painel de detalhe NÃO pode engolir cliques.

Bug (2026-06-15): "Testar no Workspace" não funcionava. O painel de detalhe vive
DENTRO do canvas, que tem @pointerdown/@pointerup de pan (onCanvasDown/onUp).
Sem @pointerdown.stop no container do painel, o pointerdown borbulhava pro
onUp → selected=null → o `<template x-if="selected ...">` removia o `<a>` do DOM
ANTES do clique navegar. O painel só fechava; a navegação nunca acontecia.

O editor de conexão já usava esse guard; o painel de detalhe (portado da
Topologia em PR-B1) ficou sem. Este teste trava a regressão por varredura de
fonte (mesmo padrão de test_grounding_by_default.py).

Convenção do projeto: não há harness de DOM/Alpine — varredura de template.
"""
from __future__ import annotations

from pathlib import Path


def _mesh_flow_src() -> str:
    return Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")


def test_painel_detalhe_para_propagacao_de_pointerdown():
    src = _mesh_flow_src()
    # Bloco do painel: do marcador "detail panel" até o botão "Testar no Workspace".
    start = src.index("<!-- detail panel -->")
    end = src.index("Testar no Workspace")
    panel_block = src[start:end]
    assert "@pointerdown.stop" in panel_block, (
        "painel de detalhe do mesh_flow precisa de @pointerdown.stop — sem ele o "
        "onUp do canvas faz selected=null e engole o clique de 'Testar no Workspace'"
    )


def test_botao_testar_no_workspace_existe_com_href():
    src = _mesh_flow_src()
    # O link continua sendo um <a> para /workspace com o agente selecionado.
    assert "/workspace?agent=' + selected.id" in src
