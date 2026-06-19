"""Jornada E2E: conversa real com um agente no Workspace (chat ponta-a-ponta).

O fluxo nº 1 do usuário final: abre o Workspace com um agente, digita uma
mensagem, envia, e recebe a resposta do agente. Envolve round-trip de LLM
(POST /api/v1/workspace/chat) — não-determinístico e potencialmente lento.

Assert resiliente (como pipeline/RAG): após enviar, a UI deve renderizar a
mensagem do usuário (dispatch confirmado) e, em seguida, uma BOLHA DO ASSISTENTE
— seja a resposta real, seja uma bolha de erro tratada. Ambas significam que o
chat fechou o ciclo ponta-a-ponta. O acerto/qualidade do LLM é coberto por
testes de backend, não por E2E de interface.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _first_active_agent(api):
    r = api.get("/api/v1/agents?limit=50")
    if r.status_code != 200:
        return None
    for a in r.json().get("agents", []):
        if a.get("status") == "active" and a.get("id"):
            return a
    return None


def test_chat_real_com_agente(authed_page, api):
    agent = _first_active_agent(api)
    if not agent:
        pytest.skip("Nenhum agente ativo no ambiente para conversar.")

    page = authed_page
    # ?agent=<id> auto-seleciona o agente → habilita o input.
    page.goto(f"/workspace?agent={agent['id']}", wait_until="domcontentloaded")

    chat_input = page.get_by_test_id("chat-input")
    expect(chat_input).to_be_enabled(timeout=15_000)

    assistant = page.get_by_test_id("chat-msg-assistant")
    before = assistant.count()

    chat_input.fill("Olá, este é um teste E2E. Responda em uma frase curta.")
    page.get_by_test_id("chat-send").click()

    # (1) dispatch: a mensagem do usuário entra na conversa imediatamente.
    expect(
        page.get_by_test_id("chat-msg-user").filter(has_text="teste E2E")
    ).to_be_visible(timeout=10_000)

    # (2) desfecho: surge ao menos uma nova bolha de assistente (resposta real
    # OU erro tratado) após o round-trip do LLM. Tolerante a nota de sistema +
    # resposta (≥ before+1).
    page.wait_for_function(
        "before => document.querySelectorAll('[data-testid=chat-msg-assistant]').length > before",
        arg=before,
        timeout=120_000,
    )
