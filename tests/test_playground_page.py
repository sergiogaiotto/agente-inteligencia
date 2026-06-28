"""Playground — console de API tipo AI Studio (submenu de AI Mesh).

Testa um pipeline COMO O APP VERIA: roda o endpoint real via X-API-Key (omitindo
o cookie → o servidor projeta a resposta de integração), com streaming ao vivo e o
código pronto (curl/Python/JS). Reusa a estação de chave + verbosidade + streaming.

Convenção: varredura de template (sem harness de DOM).
"""
from pathlib import Path

from app.routes.frontend import PAGES
from app.routes import frontend as fe

BASE = Path("app/templates/layouts/base.html")
PG = Path("app/templates/pages/mesh_playground.html")


def test_pagina_registrada_e_no_nav():
    assert PAGES.get("/mesh/playground", {}).get("template") == "pages/mesh_playground.html"
    assert PAGES["/mesh/playground"]["section"] == "mesh"
    assert hasattr(fe, "pg_mesh_playground")
    base = BASE.read_text(encoding="utf-8")
    assert 'href="/mesh/playground"' in base           # link no submenu AI Mesh
    assert "'/mesh/playground': 'mesh'" in base          # mapa de seção


def test_console_roda_como_integracao():
    src = PG.read_text(encoding="utf-8")
    assert "playgroundPage()" in src
    # fidelidade: roda o /invoke/stream via X-API-Key OMITINDO o cookie
    assert "/invoke/stream" in src
    assert "credentials: 'omit'" in src
    assert "'X-API-Key'" in src
    # reusa a estação de chave (gerar e embutir)
    assert "...curlAuthStation()" in src
    assert "generateAndEmbed()" in src


def test_console_tem_streaming_e_resposta():
    src = PG.read_text(encoding="utf-8")
    assert 'data-testid="pg-live"' in src   # passo-a-passo ao vivo
    assert "_ev(" in src                    # parser SSE
    assert "outCards()" in src              # resposta elegante (cartões)
    assert 'data-testid="pg-result"' in src


def test_console_tem_codegen_3_linguagens():
    src = PG.read_text(encoding="utf-8")
    assert "snippet()" in src
    # curl + Python (requests) + JS (fetch)
    assert "import requests" in src
    assert "await fetch(" in src
    assert "curl -X POST" in src
    # abas de linguagem
    assert "LANGS:" in src


def test_layout_lado_a_lado():
    src = PG.read_text(encoding="utf-8")
    assert "lg:grid-cols-2" in src   # builder | resposta lado a lado
