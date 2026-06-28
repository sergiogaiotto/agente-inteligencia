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


def test_bloco_ai_mesh_fecha_certo():
    """Regressão: contagem TOTAL de <div> balanceada NÃO pega mis-nesting. O bloco
    do AI Mesh (tour-nav-mesh) tinha um </div> a mais que fechava o submenu cedo e
    escondia o resto da sidebar SÓ na página do Playground. Trava o balanço do BLOCO.
    (perdido 2x no squash-drop do #436 — por isso o teste do bloco, não só o total.)"""
    import re
    base = BASE.read_text(encoding="utf-8")
    a = base.rfind("<div", 0, base.index('id="tour-nav-mesh"'))
    b = base.rfind("<div", 0, base.index('id="tour-nav-tools"'))
    block = base[a:b]
    opens = len(re.findall(r"<div(?:\s|>)", block))
    closes = len(re.findall(r"</div>", block))
    assert opens == closes, f"bloco AI Mesh desbalanceado: {opens} abrem vs {closes} fecham (fecha o nav cedo)"
    # e os itens DEPOIS do AI Mesh continuam no nav
    assert base.index('href="/mesh/playground"') < base.index("Ferramentas")
    assert 'href="/mcp"' in base and 'href="/settings"' in base


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


def test_console_tem_aba_http_e_mapa_de_erros():
    src = PG.read_text(encoding="utf-8")
    assert 'data-testid="pg-tab-http"' in src and 'data-testid="pg-http"' in src
    # status + rate-limit lidos dos headers REAIS da resposta
    assert "X-RateLimit-Remaining" in src and "this.http = {" in src
    # mapa de erros: 401/400/404 simuláveis + 409/422/429 na referência
    assert "ERRORS:" in src and "async testError(code)" in src
    for c in ("401", "400", "404", "409", "422", "429"):
        assert c in src
    assert "testError(e.code)" in src


def test_console_tem_historico_repl():
    src = PG.read_text(encoding="utf-8")
    assert 'data-testid="pg-history"' in src
    assert "_pushHistory()" in src
    assert "restore(h)" in src and "re-rodar" in src and "clearHistory()" in src
    # REPL persiste no navegador (sobrevive ao reload)
    assert "localStorage.setItem('pg_history'" in src and "_loadHistory()" in src


def test_historico_persiste_no_servidor():
    """Feature 1: o histórico agora é PERSISTIDO no servidor (por-usuário), com o
    localStorage como cache offline. A página chama o CRUD de /playground/runs."""
    src = PG.read_text(encoding="utf-8")
    # POST otimista + GET no load + DELETE (tudo / por item)
    assert "api.post('/api/v1/playground/runs'" in src
    assert "api.get('/api/v1/playground/runs" in src
    assert "api.del('/api/v1/playground/runs'" in src           # limpar tudo
    assert "api.del('/api/v1/playground/runs/'" in src          # remover um
    # métodos novos do ciclo servidor-backed
    assert "_persistRun(" in src and "_mapRun(" in src and "removeRun(h)" in src
    # cache offline preservado (sobrevive offline) + tz-correto no carimbo do servidor
    assert "localStorage.setItem('pg_history'" in src
    assert "window.tzTime(r.created_at)" in src


def test_layout_lado_a_lado():
    src = PG.read_text(encoding="utf-8")
    assert "lg:grid-cols-2" in src   # builder | resposta lado a lado
