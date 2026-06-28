"""Guard: links de página nos templates apontam para ROTAS que existem.

Bug (2026-06-28): o "Gerenciar →" do wizard de Skill (e o "Cadastrar em API
Connectors →") usavam href="/api_connectors" (underscore), mas a página é
"/api-connectors" (hífen, em frontend.PAGES) → 404 "Not Found".

Este guard varre os templates de página atrás de hrefs internos para rotas de
página e confirma que cada uma existe em PAGES. Foca no caso do api-connectors
(o que quebrou) + uma checagem genérica das rotas de página mais comuns.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.routes.frontend import PAGES

TEMPLATES = Path("app/templates")


def _page_hrefs() -> set[str]:
    """Hrefs internos (começam com '/') sem âncora/query, em todos os templates."""
    hrefs: set[str] = set()
    for f in TEMPLATES.rglob("*.html"):
        for m in re.findall(r'href="(/[a-z0-9_\-/]*)"', f.read_text(encoding="utf-8")):
            hrefs.add(m.split("#")[0].split("?")[0])
    return hrefs


def test_no_underscore_api_connectors_link():
    """Ninguém pode linkar /api_connectors (underscore) — a rota é /api-connectors."""
    offenders = []
    for f in TEMPLATES.rglob("*.html"):
        if 'href="/api_connectors"' in f.read_text(encoding="utf-8"):
            offenders.append(str(f))
    assert not offenders, f"href para /api_connectors (404) em: {offenders} — use /api-connectors"


def test_api_connectors_route_exists():
    assert "/api-connectors" in PAGES, "rota de página /api-connectors precisa existir em PAGES"


def test_common_page_hrefs_resolve_to_real_routes():
    """Rotas de página de 1º nível usadas em href devem existir em PAGES (pega
    typos como /api_connectors). Ignora endpoints de API, âncoras e externos."""
    known = set(PAGES.keys())
    # rotas de página que não estão no PAGES estático (redirects/aliases conhecidos)
    allow = {"/", "/mesh", "/login", "/logout", "/skills/new"}
    suspects = {
        h for h in _page_hrefs()
        if not h.startswith("/api/")        # chamadas de API, não páginas
        and not h.startswith("/static/")
        and "{" not in h                     # templates com placeholder Jinja
        and h not in allow
        and h not in known
        # só rotas "rasas" (1 segmento) — evita falso-positivo em rotas dinâmicas
        and h.count("/") == 1
    }
    assert not suspects, f"hrefs de página sem rota em PAGES (typo?): {sorted(suspects)}"
