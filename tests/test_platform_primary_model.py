"""Testes do Modelo Primário da plataforma (fallback global).

Cobre:
- Settings novos: primary_provider, primary_model (default vazios)
- _UI_TO_ENV_MAP traz as 2 chaves novas
- SettingsSave aceita os 2 campos via PUT /settings
- Fallback no engine: agent SEM task_type e SEM llm_provider/model →
  completa com primary_provider/primary_model da plataforma
- Respeita snapshot do agent: se já tem llm_provider/model próprios,
  primário NÃO sobrescreve
- Primário não definido (vazios) → comportamento legacy intacto
"""

from __future__ import annotations

import pytest

from app.core import config as _config


@pytest.fixture
def fresh_settings():
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


class TestSettingsFields:
    def test_settings_tem_primary_fields_default_vazios(self, fresh_settings):
        s = _config.get_settings()
        assert hasattr(s, "primary_provider")
        assert hasattr(s, "primary_model")
        assert s.primary_provider == ""
        assert s.primary_model == ""

    def test_ui_to_env_map_tem_as_2_chaves(self):
        assert "primary_provider" in _config._UI_TO_ENV_MAP
        assert "primary_model" in _config._UI_TO_ENV_MAP
        assert _config._UI_TO_ENV_MAP["primary_provider"] == "PRIMARY_PROVIDER"
        assert _config._UI_TO_ENV_MAP["primary_model"] == "PRIMARY_MODEL"

    def test_env_var_popula_settings(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("PRIMARY_PROVIDER", "gpt-oss-120b")
        monkeypatch.setenv("PRIMARY_MODEL", "openai/gpt-oss-120b")
        s = _config.get_settings()
        assert s.primary_provider == "gpt-oss-120b"
        assert s.primary_model == "openai/gpt-oss-120b"


class TestSettingsSaveSchema:
    def test_settings_save_aceita_primary_fields(self):
        from app.routes.dashboard import SettingsSave
        s = SettingsSave(
            primary_provider="gpt-oss-120b",
            primary_model="openai/gpt-oss-120b",
        )
        assert s.primary_provider == "gpt-oss-120b"
        assert s.primary_model == "openai/gpt-oss-120b"

    def test_settings_save_default_vazios(self):
        from app.routes.dashboard import SettingsSave
        s = SettingsSave()
        assert s.primary_provider == ""
        assert s.primary_model == ""


# ═════════════════════════════════════════════════════════════════
# Fallback no engine — testes da lógica de aplicação do primário
# ═════════════════════════════════════════════════════════════════
#
# Em vez de chamar execute_interaction (pesado, envolve LLM real),
# testamos diretamente a lógica isolada de fallback como uma função
# pura. Replica o snippet do engine para garantir invariantes.


def _apply_primary_fallback(agent: dict, primary_provider: str, primary_model: str) -> dict:
    """Replica a lógica de fallback do engine.execute_interaction.

    Mantida espelhada — testes garantem invariantes. Atualizar
    junto se o snippet do engine mudar.
    """
    p = (primary_provider or "").strip()
    m = (primary_model or "").strip()
    if p and m and (not agent.get("llm_provider") or not agent.get("model")):
        agent = dict(agent)
        if not agent.get("llm_provider"):
            agent["llm_provider"] = p
        if not agent.get("model"):
            agent["model"] = m
    return agent


class TestFallbackLogic:
    def test_agent_sem_provider_nem_model_recebe_primary(self):
        agent = {"id": "a1", "name": "Test"}
        out = _apply_primary_fallback(agent, "gpt-oss-120b", "openai/gpt-oss-120b")
        assert out["llm_provider"] == "gpt-oss-120b"
        assert out["model"] == "openai/gpt-oss-120b"

    def test_agent_com_provider_proprio_nao_sobrescreve(self):
        agent = {"id": "a1", "llm_provider": "maritaca", "model": "sabia-4"}
        out = _apply_primary_fallback(agent, "gpt-oss-120b", "openai/gpt-oss-120b")
        # Snapshot do agent tem prioridade
        assert out["llm_provider"] == "maritaca"
        assert out["model"] == "sabia-4"

    def test_agent_com_apenas_provider_mantem_e_completa_model(self):
        agent = {"id": "a1", "llm_provider": "ollama"}
        out = _apply_primary_fallback(agent, "gpt-oss-120b", "openai/gpt-oss-120b")
        assert out["llm_provider"] == "ollama"
        assert out["model"] == "openai/gpt-oss-120b"

    def test_agent_com_apenas_model_mantem_e_completa_provider(self):
        agent = {"id": "a1", "model": "sabia-4"}
        out = _apply_primary_fallback(agent, "gpt-oss-120b", "openai/gpt-oss-120b")
        assert out["llm_provider"] == "gpt-oss-120b"
        assert out["model"] == "sabia-4"

    def test_primary_vazio_nao_sobrescreve(self):
        agent = {"id": "a1", "name": "legacy"}
        out = _apply_primary_fallback(agent, "", "")
        assert "llm_provider" not in out
        assert "model" not in out

    def test_primary_so_provider_sem_model_nao_aplica(self):
        agent = {"id": "a1"}
        out = _apply_primary_fallback(agent, "gpt-oss-120b", "")
        assert "llm_provider" not in out
        assert "model" not in out

    def test_primary_com_whitespace_strip(self):
        agent = {"id": "a1"}
        out = _apply_primary_fallback(agent, "  gpt-oss-120b  ", "  openai/gpt-oss-120b  ")
        assert out["llm_provider"] == "gpt-oss-120b"
        assert out["model"] == "openai/gpt-oss-120b"

    def test_dict_original_nao_mutado(self):
        agent = {"id": "a1"}
        original_keys = set(agent.keys())
        _apply_primary_fallback(agent, "gpt-oss-120b", "openai/gpt-oss-120b")
        assert set(agent.keys()) == original_keys


class TestPrecedenciaCompleta:
    """Ordem de precedência: task_type > snapshot do agent > primary > hardcoded."""

    def test_snapshot_tem_precedencia_sobre_primary(self):
        agent = {"llm_provider": "azure", "model": "gpt-4o-mini"}
        out = _apply_primary_fallback(agent, "maritaca", "sabia-4")
        assert out["llm_provider"] == "azure"
        assert out["model"] == "gpt-4o-mini"

    def test_primary_tem_precedencia_sobre_hardcoded(self):
        # Sem snapshot → primary deve aplicar (e não cair em azure/gpt-4o)
        agent = {}
        out = _apply_primary_fallback(agent, "ollama", "llama3.1")
        assert out["llm_provider"] == "ollama"
        assert out["model"] == "llama3.1"
