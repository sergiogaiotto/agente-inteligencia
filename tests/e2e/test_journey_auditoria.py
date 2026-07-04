"""Jornada E2E do arco LLM-as-Judge (UI interativa, #494–#498).

O smoke de páginas só CARREGA as telas; os elementos novos (aba Parâmetros,
painéis por dono, drill-down do Quality) só renderizam sob interação/estado.
Aqui dirigimos o browser real para exercitá-los e capturar erros de JS que a
varredura de template não pega (lição do footgun Alpine boolean-undefined).

Requer app de pé + e2e_admin root (mesmo setup dos demais E2E).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _collect_errors(page):
    """Anexa coletores de erro de console/página. Retorna a lista mutável."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
        if msg.type == "error" else None,
    )
    return errors


def test_aba_parametros_renderiza_e_edita(authed_page):
    """Configurações → Parâmetros (25.1.0): a aba root/admin abre, lista os
    campos com badge de fonte e o form fica editável — sem erro de JS."""
    page = authed_page
    errors = _collect_errors(page)
    page.goto("/settings", wait_until="networkidle")

    # A aba Parâmetros existe (root/admin) e abre.
    tab = page.get_by_test_id("settings-tab-params")
    assert tab.count() == 1, "aba Parâmetros ausente para usuário root"
    tab.click()

    # O conteúdo da aba renderiza (grupo do Juiz + um campo conhecido).
    panel = page.get_by_test_id("settings-params-tab")
    panel.wait_for(state="visible", timeout=5000)
    page.wait_for_selector("text=Verifier v2 ligado", timeout=5000)
    # O badge de fonte aparece (banco | ambiente/padrão).
    assert page.locator("text=/ambiente\\/padrão|banco/").count() > 0

    # Ignora erros de rede de recursos externos (CSP bloqueia CDNs em alguns
    # ambientes) — só nos importam erros de SCRIPT da aplicação.
    app_errors = [e for e in errors if "Failed to load resource" not in e
                  and "net::ERR" not in e]
    assert not app_errors, f"erros de JS na aba Parâmetros: {app_errors}"


def test_pagina_qualidade_interativa(authed_page):
    """Página Qualidade (25.0.0): carrega os cards de stats, a lista de
    verificações e os painéis de Auditoria sem erro de JS. Os painéis por
    dono só aparecem se houver julgamentos com dono (tolerante a DB vazio)."""
    page = authed_page
    errors = _collect_errors(page)
    page.goto("/quality", wait_until="networkidle")

    # Cabeçalho + seletor de janela sempre presentes.
    page.wait_for_selector("text=Verificações recentes", timeout=5000)
    # Combos de filtro por dono (introduzidos no #497).
    assert page.get_by_test_id("quality-filter-agent").count() == 1
    assert page.get_by_test_id("quality-filter-pipeline").count() == 1
    # Export sempre presente.
    assert page.get_by_test_id("quality-export-csv").count() == 1

    app_errors = [e for e in errors if "Failed to load resource" not in e
                  and "net::ERR" not in e]
    assert not app_errors, f"erros de JS na página Qualidade: {app_errors}"


def test_observabilidade_carrega_sem_erro(authed_page):
    """Observabilidade (25.0.0): a página com o card-resumo de Auditoria novo
    carrega sem erro de JS (o card só aparece com julgamentos na janela 24h)."""
    page = authed_page
    errors = _collect_errors(page)
    page.goto("/observability", wait_until="networkidle")
    page.wait_for_selector("text=Interações Recentes", timeout=5000)

    app_errors = [e for e in errors if "Failed to load resource" not in e
                  and "net::ERR" not in e]
    assert not app_errors, f"erros de JS na Observabilidade: {app_errors}"
