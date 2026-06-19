"""Jornada E2E: alterar uma configuração pela UI e verificar que persiste.

Usa o setting SEGURO `timezone` (cosmético: só muda a exibição de datas, não a
lógica). Altera pelo select → "Salvar" → confirma que persistiu (via API, fonte
da verdade) e que a página reflete o novo valor ao recarregar. Restaura o valor
ORIGINAL no teardown para não poluir o ambiente.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


def _get_tz(api) -> str:
    d = api.get("/api/v1/settings").json()
    s = d.get("settings", d) if isinstance(d, dict) else {}
    return s.get("timezone") or "America/Sao_Paulo"


def test_alterar_timezone_persiste(authed_page, api):
    original = _get_tz(api)
    new_tz = "America/New_York" if original != "America/New_York" else "America/Sao_Paulo"

    page = authed_page
    try:
        page.goto("/settings", wait_until="domcontentloaded")
        page.get_by_test_id("settings-tab-platform").click()

        sel = page.get_by_test_id("setting-timezone")
        expect(sel).to_be_visible(timeout=10_000)
        sel.select_option(new_tz)

        page.get_by_test_id("setting-save-platform").click()

        # Persistência é a fonte da verdade (gravação é async → pequeno retry).
        persisted = None
        for _ in range(12):
            persisted = _get_tz(api)
            if persisted == new_tz:
                break
            time.sleep(0.5)
        assert persisted == new_tz, f"timezone não persistiu: {persisted} != {new_tz}"

        # E a UI reflete o novo valor ao recarregar.
        page.reload(wait_until="domcontentloaded")
        page.get_by_test_id("settings-tab-platform").click()
        expect(page.get_by_test_id("setting-timezone")).to_have_value(new_tz, timeout=10_000)
    finally:
        # Restaura o valor original — não deixa o ambiente do usuário alterado.
        try:
            api.put("/api/v1/settings", json={"timezone": original})
        except Exception:
            pass
