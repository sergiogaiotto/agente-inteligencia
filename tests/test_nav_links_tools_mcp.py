"""Regressão: links de navegação devem apontar para rotas de página REAIS.

Bug 2026-06-09 (reportado na UI): o card "MCP por ferramenta" em Configurações
linkava "Conexões" → `/tools`, e o `_pagePathMap` (ajuda contextual) mapeava
`/tools`. Mas a tela de conectores MCP (`pages/tools.html`) é servida em **`/mcp`**
(ver PAGES em frontend.py) — `/tools` NÃO é rota. Resultado: clicar em "Conexões"
dava 404 (`{"detail":"Not Found"}`) e a ajuda da tela caía no fallback.
"""
from __future__ import annotations

from pathlib import Path

import app.routes.frontend as fe

_ROOT = Path(fe.__file__).resolve().parents[2]  # app/routes/frontend.py → raiz do projeto


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_connectors_page_is_served_at_mcp_not_tools():
    """A tela de conectores é /mcp (pages/tools.html); /tools nunca foi rota."""
    assert "/mcp" in fe.PAGES
    assert fe.PAGES["/mcp"]["template"] == "pages/tools.html"
    assert "/tools" not in fe.PAGES


def test_settings_conexoes_link_targets_mcp():
    """O link 'Conexões' do card per-tool deve apontar p/ /mcp (rota válida)."""
    html = _read("app/templates/pages/settings.html")
    assert 'href="/tools"' not in html, "link Conexões aponta p/ /tools → 404"
    assert 'href="/mcp"' in html


def test_help_pathmap_maps_mcp_to_tools_help():
    """A ajuda contextual da tela de conectores (key 'tools') deve casar com /mcp."""
    base = _read("app/templates/layouts/base.html")
    assert "'/mcp': 'tools'" in base
    assert "'/tools': 'tools'" not in base  # path stale removido
