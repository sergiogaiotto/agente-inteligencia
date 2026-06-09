"""Blindagem: todo path do `_pagePathMap` (mapa da ajuda contextual em base.html)
deve corresponder a uma ROTA REAL de página (PAGES em frontend.py).

Pega a classe de bug em que o front-end referencia um path que não existe como
rota → a ajuda contextual cai no fallback (e links viram 404). Casos históricos
desta mesma classe: `/tools` (a tela é `/mcp`) e `/evidence` (a tela é `/rag`).
"""
from __future__ import annotations

import re
from pathlib import Path

import app.routes.frontend as fe

_ROOT = Path(fe.__file__).resolve().parents[2]  # app/routes/frontend.py → raiz

# Aliases intencionais no mapa que não são rotas próprias (variações de URL
# aceitas que redirecionam/equivalem a uma rota canônica).
_ALIASES = {"/dashboard", "/api_connectors"}


def _pagepathmap_paths() -> list[str]:
    base = (_ROOT / "app/templates/layouts/base.html").read_text(encoding="utf-8")
    m = re.search(r"_pagePathMap:\s*\{(.*?)\n\s*\},", base, re.DOTALL)
    assert m, "_pagePathMap não encontrado em base.html"
    return re.findall(r"'(/[^']*)'\s*:", m.group(1))


def test_pagepathmap_paths_exist_in_pages():
    """Nenhum path do mapa pode apontar p/ rota inexistente (era o bug /tools, /evidence)."""
    paths = _pagepathmap_paths()
    assert len(paths) > 5, "regex não capturou paths — teste estaria vazio (falso positivo)"
    pages = set(fe.PAGES.keys())
    broken = [p for p in paths if p not in pages and p not in _ALIASES]
    assert not broken, f"_pagePathMap aponta p/ paths que não existem em PAGES: {broken}"


def test_rag_help_mapped_not_evidence():
    """A tela de conhecimento é servida em /rag — o mapa deve usar /rag, não /evidence."""
    paths = _pagepathmap_paths()
    assert "/rag" in paths, "a tela de conhecimento (/rag) precisa estar no mapa de ajuda"
    assert "/evidence" not in paths, "/evidence não é rota (era o bug) — deve ser /rag"
