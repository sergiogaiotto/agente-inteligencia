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


def test_console_tem_abas_tempo_e_trace():
    src = PG.read_text(encoding="utf-8")
    # abas novas
    assert 'data-testid="pg-tab-tempo"' in src and 'data-testid="pg-tab-trace"' in src
    assert 'data-testid="pg-tempo"' in src and 'data-testid="pg-trace"' in src
    # Tempo: waterfall do timing do stream + totais
    assert "get waterfall()" in src and "performance.now()" in src
    assert "get totalCost()" in src
    # Trace: lê o trace da resposta FULL (custo/sql/evidência) — só Debug
    assert "get traceItems()" in src
    assert "sql_rendered" in src and "evidence_score" in src
    # custo/SQL só em Debug (fullSteps = pipeline_steps, presente só no full)
    assert "get fullSteps()" in src
    assert "só aparece em <strong>Debug</strong>" in src


def test_trace_recolhe_expande_com_tooltips():
    src = PG.read_text(encoding="utf-8")
    # recolher/expandir por agente
    assert "expanded[i] = !expanded[i]" in src
    assert 'x-show="expanded[i]"' in src
    # tooltips de avaliação (title=) nos termos que precisam de explicação
    assert "Pontuação de evidência" in src
    assert "máquina de decisão" in src


def test_layout_lado_a_lado():
    src = PG.read_text(encoding="utf-8")
    assert "lg:grid-cols-2" in src   # builder | resposta lado a lado
