"""Recolher/expandir nas seções do painel de pipeline (2026-06-19).

Usuário pediu: aplicar o MESMO padrão de recolher/expandir (setinha que gira)
já usado nas seções de status de pipeline também em "Agentes no pipeline" e
"Incluir agentes". Estes testes travam o wiring: cabeçalho clicável que chama
toggleSection + corpo com x-show na chave correta, reusando o padrão existente.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh_flow.html"


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_members_section_is_collapsible(html: str):
    assert "toggleSection('members')" in html
    assert 'x-show="sectionOpen.members"' in html
    # a setinha gira como nas demais seções (rotate-90 quando aberto)
    assert "sectionOpen.members ? 'rotate-90' : ''" in html


def test_include_section_is_collapsible(html: str):
    assert "toggleSection('include')" in html
    assert 'x-show="sectionOpen.include"' in html
    assert "sectionOpen.include ? 'rotate-90' : ''" in html


def test_new_keys_default_open(html: str):
    # ambas começam expandidas (true) — reusa o mesmo objeto sectionOpen
    assert "members: true" in html
    assert "include: true" in html


def test_simulate_section_is_collapsible(html: str):
    """Seção 'Simular com estes dados' (no modal de regra condicional) também
    recolhe/expande, mesmo padrão."""
    assert "toggleSection('simulate')" in html
    assert 'x-show="sectionOpen.simulate"' in html
    assert "sectionOpen.simulate ? 'rotate-90' : ''" in html
    assert "simulate: true" in html


def test_reuses_existing_toggle_and_chevron(html: str):
    # mesmo método e mesmo path de chevron das seções de status (sem reinventar)
    assert "toggleSection(key) {" in html or "toggleSection(key)" in html
    # status (template) + members + include + simulate
    assert html.count('d="M9 6l6 6-6 6"') >= 4
