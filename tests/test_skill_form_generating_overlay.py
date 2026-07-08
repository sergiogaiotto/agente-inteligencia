"""Smoke do template skill_form.html — overlay "Gerando SKILL.md…" no editor.

Achado no teste E2E (Cenário A): durante a geração da skill pelo wizard "IA, me
ajude", o editor SKILL.md PISCAVA com o template vazio (0 chars) antes de popular,
parecendo que falhou. Fix: um overlay com spinner cobre o textarea enquanto
`wizardLoading` está ativo, e o textarea fica desabilitado.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "skill_form.html").read_text(encoding="utf-8")


class TestGeneratingOverlay:
    def test_overlay_present(self, html):
        assert 'data-testid="skill-raw-generating"' in html
        assert "Gerando SKILL.md" in html

    def test_overlay_gated_by_wizard_loading(self, html):
        # o overlay só aparece durante a geração
        idx = html.index('data-testid="skill-raw-generating"')
        block = html[idx - 220: idx + 120]
        assert 'x-show="wizardLoading"' in block

    def test_textarea_disabled_while_generating(self, html):
        # o textarea não aceita digitação enquanto gera
        idx = html.index('data-testid="skill-raw"')
        block = html[idx: idx + 260]
        assert ':disabled="wizardLoading"' in block
