"""Testes do provider OpenAI público (api.openai.com) — PR #194.

User pediu (2026-05-29): incluir OpenAI separado de Azure no dropdown de
Roteamento LLM. Provider OpenAIPublicProvider é distinto do alias
"openai" (que continua apontando pra Azure por retrocompat).

Cobertura:
- Factory: get_provider("openai_public") retorna OpenAIPublicProvider
- Pre-check do engine (_resolve_provider_config): chave própria, mensagem
  de erro específica quando ausente
- Endpoint /wizard/models: bloco openai_public presente
- Endpoint /settings/test-provider: aceita provider=openai_public
- SettingsSave: campos openai_public_api_key/base_url/model
- _UI_TO_ENV_MAP: mapeia pros env vars OPENAI_PUBLIC_*
"""
from __future__ import annotations

import pytest


# ───────────────────────────────────────────────────────────────
# Factory
# ───────────────────────────────────────────────────────────────


class TestProviderFactory:
    def test_get_provider_openai_public_returns_dedicated_class(self):
        """openai_public retorna OpenAIPublicProvider — não Azure (que é
        o que 'openai' devolve por retrocompat)."""
        from app.core.llm_providers import (
            get_provider, OpenAIPublicProvider, AzureOpenAIProvider,
        )
        p = get_provider("openai_public", model="gpt-4o")
        assert isinstance(p, OpenAIPublicProvider)
        assert not isinstance(p, AzureOpenAIProvider)

    def test_openai_alias_still_returns_azure(self):
        """Retrocompat: 'openai' continua sendo alias de Azure (agentes
        legacy não quebram)."""
        from app.core.llm_providers import get_provider, AzureOpenAIProvider
        p = get_provider("openai", model="gpt-4o")
        assert isinstance(p, AzureOpenAIProvider)

    def test_provider_uses_api_openai_base_url_by_default(self, monkeypatch):
        """Sem env custom, OpenAIPublicProvider aponta pra api.openai.com/v1."""
        monkeypatch.setenv("OPENAI_PUBLIC_API_KEY", "sk-test-fake")
        # Limpa cache do Settings
        from app.core import config as _config
        _config.get_settings.cache_clear()
        try:
            from app.core.llm_providers import OpenAIPublicProvider
            p = OpenAIPublicProvider(model="gpt-4o")
            assert p.base_url == "https://api.openai.com/v1"
            assert p.model == "gpt-4o"
        finally:
            _config.get_settings.cache_clear()

    def test_provider_supports_structured_output(self):
        from app.core.llm_providers import OpenAIPublicProvider
        assert OpenAIPublicProvider.supports_structured_output is True


# ───────────────────────────────────────────────────────────────
# Pre-check do engine (_resolve_provider_config)
# ───────────────────────────────────────────────────────────────


class TestProviderPreCheck:
    def test_openai_public_uses_dedicated_key(self, monkeypatch):
        """Pre-check do engine pra openai_public lê openai_public_api_key —
        NÃO azure_openai_api_key. Operador pode ter Azure configurado mas
        OpenAI público vazio (ou vice-versa)."""
        monkeypatch.setenv("OPENAI_PUBLIC_API_KEY", "sk-public-real")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-fake")
        from app.core import config as _config
        _config.get_settings.cache_clear()
        try:
            from app.agents.engine import _resolve_provider_config
            settings = _config.get_settings()
            api_key, missing = _resolve_provider_config("openai_public", settings)
            assert missing is None
            assert api_key == "sk-public-real"  # NÃO a key do Azure
        finally:
            _config.get_settings.cache_clear()

    def test_openai_public_missing_key_returns_specific_error(self, monkeypatch):
        """Mensagem de erro precisa citar 'OpenAI público' — não Azure
        (operador olhando log entende o que faltou)."""
        monkeypatch.setenv("OPENAI_PUBLIC_API_KEY", "")
        from app.core import config as _config
        _config.get_settings.cache_clear()
        try:
            from app.agents.engine import _resolve_provider_config
            settings = _config.get_settings()
            api_key, missing = _resolve_provider_config("openai_public", settings)
            assert api_key == ""
            assert missing is not None
            assert "OpenAI público" in missing or "openai_public" in missing.lower()
        finally:
            _config.get_settings.cache_clear()

    def test_openai_alias_does_not_use_public_key(self, monkeypatch):
        """openai (alias) lê azure_openai_api_key, ignora openai_public_api_key.
        Path antigo intacto."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-real")
        monkeypatch.setenv("OPENAI_PUBLIC_API_KEY", "sk-public")
        from app.core import config as _config
        _config.get_settings.cache_clear()
        try:
            from app.agents.engine import _resolve_provider_config
            settings = _config.get_settings()
            api_key, missing = _resolve_provider_config("openai", settings)
            assert missing is None
            assert api_key == "azure-real"  # NÃO sk-public
        finally:
            _config.get_settings.cache_clear()


# ───────────────────────────────────────────────────────────────
# Endpoint /wizard/models
# ───────────────────────────────────────────────────────────────


class TestWizardModelsEndpoint:
    @pytest.mark.asyncio
    async def test_models_endpoint_exposes_openai_public_block(self):
        from app.routes.wizard import list_available_models
        result = await list_available_models()
        assert "openai_public" in result, (
            "endpoint /wizard/models não expõe openai_public — "
            "frontend dropdown não terá opção pra escolher"
        )
        # Tem os mesmos modelos canônicos (gpt-4o etc.)
        ids = [m["id"] for m in result["openai_public"]]
        assert "gpt-4o" in ids


# ───────────────────────────────────────────────────────────────
# Endpoint /settings/test-provider
# ───────────────────────────────────────────────────────────────


class TestSettingsTestProviderEndpoint:
    def test_endpoint_accepts_openai_public(self):
        """Endpoint /settings/test-provider precisa estar na whitelist
        valid_providers. Sem isso, UI tenta testar mas backend rejeita."""
        # Vamos validar que o set valid_providers tem a chave — sem chamar
        # o endpoint de verdade (requer FastAPI app setup).
        from app.routes.dashboard import test_provider  # noqa: F401
        import inspect
        src = inspect.getsource(test_provider)
        assert "openai_public" in src, (
            "valid_providers em test_provider não inclui openai_public — "
            "UI vai receber 400 ao testar conectividade"
        )


# ───────────────────────────────────────────────────────────────
# Persistência (SettingsSave + UI_TO_ENV_MAP)
# ───────────────────────────────────────────────────────────────


class TestSettingsPersistence:
    def test_settings_save_has_openai_public_fields(self):
        """SettingsSave precisa ter os 3 campos pra PUT /settings aceitar
        e settings_store persistir."""
        from app.routes.dashboard import SettingsSave
        fields = set(SettingsSave.model_fields.keys())
        for f in ("openai_public_api_key", "openai_public_base_url", "openai_public_model"):
            assert f in fields, f"SettingsSave sem campo {f!r}"

    def test_ui_to_env_map_has_openai_public(self):
        """_UI_TO_ENV_MAP mapeia settings_store key → env var. Sem isso,
        a key salva no DB não vira disponível pro provider em runtime."""
        from app.core.config import _UI_TO_ENV_MAP
        assert "openai_public_api_key" in _UI_TO_ENV_MAP
        assert _UI_TO_ENV_MAP["openai_public_api_key"] == "OPENAI_PUBLIC_API_KEY"
        assert _UI_TO_ENV_MAP["openai_public_base_url"] == "OPENAI_PUBLIC_BASE_URL"
        assert _UI_TO_ENV_MAP["openai_public_model"] == "OPENAI_PUBLIC_MODEL"


# ───────────────────────────────────────────────────────────────
# Frontend dropdown (settings.html)
# ───────────────────────────────────────────────────────────────


class TestRoutingDropdown:
    def test_settings_html_includes_openai_public_in_providers_list(self):
        """O array providers de routingOptions precisa incluir openai_public
        pra opção aparecer no dropdown de Roteamento LLM."""
        from pathlib import Path
        html = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "'openai_public'" in html
        # Label distinto do "Azure"
        assert "OpenAI (público)" in html

    def test_settings_html_renames_openai_alias_to_azure(self):
        """Visual: o alias 'openai' (que é Azure) agora tem label 'Azure'
        — não mais 'OpenAI/Azure' confuso."""
        from pathlib import Path
        html = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        # Antigo "OpenAI/Azure" removido (no contexto do dropdown)
        # Confirma que tem label "Azure" como provider label
        assert "openai: 'Azure'" in html
