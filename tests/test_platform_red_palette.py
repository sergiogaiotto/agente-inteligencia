"""Platform-wide red+white palette enforcement.

User pediu (2026-05-30): "para os tons de roxo você deve usar tons de
vermelho e branco em toda plataforma".

PR #209 fez bulk replace de violet/fuchsia/purple → red em todos os
templates e statics. Estes testes são GUARD-RAIL: garantem que ninguém
re-introduza tons de roxo no futuro sem perceber.

Se um teste aqui falhar:
- Adicionou intencionalmente? Edite este teste pra reconhecer a exceção
  (com comentário explicando o porquê).
- Não foi intencional? Troque por red-* equivalente.
"""
from __future__ import annotations

import glob
import re

import pytest


PURPLE_PATTERN = re.compile(r"\b(violet|fuchsia|purple)-\d+(?:/\d+)?")


def _scan(paths_glob: str) -> dict[str, list[str]]:
    """Retorna {filepath: [matches]} pra todos os arquivos que casam o glob."""
    out: dict[str, list[str]] = {}
    for f in glob.glob(paths_glob, recursive=True):
        try:
            content = open(f, encoding="utf-8").read()
        except Exception:
            continue
        matches = PURPLE_PATTERN.findall(content)
        if matches:
            out[f] = matches
    return out


# ────────────────────────────────────────────────────────────────
# Hardening
# ────────────────────────────────────────────────────────────────


class TestNoRoxoInTemplates:
    def test_zero_violet_in_html_templates(self):
        """Nenhum arquivo .html em app/templates/ pode ter `violet-NNN`."""
        found = _scan("app/templates/**/*.html")
        # Filtra só violet específicamente
        violet_files = {
            f: [m for m in ms if m == "violet"]
            for f, ms in found.items()
        }
        violet_files = {f: ms for f, ms in violet_files.items() if ms}
        assert not violet_files, (
            f"Resíduo violet em templates: {list(violet_files.keys())[:5]}"
        )

    def test_zero_fuchsia_in_html_templates(self):
        found = _scan("app/templates/**/*.html")
        fuchsia_files = {
            f: [m for m in ms if m == "fuchsia"]
            for f, ms in found.items()
        }
        fuchsia_files = {f: ms for f, ms in fuchsia_files.items() if ms}
        assert not fuchsia_files, (
            f"Resíduo fuchsia em templates: {list(fuchsia_files.keys())[:5]}"
        )

    def test_zero_purple_in_html_templates(self):
        found = _scan("app/templates/**/*.html")
        purple_files = {
            f: [m for m in ms if m == "purple"]
            for f, ms in found.items()
        }
        purple_files = {f: ms for f, ms in purple_files.items() if ms}
        assert not purple_files, (
            f"Resíduo purple em templates: {list(purple_files.keys())[:5]}"
        )

    def test_zero_purple_in_static_css(self):
        """CSS static também (se existir)."""
        found = _scan("app/static/**/*.css")
        assert not found, f"Resíduo em CSS: {list(found.keys())[:5]}"

    def test_zero_purple_in_static_js(self):
        """JS static — algumas vezes tem classes inline."""
        found = _scan("app/static/**/*.js")
        assert not found, f"Resíduo em JS: {list(found.keys())[:5]}"


# ────────────────────────────────────────────────────────────────
# Sanity: red foi efetivamente aplicado
# ────────────────────────────────────────────────────────────────


class TestRedAppliedToMajorFiles:
    """Sanity check: os arquivos que tinham mais violet/fuchsia (per survey
    do PR #209) agora têm red-* nas mesmas regiões."""

    @pytest.mark.parametrize("filepath,min_count", [
        ("app/templates/pages/skill_form.html", 50),
        ("app/templates/pages/workspace.html", 30),
        ("app/templates/pages/api_connectors.html", 30),
        ("app/templates/pages/tools.html", 20),
        ("app/templates/pages/settings.html", 15),
    ])
    def test_file_has_significant_red_usage(self, filepath, min_count):
        """Cada arquivo que tinha muito violet/fuchsia agora tem ≥min_count
        ocorrências de red-* — não foi só substituído por nada."""
        from pathlib import Path
        content = Path(filepath).read_text(encoding="utf-8")
        red_count = len(re.findall(r"\bred-\d+(?:/\d+)?", content))
        assert red_count >= min_count, (
            f"{filepath} tem só {red_count} red-* (esperava ≥ {min_count})"
        )
