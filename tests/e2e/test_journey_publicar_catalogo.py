"""Jornada E2E: publicar um agente no Catálogo pelo wizard de 4 passos.

Caminho real: cria um agente (via API, setup) → /catalog/publish → escolhe o
artefato → metadata (versão) → capability → revisão → "Confirmar e Submeter" →
redirect p/ /catalog/{id}. Determinístico (não chama LLM). Limpa entry+agente.
"""
from __future__ import annotations

import re
import uuid

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.fixture
def throwaway_agent(api):
    """Cria um agente descartável p/ publicar; remove no teardown."""
    name = f"E2E Pub {uuid.uuid4().hex[:8]}"
    payload = {
        "name": name,
        "kind": "subagent",
        "description": "Agente descartável para teste E2E de publicação.",
        "task_type": "instruct",
        "system_prompt": (
            "Você é um agente de teste de publicação no catálogo. Responda de "
            "forma objetiva às solicitações, confirmando dados antes de agir e "
            "sem inventar informações fora da evidência recebida do usuário."
        ),
    }
    r = api.post("/api/v1/agents", json=payload)
    assert r.status_code in (200, 201), f"falha ao criar agente: {r.status_code} {r.text}"
    agent_id = r.json().get("id")
    yield {"id": agent_id, "name": name}
    if agent_id:
        try:
            api.delete(f"/api/v1/agents/{agent_id}")
        except Exception:
            pass


def test_publicar_agente_no_catalogo(authed_page, api, throwaway_agent):
    page = authed_page
    name = throwaway_agent["name"]

    page.goto("/catalog/publish", wait_until="domcontentloaded")

    # ── Passo 1: escolher o artefato (o agente recém-criado) ──
    artifact_list = page.get_by_test_id("pub-artifact-list")
    artifact = artifact_list.get_by_text(name, exact=False)
    expect(artifact).to_be_visible(timeout=15_000)
    artifact.click()
    page.get_by_test_id("pub-next").click()

    # ── Passo 2: metadata (nome pré-preenchido; fixa versão válida) ──
    version = page.get_by_test_id("pub-version")
    expect(version).to_be_visible()
    version.fill("1.0.0")
    page.get_by_test_id("pub-next").click()

    # ── Passo 3: capability (defaults já satisfazem canAdvance) ──
    page.get_by_test_id("pub-next").click()

    # ── Passo 4: revisão → submeter ──
    submit = page.get_by_test_id("pub-submit")
    expect(submit).to_be_visible()
    submit.click()

    # Sucesso = redirect p/ /catalog/{entryId}. Timeout folgado: o submit faz 3
    # chamadas encadeadas (create+capability+submit) que podem demorar sob carga.
    page.wait_for_url(re.compile(r"/catalog/[0-9a-fA-F-]{8,}"), timeout=40_000)
    m = re.search(r"/catalog/([0-9a-fA-F-]{8,})", page.url)
    entry_id = m.group(1) if m else None
    assert entry_id, f"não capturei o entry_id da URL: {page.url}"

    # ── teardown: tenta remover a entry (pode 409 se já submitted — ok) ──
    try:
        api.delete(f"/api/v1/catalog/entries/{entry_id}")
    except Exception:
        pass
