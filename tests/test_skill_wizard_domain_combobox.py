"""Wizard de SKILL.md (skill_form.html) — campo Domínio vira COMBOBOX.

User pediu: no campo "Domínio" deve dar pra escolher um domínio existente numa
lista OU informar um novo. Antes era um <input> de texto livre, sem sugestões.

Fix: <input list="wizard-domain-list"> + <datalist> populado com os domínios
existentes (GET /api/v1/domains, carregado no load()). O datalist é nativo —
sugere os existentes e ainda aceita um valor novo digitado. Varredura de template.
"""
from __future__ import annotations

from pathlib import Path

import pytest

PG = Path("app/templates/pages/skill_form.html")


@pytest.fixture(scope="module")
def html() -> str:
    return PG.read_text(encoding="utf-8")


def test_input_dominio_usa_datalist(html):
    # o input aponta pra um datalist (combobox nativo: escolher OU digitar)
    assert 'list="wizard-domain-list"' in html
    assert 'x-model="wizardDomain"' in html
    assert 'data-testid="wizard-domain"' in html


def test_datalist_popula_dos_dominios_existentes(html):
    # <datalist> com as opções vindas de wizardDomains (x-for sobre os domínios)
    assert '<datalist id="wizard-domain-list">' in html
    assert 'x-for="d in wizardDomains"' in html
    assert ':value="d.name"' in html


def test_dominios_carregados_no_load(html):
    # estado + fetch dos domínios existentes no load()
    assert "wizardDomains: []" in html
    assert "api.get('/api/v1/domains')" in html


def test_aceita_dominio_novo(html):
    """datalist NÃO restringe o valor — o input segue de texto livre (digitar novo).
    Garante que não viramos um <select> (que travaria em valores existentes)."""
    # o campo é um <input> (não <select>) ligado a wizardDomain
    assert '<input x-model="wizardDomain" list="wizard-domain-list"' in html
