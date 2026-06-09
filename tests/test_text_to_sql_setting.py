"""Tier 2 — TEXT_TO_SQL_ENABLED como platform_setting togglável pela UI.

PR1 do Tier 2 (text-to-SQL governado): só a infraestrutura de gating, espelhando
o precedente `MCP_PER_TOOL_ENABLED` (F6). `apply_settings_to_env` mapeia
`text_to_sql_enabled` (DB) → env `TEXT_TO_SQL_ENABLED`; `text_to_sql_enabled()`
reflete sem restart. Default OFF preservado (`SettingsSave.text_to_sql_enabled =
False`). NÃO é selado — é flag de comportamento (como `grounding_strict` e
`mcp_per_tool_enabled`), não credencial de modelo, então a ausência no banco não
força remoção do env (preserva boot).
"""
from __future__ import annotations

import os

import pytest

from app.core import config as _config
from app.data_tables.runtime import text_to_sql_enabled


@pytest.fixture
def env_sandbox():
    """Snapshot/restore de os.environ (apply_settings_to_env muta direto)."""
    _config.get_settings.cache_clear()
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
        _config.get_settings.cache_clear()


@pytest.fixture
def mock_store(monkeypatch):
    data: dict[str, str] = {}

    async def fake_get_all():
        return dict(data)

    monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
    return data


def test_setting_mapped_and_not_sealed():
    assert _config._UI_TO_ENV_MAP.get("text_to_sql_enabled") == "TEXT_TO_SQL_ENABLED"
    assert "text_to_sql_enabled" in _config._NON_MODEL_UI_KEYS
    # flag de comportamento, NÃO credencial → não é selada
    assert "TEXT_TO_SQL_ENABLED" not in _config._SEALED_ENV_VARS


@pytest.mark.asyncio
async def test_db_true_turns_flag_on(env_sandbox, mock_store):
    os.environ.pop("TEXT_TO_SQL_ENABLED", None)
    mock_store["text_to_sql_enabled"] = "true"
    await _config.apply_settings_to_env()
    assert os.environ.get("TEXT_TO_SQL_ENABLED") == "true"
    assert text_to_sql_enabled() is True


@pytest.mark.asyncio
async def test_db_false_keeps_flag_off(env_sandbox, mock_store):
    os.environ["TEXT_TO_SQL_ENABLED"] = "true"  # resíduo a ser sobrescrito
    mock_store["text_to_sql_enabled"] = "False"  # toggle salvo OFF (str(False))
    await _config.apply_settings_to_env()
    assert text_to_sql_enabled() is False


@pytest.mark.asyncio
async def test_db_absent_does_not_touch_flag(env_sandbox, mock_store):
    # não-modelo: ausência no banco não força remoção (preserva boot)
    os.environ.pop("TEXT_TO_SQL_ENABLED", None)
    await _config.apply_settings_to_env()  # store vazio
    assert text_to_sql_enabled() is False  # segue OFF (default)


def test_runtime_helper_reads_env_each_call(env_sandbox):
    # Lê os.environ a cada chamada → toggle vale em runtime sem restart.
    os.environ.pop("TEXT_TO_SQL_ENABLED", None)
    assert text_to_sql_enabled() is False
    for truthy in ("1", "true", "True", "yes", "on", "ON"):
        os.environ["TEXT_TO_SQL_ENABLED"] = truthy
        assert text_to_sql_enabled() is True, truthy
    for falsy in ("", "0", "false", "no", "off", "nope"):
        os.environ["TEXT_TO_SQL_ENABLED"] = falsy
        assert text_to_sql_enabled() is False, falsy


def test_settings_save_default_off():
    from app.routes.dashboard import SettingsSave
    assert SettingsSave().text_to_sql_enabled is False


def test_settings_template_has_text_to_sql_toggle():
    from pathlib import Path
    content = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
    assert 'x-model="config.text_to_sql_enabled"' in content   # o checkbox
    assert "text_to_sql_enabled: false" in content             # estado inicial OFF
    assert "text_to_sql_enabled" in content                    # coerção bool no load
    assert "text-to-sql" in content.lower()
