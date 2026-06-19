"""Jornada E2E: criar um agente pela UI (wizard completo) e vê-lo na lista.

Caminho real: /agents/new → escolher camada (Especialista) → nome → system
prompt → Revisão (pre-flight) → "Criar Agente" → redirect p/ /agents → o agente
aparece na lista. Determinístico (não chama LLM). Limpa o agente no teardown.
"""
from __future__ import annotations

import uuid

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e

# Pre-flight pode avisar sobre pass-through abaixo de 200 chars; mandamos um
# prompt longo e específico para não cair em "blocked" e habilitar o salvar.
SYSTEM_PROMPT = (
    "Você é um especialista de teste E2E. Sua tarefa é responder de forma "
    "objetiva e cordial a perguntas do usuário sobre o status do pedido. "
    "Sempre confirme o número do pedido antes de responder, peça o CPF quando "
    "faltar identificação e nunca invente informações que não estejam na "
    "evidência fornecida. Em caso de dúvida, encaminhe ao atendimento humano."
)


def test_criar_agente_aparece_na_lista(authed_page, api):
    page = authed_page
    name = f"E2E Agente {uuid.uuid4().hex[:8]}"

    page.goto("/agents/new", wait_until="domcontentloaded")

    # Passo 1 — camada + nome
    page.get_by_test_id("agent-kind-subagent").click()
    page.get_by_test_id("agent-name").fill(name)

    # Passo 3 (índice 2) — system prompt
    page.get_by_test_id("agent-step-2").click()
    prompt = page.get_by_test_id("agent-system-prompt")
    expect(prompt).to_be_visible()
    prompt.fill(SYSTEM_PROMPT)

    # Passo 4 (índice 3) — Revisão: pre-flight roda no $watch(step===3)
    page.get_by_test_id("agent-step-3").click()
    save = page.get_by_test_id("agent-save")
    expect(save).to_be_visible()
    expect(save).to_be_enabled(timeout=15_000)  # pre-flight terminou e não bloqueou
    save.click()

    # save() redireciona p/ /agents após ~800ms
    page.wait_for_url("**/agents", timeout=15_000)
    expect(
        page.get_by_test_id("agent-row-name").filter(has_text=name)
    ).to_be_visible(timeout=15_000)

    # ── teardown: remove o agente criado para a suíte ser idempotente ──
    try:
        r = api.get("/api/v1/agents")
        if r.status_code == 200:
            agents = r.json()
            if isinstance(agents, dict):
                agents = agents.get("agents", agents.get("items", []))
            for a in agents:
                if a.get("name") == name and a.get("id"):
                    api.delete(f"/api/v1/agents/{a['id']}")
    except Exception:
        pass
