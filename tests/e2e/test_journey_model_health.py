"""Jornada E2E: o chip de Saúde dos Modelos no header informa o que será usado.

Ao entrar na plataforma, o chip (em base.html, presente em toda tela) busca
GET /api/v1/llm/health e mostra o que será usado em chat/roteamento + embeddings,
sinalizando indisponibilidade/fallback. Aqui validamos que o chip renderiza,
abre o painel e lista o mapa de modelos (estrutura determinística; os valores
variam conforme Configurações/disponibilidade).
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


def test_chip_saude_dos_modelos(authed_page):
    page = authed_page
    page.goto("/", wait_until="domcontentloaded")

    chip = page.get_by_test_id("model-health-chip")
    expect(chip).to_be_visible(timeout=10_000)
    chip.click()

    panel = page.get_by_test_id("model-health-panel")
    expect(panel).to_be_visible(timeout=10_000)

    # após o probe (assíncrono), o painel lista os papéis de chat + embeddings
    expect(panel).to_contain_text("Embeddings", timeout=25_000)
    expect(panel).to_contain_text("Tool calling", timeout=25_000)
