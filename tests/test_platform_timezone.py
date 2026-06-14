"""Timezone da plataforma parametrizável (Configurações > Plataforma).

Padrão: America/Sao_Paulo (GMT-3 Brasília). A setting `timezone` vira
os.environ['TZ'] via apply_settings_to_env e é exposta à UI (window.PLATFORM_TZ),
que injeta o fuso em toda formatação de data (Date.prototype.toLocale*).
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "app" / "templates" / "layouts" / "base.html"
SETTINGS_HTML = ROOT / "app" / "templates" / "pages" / "settings.html"


def test_settingsave_default_is_brasilia():
    from app.routes.dashboard import SettingsSave
    assert SettingsSave().timezone == "America/Sao_Paulo"


def test_timezone_mapped_to_TZ_and_non_sealed():
    from app.core.config import _UI_TO_ENV_MAP, _NON_MODEL_UI_KEYS, _SEALED_ENV_VARS
    assert _UI_TO_ENV_MAP.get("timezone") == "TZ"
    # não-selada: pode cair no .env; e TZ NÃO pode ser removida como resíduo selado
    assert "timezone" in _NON_MODEL_UI_KEYS
    assert "TZ" not in _SEALED_ENV_VARS


@pytest.mark.asyncio
async def test_apply_settings_sets_tz_env(monkeypatch):
    """A setting timezone do banco vira os.environ['TZ']."""
    import os
    from app.core import config as cfg

    class _Store:
        async def get_all(self):
            return {"timezone": "America/Recife"}

    monkeypatch.setattr("app.core.database.settings_store", _Store())
    monkeypatch.delenv("TZ", raising=False)
    await cfg.apply_settings_to_env()
    assert os.environ.get("TZ") == "America/Recife"


def test_base_html_has_tz_shim():
    txt = BASE.read_text(encoding="utf-8")
    assert "window.PLATFORM_TZ" in txt
    assert "platform_tz()" in txt
    # patcheia Date (não Number) e injeta timeZone só quando ausente
    assert "Date.prototype[m]" in txt
    assert "options.timeZone == null" in txt
    assert "toLocaleDateString" in txt and "toLocaleTimeString" in txt


def test_settings_ui_has_timezone_field():
    txt = SETTINGS_HTML.read_text(encoding="utf-8")
    assert "config.timezone" in txt
    assert 'timezone: \'America/Sao_Paulo\'' in txt  # default no state
    assert '<option value="America/Sao_Paulo">' in txt
