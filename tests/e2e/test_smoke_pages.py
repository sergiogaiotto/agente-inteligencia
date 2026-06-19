"""Smoke E2E de TODAS as telas — a maior cobertura de interface por esforço.

Para cada rota de página renderizada (PAGES em app/routes/frontend.py), valida que:
  1. carrega autenticada (não cai em redirect p/ /login),
  2. responde HTTP < 400,
  3. tem o <title> "… — Maestro" (base.html renderizou),
  4. não dispara erro de JS não-tratado (`pageerror`) — i.e. o Alpine subiu e a
     página não está quebrada para o usuário.

O sinal de quebra é `pageerror` (exceção JS não-capturada), não o console.error —
falhas de fetch logadas viram console.error e são ruído esperado em alguns
ambientes; uma exceção não-tratada é que indica tela realmente quebrada.

Mantenha esta lista em sincronia com PAGES (rotas sem parâmetro de path). As
rotas dinâmicas (/agents/{id}/edit, /catalog/{id}) são cobertas pelas jornadas.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e

# Rotas de página (GET, sem parâmetro de path) — espelha frontend.PAGES.
SMOKE_ROUTES = [
    "/",
    "/agents",
    "/agents/new",
    "/skills",
    "/skills/new",
    "/catalog",
    "/catalog/publish",
    "/catalog/queue",
    "/catalog/inventory",
    "/catalog/stewardship",
    "/catalog/cost",
    "/workspace",
    "/mesh/flow",
    "/mcp",
    "/rag",
    "/harness",
    "/releases",
    "/quality",
    "/observability",
    "/infra",
    "/history",
    "/settings",
    "/api-connectors",
    "/federation",
]


@pytest.mark.parametrize("route", SMOKE_ROUTES)
def test_page_loads_without_js_error(authed_page, route):
    page = authed_page
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    resp = page.goto(route, wait_until="domcontentloaded")

    # 1) não foi redirecionado para o login (sessão válida)
    assert "/login" not in page.url, f"{route} redirecionou para login (auth falhou?)"
    # 2) status HTTP saudável
    if resp is not None:
        assert resp.status < 400, f"{route} respondeu HTTP {resp.status}"
    # 3) título renderizado pela base.html
    expect(page).to_have_title(re.compile(r"Maestro"))
    # 4) deixa o Alpine inicializar (x-init/x-data) e coleta erros não-tratados
    page.wait_for_timeout(700)
    assert not js_errors, f"{route} disparou erro(s) de JS: {js_errors}"


def test_unauthenticated_redirects_to_login(browser, base_url):
    """Tela protegida sem sessão deve mandar o usuário para /login (auth gate)."""
    context = browser.new_context(base_url=base_url)
    try:
        page = context.new_page()
        page.goto("/agents", wait_until="domcontentloaded")
        page.wait_for_url(re.compile(r"/login"), timeout=10_000)
        assert page.url.endswith("/login")
    finally:
        context.close()
