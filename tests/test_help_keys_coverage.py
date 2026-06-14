"""Guard: todo botão "?" (openHelp('X')) tem um entry de ajuda em help-content.js.

Motivado pela revisão do Guia Interativo (2026-06-13): a página Federação tinha
botão de nav mas NENHUMA ajuda; e os submenus Fluxograma/Workspace nem tinham o
"?". Este guard impede que um openHelp() aponte para uma chave inexistente.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "app" / "templates" / "layouts" / "base.html"
HELP = ROOT / "app" / "static" / "js" / "help-content.js"


def _help_keys() -> set[str]:
    """Chaves de topo de window.HELP_CONTENT (indentadas com 2 espaços)."""
    txt = HELP.read_text(encoding="utf-8")
    # corta a partir do início do objeto pra não pegar chaves aninhadas
    return set(re.findall(r'^  ([a-z_]+):\s*\{', txt, re.M))


def _openhelp_keys() -> set[str]:
    return set(re.findall(r"openHelp\('([a-z_]+)'\)", BASE.read_text(encoding="utf-8")))


def test_every_openhelp_has_help_entry():
    missing = _openhelp_keys() - _help_keys()
    assert not missing, f"openHelp() sem entry em help-content.js: {sorted(missing)}"


def test_federation_help_exists():
    assert "federation" in _help_keys()


def test_mesh_submenu_help_buttons_wired():
    """Fluxograma e Workspace (submenus do AI Mesh) têm botão '?'."""
    txt = BASE.read_text(encoding="utf-8")
    assert "openHelp('mesh')" in txt        # Fluxograma de agentes
    assert "openHelp('workspace')" in txt   # Workspace
    assert "openHelp('federation')" in txt  # Federação


def test_tour_covers_federation():
    assert "tour-nav-federation" in BASE.read_text(encoding="utf-8")
