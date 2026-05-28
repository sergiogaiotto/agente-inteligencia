"""UI /settings agora tem dropdown pro default global de idioma.

Antes deste PR, operador precisava mexer via env DEFAULT_RESPONSE_LANGUAGE
ou SQL direto na tabela platform_settings. Após este PR: dropdown no card
"Idioma de Resposta — Default Global" no topo da aba Plataforma.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.routes.dashboard import SettingsSave


# ─── Schema backend ────────────────────────────────────────────────


class TestSettingsSaveSchema:
    def test_default_value_is_pt_br(self):
        """SettingsSave sem campo informado → default pt-BR."""
        s = SettingsSave()
        assert s.default_response_language == "pt-BR"

    def test_accepts_valid_bcp47_tag(self):
        s = SettingsSave(default_response_language="en-US")
        assert s.default_response_language == "en-US"

    def test_rejects_arbitrary_value(self):
        """Pattern BCP-47 bloqueia valores arbitrários — pessoa não pode
        salvar 'klingon' ou 'PT_br' via API direta."""
        with pytest.raises(Exception):
            SettingsSave(default_response_language="klingon")
        with pytest.raises(Exception):
            SettingsSave(default_response_language="PT_br")

    def test_accepts_language_only_without_region(self):
        """BCP-47 permite só código de língua (pt, en, es, etc) sem região."""
        s = SettingsSave(default_response_language="pt")
        assert s.default_response_language == "pt"


# ─── Smoke do HTML da settings.html ────────────────────────────────


@pytest.fixture(scope="module")
def settings_html() -> str:
    path = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html"
    return path.read_text(encoding="utf-8")


class TestSettingsUiHtml:
    def test_default_response_language_card_present(self, html=None):
        """Card 'Idioma de Resposta — Default Global' renderizado."""
        from pathlib import Path as _P
        s = (_P(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html").read_text(encoding="utf-8")
        assert "Idioma de Resposta" in s
        assert "Default Global" in s

    def test_select_bound_to_config(self):
        from pathlib import Path as _P
        s = (_P(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html").read_text(encoding="utf-8")
        assert 'x-model="config.default_response_language"' in s

    def test_alpine_config_has_default_pt_br(self):
        """Estado Alpine inicializa com pt-BR pra UX consistente — quando
        backend devolve vazio, dropdown ainda mostra pt-BR selecionado."""
        from pathlib import Path as _P
        s = (_P(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html").read_text(encoding="utf-8")
        assert "default_response_language: 'pt-BR'" in s

    def test_includes_options_for_main_languages(self):
        from pathlib import Path as _P
        s = (_P(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html").read_text(encoding="utf-8")
        # Dropdown precisa cobrir pelo menos pt-BR/en-US/es-ES/fr-FR pra ser útil
        for tag in ("pt-BR", "en-US", "es-ES", "fr-FR", "de-DE", "ja-JP"):
            assert tag in s, f"Tag BCP-47 ausente no dropdown: {tag}"

    def test_card_points_to_agents_for_override(self):
        """Card explica que override por agente fica em /agents — UX coerente."""
        from pathlib import Path as _P
        s = (_P(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html").read_text(encoding="utf-8")
        assert 'href="/agents"' in s and "response_language" in s
