"""UI: o rodapé da sidebar mostra a VERSÃO do produto (PR-driven) no lugar do
texto técnico "LangGraph · LangFuse · FSM §15".

Esquema MAJOR.MEDIUM.MINOR, bumped a cada PR — ver app/core/version.py.
Decidido com o usuário em 2026-06-06.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class TestAppVersionConstant:
    def test_importable_and_semver_shaped(self):
        from app.core.version import APP_VERSION
        assert isinstance(APP_VERSION, str)
        assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION), APP_VERSION


class TestAppVersionWiring:
    def test_main_injects_app_version_into_jinja_globals(self):
        src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        assert "from app.core.version import APP_VERSION" in src
        assert 'env.globals["app_version"]' in src

    def test_jinja_global_resolves_to_constant(self):
        """O mesmo wiring (global app_version) renderiza 'v<APP_VERSION>'."""
        from jinja2 import Environment
        from app.core.version import APP_VERSION
        env = Environment()
        env.globals["app_version"] = APP_VERSION
        assert env.from_string("v{{ app_version }}").render() == f"v{APP_VERSION}"


class TestFooterMarkup:
    def _base_html(self) -> str:
        return (_ROOT / "app" / "templates" / "layouts" / "base.html").read_text(encoding="utf-8")

    def test_tech_stack_text_removed(self):
        assert "LangGraph · LangFuse · FSM §15" not in self._base_html()

    def test_footer_renders_version(self):
        assert "v{{ app_version }}" in self._base_html()
