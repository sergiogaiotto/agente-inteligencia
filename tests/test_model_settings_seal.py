"""Testes do SSOT de modelos (seal do .env) — pedido do operador 2026-06-06.

Princípio: TODOS os modelos da plataforma (provedores Azure / OpenAI público /
Maritaca / Ollama / GPT-OSS 120b/20b, embedding Qwen3/Azure, Modelo Primário e
Langfuse) usam EXCLUSIVAMENTE as chaves/acessos da tela de Configurações
(persistidos em platform_settings → os.environ via apply_settings_to_env). O
arquivo .env é IGNORADO para esses campos.

Fora do escopo (continuam lendo .env normalmente): infra, flags de segurança,
default_llm_provider, grounding_strict e default_response_language.

Cobre:
- _SEALED_ENV_VARS deriva de _UI_TO_ENV_MAP menos as chaves não-modelo;
- Settings.settings_customise_sources filtra as chaves seladas da fonte dotenv;
- apply_settings_to_env: banco não-vazio vence env plantado; banco vazio remove
  resíduo do .env de os.environ → cai no default da classe (NÃO no .env);
- não-modelo (grounding_strict, idioma) não é tocado quando banco vazio;
- banco indisponível → retorna 0 sem crashar e sem mexer em os.environ.
"""

from __future__ import annotations

import os

import pytest

from app.core import config as _config


# ═════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def env_sandbox():
    """Snapshot/restore de os.environ + invalida cache de get_settings.

    apply_settings_to_env muta os.environ DIRETAMENTE (set/del), fora do
    monkeypatch — por isso o snapshot manual garante isolamento entre testes.
    """
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
    """Patcheia settings_store.get_all com um dict controlado (sem Postgres)."""
    data: dict[str, str] = {}

    async def fake_get_all():
        return dict(data)

    monkeypatch.setattr("app.core.database.settings_store.get_all", fake_get_all)
    return data


# ═════════════════════════════════════════════════════════════════
# Parte 1 — Conjunto selado (_SEALED_ENV_VARS)
# ═════════════════════════════════════════════════════════════════


class TestSealedSet:
    def test_sealed_deriva_do_mapa_menos_nao_modelo(self):
        expected = {
            env
            for ui, env in _config._UI_TO_ENV_MAP.items()
            if ui not in _config._NON_MODEL_UI_KEYS
        }
        assert _config._SEALED_ENV_VARS == expected

    def test_non_model_keys_nao_estao_seladas(self):
        # grounding_strict e idioma continuam lendo .env — fora do escopo.
        assert "GROUNDING_STRICT" not in _config._SEALED_ENV_VARS
        assert "DEFAULT_RESPONSE_LANGUAGE" not in _config._SEALED_ENV_VARS
        assert "grounding_strict" in _config._NON_MODEL_UI_KEYS
        assert "default_response_language" in _config._NON_MODEL_UI_KEYS

    def test_provedores_embedding_primario_langfuse_estao_selados(self):
        for env in [
            # Azure
            "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_CHAT_DEPLOYMENT",
            "AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT",
            # OpenAI público
            "OPENAI_PUBLIC_API_KEY", "OPENAI_PUBLIC_BASE_URL", "OPENAI_PUBLIC_MODEL",
            # Maritaca
            "MARITACA_API_KEY", "MARITACA_API_URL", "MARITACA_MODEL",
            # Ollama
            "OLLAMA_API_URL", "OLLAMA_MODEL",
            # GPT-OSS
            "OSS120B_URL", "OSS120B_MODEL", "OSS120B_API_KEY",
            "OSS20B_URL", "OSS20B_MODEL", "OSS20B_API_KEY",
            "LLM_TIMEOUT_SECONDS",
            # Modelo Primário
            "PRIMARY_PROVIDER", "PRIMARY_MODEL",
            # Embedding
            "EMBEDDING_PROVIDER", "QWEN3_SOURCE", "QWEN3_PATH",
            "QWEN3_MODEL", "QWEN3_DIMENSIONS",
            # Langfuse
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
        ]:
            assert env in _config._SEALED_ENV_VARS, env


# ═════════════════════════════════════════════════════════════════
# Parte 2 — Filtro da fonte dotenv (settings_customise_sources)
# ═════════════════════════════════════════════════════════════════


class TestDotenvSourceFilter:
    def test_dotenv_source_remove_chaves_seladas(self):
        def fake_dotenv():
            return {
                "azure_openai_api_key": "FROM_DOTENV",
                "langfuse_secret_key": "FROM_DOTENV",
                "oss120b_url": "FROM_DOTENV",
                "primary_provider": "FROM_DOTENV",
                "grounding_strict": "false",
                "default_response_language": "en-US",
                "app_name": "FromDotenv",
                "database_url": "postgresql://x",
            }

        sources = _config.Settings.settings_customise_sources(
            _config.Settings,
            lambda: {},   # init
            lambda: {},   # env
            fake_dotenv,  # dotenv
            lambda: {},   # file_secret
        )
        assert len(sources) == 4
        sealed_dotenv = sources[2]
        out = sealed_dotenv()

        # Seladas (modelo) saem da fonte dotenv:
        assert "azure_openai_api_key" not in out
        assert "langfuse_secret_key" not in out
        assert "oss120b_url" not in out
        assert "primary_provider" not in out
        # Não-modelo e infra continuam vindo do .env:
        assert out["grounding_strict"] == "false"
        assert out["default_response_language"] == "en-US"
        assert out["app_name"] == "FromDotenv"
        assert out["database_url"] == "postgresql://x"

    def test_ordem_das_fontes_preservada(self):
        init = lambda: {"a": 1}
        env = lambda: {"b": 2}
        dot = lambda: {}
        sec = lambda: {"c": 3}
        sources = _config.Settings.settings_customise_sources(
            _config.Settings, init, env, dot, sec
        )
        # init > env > dotenv(filtrado) > secrets — posições inalteradas.
        assert sources[0] is init
        assert sources[1] is env
        assert sources[3] is sec


# ═════════════════════════════════════════════════════════════════
# Parte 3 — apply_settings_to_env (set/del autoritativo)
# ═════════════════════════════════════════════════════════════════


class TestApplySettingsSeal:
    @pytest.mark.asyncio
    async def test_banco_vence_env_plantado(self, env_sandbox, mock_store):
        os.environ["AZURE_OPENAI_API_KEY"] = "ENV_LEAK"
        mock_store["azure_key"] = "DB_WINS"

        applied = await _config.apply_settings_to_env()

        assert applied >= 1
        assert os.environ["AZURE_OPENAI_API_KEY"] == "DB_WINS"
        assert _config.get_settings().azure_openai_api_key == "DB_WINS"

    @pytest.mark.asyncio
    async def test_banco_vazio_cai_no_default_nao_no_env(self, env_sandbox, mock_store):
        # Resíduo do .env injetado no boot (docker env_file):
        os.environ["MARITACA_API_KEY"] = "ENV_LEAK_SHOULD_VANISH"
        # banco vazio (mock_store == {})

        await _config.apply_settings_to_env()

        assert "MARITACA_API_KEY" not in os.environ
        # default da classe ("" para a chave Maritaca), NUNCA o valor do .env:
        assert _config.get_settings().maritaca_api_key == ""

    @pytest.mark.asyncio
    async def test_valor_vazio_no_banco_tambem_remove_residuo(self, env_sandbox, mock_store):
        os.environ["OSS120B_URL"] = "https://env.leak.example"
        mock_store["oss120b_url"] = ""  # explicitamente vazio no banco

        await _config.apply_settings_to_env()

        assert "OSS120B_URL" not in os.environ
        assert _config.get_settings().oss120b_url == ""

    @pytest.mark.asyncio
    async def test_valor_do_banco_e_stripado(self, env_sandbox, mock_store):
        mock_store["primary_provider"] = "  gpt-oss-120b  "

        await _config.apply_settings_to_env()

        assert os.environ["PRIMARY_PROVIDER"] == "gpt-oss-120b"
        assert _config.get_settings().primary_provider == "gpt-oss-120b"

    @pytest.mark.asyncio
    async def test_langfuse_selado_banco_vence_env(self, env_sandbox, mock_store):
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk_env_leak"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk_env_leak"
        mock_store["langfuse_public"] = "pk_db"
        mock_store["langfuse_secret"] = "sk_db"
        mock_store["langfuse_host"] = "https://db.langfuse.example"

        await _config.apply_settings_to_env()

        s = _config.get_settings()
        assert s.langfuse_public_key == "pk_db"
        assert s.langfuse_secret_key == "sk_db"
        assert s.langfuse_host == "https://db.langfuse.example"

    @pytest.mark.asyncio
    async def test_langfuse_selado_banco_vazio_cai_no_default(self, env_sandbox, mock_store):
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk_env_leak"
        os.environ["LANGFUSE_HOST"] = "https://env.leak.example"
        # banco vazio

        await _config.apply_settings_to_env()

        assert "LANGFUSE_PUBLIC_KEY" not in os.environ
        assert "LANGFUSE_HOST" not in os.environ
        s = _config.get_settings()
        assert s.langfuse_public_key == ""
        assert s.langfuse_host == "https://cloud.langfuse.com"  # default da classe

    @pytest.mark.asyncio
    async def test_nao_modelo_nao_e_tocado_quando_banco_vazio(self, env_sandbox, mock_store):
        # grounding_strict e idioma NÃO são selados → seguem do .env/env.
        os.environ["GROUNDING_STRICT"] = "false"
        os.environ["DEFAULT_RESPONSE_LANGUAGE"] = "en-US"
        # banco vazio

        await _config.apply_settings_to_env()

        assert os.environ.get("GROUNDING_STRICT") == "false"
        assert os.environ.get("DEFAULT_RESPONSE_LANGUAGE") == "en-US"
        s = _config.get_settings()
        assert s.grounding_strict is False
        assert s.default_response_language == "en-US"

    @pytest.mark.asyncio
    async def test_store_indisponivel_retorna_0_sem_mexer_no_env(self, env_sandbox, monkeypatch):
        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("app.core.database.settings_store.get_all", boom)
        os.environ["AZURE_OPENAI_API_KEY"] = "ENV_FALLBACK_DURANTE_BOOT"

        applied = await _config.apply_settings_to_env()

        assert applied == 0
        # Sem banco não dá pra selar — preserva o que já estava (fallback de boot).
        assert os.environ["AZURE_OPENAI_API_KEY"] == "ENV_FALLBACK_DURANTE_BOOT"
