"""Jornada E2E: login pelo formulário real (não via cookie injetado).

Valida o caminho que todo usuário percorre: abrir /login, digitar credenciais,
clicar Entrar e cair autenticado no Dashboard.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


def test_login_via_form(page, base_url, e2e_auth):
    if not e2e_auth["available"]:
        pytest.skip(
            "Sem credenciais E2E válidas — rode "
            "`docker exec agente_app python scripts/seed_e2e_user.py`."
        )

    page.goto("/login", wait_until="domcontentloaded")

    user = page.get_by_test_id("login-username")
    expect(user).to_be_visible(timeout=10_000)
    user.fill(e2e_auth["username"])
    page.get_by_test_id("login-password").fill(e2e_auth["password"])
    page.get_by_test_id("login-submit").click()

    # Sucesso = sai de /login e cai numa tela autenticada (Dashboard).
    page.wait_for_url(lambda u: not u.rstrip("/").endswith("/login"), timeout=15_000)
    assert "/login" not in page.url
    expect(page).to_have_title(re.compile(r"Maestro"))
