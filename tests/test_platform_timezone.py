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


# ── Helpers globais de formatação (fonte única; substituem o fatiar ISO) ──────
PAGES_DIR = ROOT / "app" / "templates" / "pages"

# Padrões que mostram UTC cru porque fatiam a string ISO em vez de passar pelos
# toLocale* patcheados (o bug que o usuário reportou: chat mostrava 20:26 em vez
# de 17:26 no GMT-3). NENHUM template de página pode voltar a usá-los.
_ISO_SLICE_ANTIPATTERNS = (
    "replace('T',' ')",
    "replace('T', ' ')",
    'replace("T"," ")',
    'replace("T", " ")',
    ".toISOString().substring",
)


def test_base_html_exposes_tz_helpers():
    """base.html define os helpers globais de fuso usados por todas as páginas."""
    txt = BASE.read_text(encoding="utf-8")
    for fn in ("tzParse", "tzDate", "tzTime", "tzTimeSec", "tzDateTime", "tzDateTimeSec"):
        assert f"window.{fn} =" in txt, f"helper window.{fn} ausente em base.html"
    # tzParse trata string naive (sem Z/offset) como UTC — datas no banco são UTC.
    assert "+= 'Z'" in txt or "+ 'Z'" in txt
    # Formato ISO-like preservado via locale sueco (AAAA-MM-DD HH:MM).
    assert "'sv-SE'" in txt


def test_no_page_slices_iso_timestamps():
    """Nenhuma página fatia a string ISO para exibir data/hora (bypassa o fuso).

    Regressão do fix de timezone: chat/sessões/listas mostravam UTC porque
    usavam created_at.replace('T',' ').substring(...) em vez de tzDateTime()/
    tzTime(). Garante que a varredura foi completa e não reincide.
    """
    offenders = []
    for path in PAGES_DIR.glob("*.html"):
        txt = path.read_text(encoding="utf-8")
        for pat in _ISO_SLICE_ANTIPATTERNS:
            if pat in txt:
                offenders.append(f"{path.name}: {pat}")
    assert not offenders, "ISO-slice antipattern reintroduzido: " + "; ".join(offenders)


def test_workspace_chat_uses_tz_helper():
    """O sintoma reportado (timestamp do chat) usa o helper de fuso."""
    txt = (PAGES_DIR / "workspace.html").read_text(encoding="utf-8")
    assert "tzTime(msg.created_at)" in txt
    assert "tzDateTime(s.created_at)" in txt
