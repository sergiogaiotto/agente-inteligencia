"""Jornada E2E: rodar uma avaliação no Harness (§9.5).

Fluxo de qualidade: selecionar agente + release + tipo (baseline) e executar a
avaliação contra o Golden Dataset. O run invoca o agente em cada caso via LLM —
NÃO-determinístico e LENTO (~2min para ~10 casos). Por isso `slow` + timeout
generoso, e assert resiliente: após executar, surge uma NOVA linha de execução
na lista (o run terminou e a UI renderizou o desfecho — aprovado/rejeitado).

PULA se não houver agente ativo, release ou casos no Golden Dataset.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _first(items, pred=lambda x: True):
    for x in items:
        if pred(x):
            return x
    return None


def test_rodar_avaliacao_no_harness(authed_page, api):
    agents = api.get("/api/v1/agents?limit=50").json().get("agents", [])
    agent = _first(agents, lambda a: a.get("status") == "active" and a.get("id"))

    rel = api.get("/api/v1/releases").json()
    releases = rel if isinstance(rel, list) else rel.get("releases", rel.get("items", []))
    release = _first(releases, lambda r: r.get("id"))

    gc = api.get("/api/v1/gold-cases").json()
    cases = gc if isinstance(gc, list) else gc.get("cases", gc.get("gold_cases", gc.get("items", [])))

    if not agent or not release or not cases:
        pytest.skip("Harness sem pré-requisitos (agente ativo + release + golden cases).")

    page = authed_page
    page.goto("/harness", wait_until="domcontentloaded")

    # abre o formulário de execução
    page.get_by_test_id("harness-new-run").click()
    agent_sel = page.get_by_test_id("harness-agent")
    expect(agent_sel).to_be_visible(timeout=10_000)
    agent_sel.select_option(value=agent["id"])
    page.get_by_test_id("harness-release").select_option(value=release["id"])
    page.get_by_test_id("harness-runtype").select_option(value="baseline")

    runs = page.get_by_test_id("harness-run-row")
    before = runs.count()

    page.get_by_test_id("harness-run").click()

    # (1) dispatch: o botão entra em "Executando..." (determinístico).
    expect(page.get_by_test_id("harness-run")).to_contain_text("Executando", timeout=10_000)

    # (2) desfecho: surge uma nova linha de execução (run concluído). O run chama
    # LLM em ~10 casos → lento; timeout generoso. Latência ≠ defeito de UI.
    page.wait_for_function(
        "before => document.querySelectorAll('[data-testid=harness-run-row]').length > before",
        arg=before,
        timeout=240_000,
    )
