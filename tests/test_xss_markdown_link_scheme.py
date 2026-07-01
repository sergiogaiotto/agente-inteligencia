"""Regressão anti-XSS nos renderers de link markdown (SKILL.md §4 / CWE-79).

Trava a fiação segura em workspace.html e tools.html: o padrão vulnerável
`href="$2"` (URL crua do markdown direto no atributo) NÃO pode voltar, e o
callback de link precisa passar pelo allowlist de esquema `_safeHref`.

A prova da LÓGICA (bloqueio de javascript:/data:/vbscript:, preservação de
http/https/mailto, encode de aspas) roda em Node sobre o `_safeHref` extraído dos
próprios templates — ver scripts de verificação; aqui garantimos que o código
seguro está presente e o inseguro ausente.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATES = [
    Path("app/templates/pages/workspace.html"),
    Path("app/templates/pages/tools.html"),
]


@pytest.mark.parametrize("path", _TEMPLATES, ids=lambda p: p.name)
def test_no_raw_href_dollar2_in_link_render(path):
    src = path.read_text(encoding="utf-8")
    # O padrão vulnerável era: .replace(/\[...\]\(...\)/g, '<a href="$2" ...>$1</a>')
    assert 'href="$2"' not in src, (
        f"{path}: padrão de link inseguro (href=\"$2\") reintroduzido — "
        "a URL do markdown volta crua ao atributo (XSS)."
    )


@pytest.mark.parametrize("path", _TEMPLATES, ids=lambda p: p.name)
def test_safehref_helper_present(path):
    src = path.read_text(encoding="utf-8")
    assert "_safeHref(url)" in src, f"{path}: helper _safeHref ausente."
    # allowlist de esquema + bloqueio de esquema explícito não permitido
    assert "https?:|mailto:" in src, f"{path}: allowlist de esquema ausente em _safeHref."


@pytest.mark.parametrize("path", _TEMPLATES, ids=lambda p: p.name)
def test_link_callback_uses_safehref(path):
    src = path.read_text(encoding="utf-8")
    # o replace de link markdown deve chamar _safeHref no href
    assert "this._safeHref(url)" in src, (
        f"{path}: o callback de link não passa a URL por _safeHref."
    )
