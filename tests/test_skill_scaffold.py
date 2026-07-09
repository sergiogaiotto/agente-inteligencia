"""F4 — o editor de nova skill (/skills/new) deve abrir com um scaffold que já
cobre TODAS as seções obrigatórias, para o usuário não descobrir as faltantes só
ao clicar em Validar.

O scaffold vive no método JS `_newSkillScaffold()` em skill_form.html (Alpine).
Estes testes extraem esse scaffold do template e o cruzam com a fonte da verdade
do parser (REQUIRED_SECTIONS) — guardando a sincronia e provando que o scaffold
passa a validação sem 'Seção obrigatória ausente'.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.skill_parser.parser import REQUIRED_SECTIONS, parse_skill_md

_SKILL_FORM = (
    Path(__file__).resolve().parent.parent
    / "app" / "templates" / "pages" / "skill_form.html"
)


def _scaffold_md() -> str:
    """Reconstrói o markdown do scaffold a partir do array JS de _newSkillScaffold()."""
    html = _SKILL_FORM.read_text(encoding="utf-8")
    m = re.search(r"_newSkillScaffold\(\)\s*\{\s*return\s*\[(.*?)\]", html, re.S)
    assert m, "método _newSkillScaffold() não encontrado em skill_form.html"
    body = m.group(1)
    lines = re.findall(r"'((?:[^'\\]|\\.)*)'", body)
    return "\n".join(lines)


def test_scaffold_covers_all_required_sections():
    md = _scaffold_md()
    for section in REQUIRED_SECTIONS:
        assert f"## {section}" in md, (
            f"scaffold do editor não cobre a seção obrigatória: ## {section}. "
            f"Atualize _newSkillScaffold() em skill_form.html."
        )


def test_scaffold_passes_parser_without_missing_sections():
    parsed = parse_skill_md(_scaffold_md())
    missing = [e for e in parsed.validation_errors if "Seção obrigatória ausente" in e]
    assert not missing, f"o scaffold ainda dispara seções faltantes: {missing}"
