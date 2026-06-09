"""F6 — MCP_PER_TOOL_ENABLED como platform_setting togglável pela UI.

`apply_settings_to_env` mapeia `mcp_per_tool_enabled` (DB) → env
`MCP_PER_TOOL_ENABLED`; `per_tool_enabled()` reflete sem restart. Default OFF
preservado (`SettingsSave.mcp_per_tool_enabled = False`). NÃO é selado — é flag
de comportamento (como `grounding_strict`), não credencial de modelo, então a
ausência no banco não força remoção do env.
"""
from __future__ import annotations

import os

import pytest

from app.core import config as _config
from app.mcp.runtime import per_tool_enabled


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
    assert _config._UI_TO_ENV_MAP.get("mcp_per_tool_enabled") == "MCP_PER_TOOL_ENABLED"
    assert "mcp_per_tool_enabled" in _config._NON_MODEL_UI_KEYS
    # flag de comportamento, NÃO credencial → não é selada
    assert "MCP_PER_TOOL_ENABLED" not in _config._SEALED_ENV_VARS


@pytest.mark.asyncio
async def test_db_true_turns_flag_on(env_sandbox, mock_store):
    os.environ.pop("MCP_PER_TOOL_ENABLED", None)
    mock_store["mcp_per_tool_enabled"] = "true"
    await _config.apply_settings_to_env()
    assert os.environ.get("MCP_PER_TOOL_ENABLED") == "true"
    assert per_tool_enabled() is True


@pytest.mark.asyncio
async def test_db_false_keeps_flag_off(env_sandbox, mock_store):
    os.environ["MCP_PER_TOOL_ENABLED"] = "true"  # resíduo a ser sobrescrito
    mock_store["mcp_per_tool_enabled"] = "False"  # toggle salvo OFF (str(False))
    await _config.apply_settings_to_env()
    assert per_tool_enabled() is False


@pytest.mark.asyncio
async def test_db_absent_does_not_touch_flag(env_sandbox, mock_store):
    # não-modelo: ausência no banco não força remoção (preserva boot)
    os.environ.pop("MCP_PER_TOOL_ENABLED", None)
    await _config.apply_settings_to_env()  # store vazio
    assert per_tool_enabled() is False  # segue OFF (default)


def test_settings_save_default_off():
    from app.routes.dashboard import SettingsSave
    assert SettingsSave().mcp_per_tool_enabled is False


def test_settings_template_has_per_tool_toggle():
    from pathlib import Path
    content = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
    assert 'x-model="config.mcp_per_tool_enabled"' in content     # o checkbox
    assert "mcp_per_tool_enabled: false" in content               # estado inicial OFF
    assert "mcp_per_tool_enabled" in content                       # coerção bool no load
    assert "per-tool" in content.lower()
