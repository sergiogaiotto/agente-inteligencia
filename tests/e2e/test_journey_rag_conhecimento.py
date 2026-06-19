"""Jornada E2E: criar uma base de conhecimento e ingerir texto pela UI (/rag).

Duas partes:
  1. Criar fonte de conhecimento (determinístico) → aparece na lista.
  2. Ingerir texto: abre o modal, cola texto, "Ingerir" → a UI atinge um ESTADO
     TERMINAL (resultado OU erro renderizado).

Por que o passo 2 é resiliente: a ingestão chama o serviço de embeddings
(Azure/pgvector), que pode NÃO estar configurado no ambiente de teste (retorna
503). Como na jornada de pipeline, asserimos que a interface fechou o ciclo
ponta-a-ponta — disparou a ingestão e renderizou o desfecho (sucesso ou erro).
O acerto do embedding é responsabilidade de testes de backend, não do E2E de UI.
"""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


def test_criar_base_e_ingerir_texto(authed_page, api):
    page = authed_page
    name = f"E2E KB {uuid.uuid4().hex[:8]}"

    page.goto("/rag", wait_until="domcontentloaded")

    # ── Parte 1: criar a fonte de conhecimento ──
    page.get_by_test_id("kb-new").click()
    kb_name = page.get_by_test_id("kb-name")
    expect(kb_name).to_be_visible(timeout=10_000)
    kb_name.fill(name)
    page.get_by_test_id("kb-save").click()

    # a base aparece na lista (determinístico)
    card = page.get_by_test_id("kb-row").filter(has_text=name)
    expect(card).to_be_visible(timeout=30_000)

    # ── Parte 2: ingerir texto (resiliente a embeddings ausentes) ──
    card.get_by_test_id("kb-ingest-open").click()
    txt = page.get_by_test_id("kb-ingest-text")
    expect(txt).to_be_visible(timeout=10_000)
    txt.fill(
        "O prazo de garantia legal é de 90 dias para produtos duráveis "
        "conforme o CDC. A troca por arrependimento pode ser solicitada em "
        "até 7 dias a partir do recebimento."
    )
    page.get_by_test_id("kb-ingest-submit").click()

    # Estado terminal: resultado (chunks) OU erro renderizado. offsetParent==null
    # ⇒ oculto (x-if fechado / display:none). Tolera 503 de embeddings.
    _TERMINAL = (
        "() => { const vis = el => el && el.offsetParent !== null;"
        " const r = document.querySelector('[data-testid=kb-ingest-result]');"
        " const e = document.querySelector('[data-testid=kb-ingest-error]');"
        " return vis(r) || vis(e); }"
    )
    page.wait_for_function(_TERMINAL, timeout=60_000)

    # ── teardown: remove a base criada ──
    try:
        r = api.get("/api/v1/knowledge-sources")
        if r.status_code == 200:
            data = r.json()
            sources = data if isinstance(data, list) else data.get(
                "sources", data.get("knowledge_sources", data.get("items", []))
            )
            for s in sources:
                if s.get("name") == name and s.get("id"):
                    api.delete(f"/api/v1/knowledge-sources/{s['id']}")
    except Exception:
        pass
