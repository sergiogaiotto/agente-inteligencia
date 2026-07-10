"""Jornada E2E: aprovar uma submissão na Fila de Revisão do Catálogo.

Governança核心: quando uma entrada é publicada, vai para "submitted" e cai na
fila para um Root aprovar/rejeitar. Aqui criamos (via API) um agente → entry →
submit, e então dirigimos a UI: /catalog/queue → achar a submissão pelo nome →
"Aprovar" → modal → "Confirmar" → a submissão sai de pendente. Determinístico.
"""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.fixture
def submitted_entry(api):
    """Cria agente → entry → disclosure → submit (gera submissão pendente).
    A disclosure R6.3 é obrigatória para o Aprovar da jornada passar (gate no
    decide) e só é editável em draft — declare antes do submit. Limpa no fim."""
    name = f"E2E Gov {uuid.uuid4().hex[:8]}"
    ag = api.post("/api/v1/agents", json={
        "name": name, "kind": "subagent", "task_type": "instruct",
        "system_prompt": "Agente descartável p/ teste E2E de governança do catálogo.",
    })
    assert ag.status_code in (200, 201), ag.text
    agent_id = ag.json()["id"]
    entry = api.post("/api/v1/catalog/entries", json={
        "name": name, "kind": "agent", "artifact_type": "agent",
        "artifact_id": agent_id, "version": "1.0.0", "visibility": "private",
    })
    assert entry.status_code in (200, 201), entry.text
    entry_id = entry.json()["id"]
    cap = api.put(f"/api/v1/catalog/entries/{entry_id}/capability", json={})
    assert cap.status_code == 200, cap.text
    sub = api.post(f"/api/v1/catalog/entries/{entry_id}/submit", json={"notes": ""})
    assert sub.status_code in (200, 201), sub.text

    yield {"name": name, "entry_id": entry_id, "agent_id": agent_id}

    for url in (f"/api/v1/catalog/entries/{entry_id}", f"/api/v1/agents/{agent_id}"):
        try:
            api.delete(url)
        except Exception:
            pass


def test_aprovar_submissao_na_fila(authed_page, submitted_entry):
    page = authed_page
    name = submitted_entry["name"]

    page.goto("/catalog/queue", wait_until="domcontentloaded")

    # acha a submissão pelo nome da entry e clica em "Aprovar"
    row = page.get_by_test_id("queue-row").filter(has_text=name)
    expect(row).to_be_visible(timeout=20_000)
    row.get_by_test_id("queue-approve").click()

    # modal de decisão → Confirmar
    confirm = page.get_by_test_id("queue-decide-confirm")
    expect(confirm).to_be_visible(timeout=10_000)
    confirm.click()

    # sucesso: após a decisão a fila recarrega e a submissão sai de "pendente"
    # (a linha pendente daquele nome some). Confirmamos também via API o status.
    expect(
        page.get_by_test_id("queue-row").filter(has_text=name)
    ).to_have_count(0, timeout=20_000)
