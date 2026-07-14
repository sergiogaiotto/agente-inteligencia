"""Regressão pro bug 'GPT-OSS configurado mas runtime reclama de api_key'.

Cenário real (2026-05-27): user configurou GPT-OSS-120B em /settings
com URL=https://hub-gpus.claro.com.br/gpt120/v1, key=not-needed.
Teste de conectividade passou (OK 1146ms). Mas ao invocar o agente no
Workspace estourava:

    ⚠ API Key do provedor 'gpt-oss-120b' não configurada.
    Acesse Configurações → Plataforma e insira a API Key do GPT-OSS-120B.

Causa: pre-check em engine.py tinha if/elif só pra openai/azure/maritaca/
ollama. gpt-oss-* caía no `else: api_key = ""` e disparava o draft de erro.

Fix: _resolve_provider_config reconhece gpt-oss-20b/120b explicitamente,
aceita 'not-needed' como key, usa URL como source of truth.
"""
from __future__ import annotations


from app.agents.engine import _resolve_provider_config


class _FakeSettings:
    """Stub do Settings — só os attrs lidos pelo helper."""
    def __init__(self, **kw):
        self.azure_openai_api_key = kw.get("azure_openai_api_key", "")
        self.maritaca_api_key = kw.get("maritaca_api_key", "")
        self.ollama_api_key = kw.get("ollama_api_key", "")
        self.oss20b_api_key = kw.get("oss20b_api_key", "")
        self.oss20b_url = kw.get("oss20b_url", "")
        self.oss120b_api_key = kw.get("oss120b_api_key", "")
        self.oss120b_url = kw.get("oss120b_url", "")


class TestGptOssProviders:
    def test_gpt_oss_120b_url_set_and_not_needed_key_passes(self):
        """O caso EXATO reportado pelo user: URL setada, key='not-needed'."""
        s = _FakeSettings(
            oss120b_url="https://hub-gpus.claro.com.br/gpt120/v1",
            oss120b_api_key="not-needed",
        )
        api_key, missing = _resolve_provider_config("gpt-oss-120b", s)
        assert missing is None, f"Não deveria bloquear: {missing}"
        assert api_key == "not-needed"

    def test_gpt_oss_120b_without_url_blocks(self):
        """Sem URL, o agente não tem pra onde chamar — bloqueia com mensagem clara."""
        s = _FakeSettings(oss120b_url="", oss120b_api_key="not-needed")
        api_key, missing = _resolve_provider_config("gpt-oss-120b", s)
        assert missing is not None
        assert "URL" in missing
        assert "GPT-OSS-120B" in missing

    def test_gpt_oss_120b_empty_key_uses_not_needed_sentinel(self):
        """Quando key está vazia mas URL está OK, o helper devolve 'not-needed'
        como sentinel — mesmo comportamento do GPTOSSProvider em llm_providers.py."""
        s = _FakeSettings(oss120b_url="https://hub.internal/v1", oss120b_api_key="")
        api_key, missing = _resolve_provider_config("gpt-oss-120b", s)
        assert missing is None
        assert api_key == "not-needed"

    def test_gpt_oss_20b_with_real_key_passes(self):
        s = _FakeSettings(
            oss20b_url="https://hub.internal/gpt20/v1",
            oss20b_api_key="sk-real-key-xyz",
        )
        api_key, missing = _resolve_provider_config("gpt-oss-20b", s)
        assert missing is None
        assert api_key == "sk-real-key-xyz"

    def test_gpt_oss_20b_without_url_blocks(self):
        s = _FakeSettings(oss20b_url="", oss20b_api_key="not-needed")
        _, missing = _resolve_provider_config("gpt-oss-20b", s)
        assert missing is not None
        assert "GPT-OSS-20B" in missing


class TestLegacyProviders:
    """Não-regressão: providers antigos continuam validando do jeito certo."""

    def test_azure_with_real_key_passes(self):
        s = _FakeSettings(azure_openai_api_key="sk-azure-real")
        api_key, missing = _resolve_provider_config("azure", s)
        assert missing is None
        assert api_key == "sk-azure-real"

    def test_openai_alias_uses_azure_key(self):
        """'openai' é alias de 'azure' (Onda 7 Wave 4 cleanup)."""
        s = _FakeSettings(azure_openai_api_key="sk-azure-real")
        api_key, missing = _resolve_provider_config("openai", s)
        assert missing is None
        assert api_key == "sk-azure-real"

    def test_azure_with_placeholder_blocks(self):
        s = _FakeSettings(azure_openai_api_key="sk-your-placeholder-here")
        _, missing = _resolve_provider_config("azure", s)
        assert missing is not None
        assert "Azure" in missing

    def test_azure_empty_blocks(self):
        s = _FakeSettings(azure_openai_api_key="")
        _, missing = _resolve_provider_config("azure", s)
        assert missing is not None

    def test_maritaca_placeholder_blocks(self):
        s = _FakeSettings(maritaca_api_key="mrt-your-key")
        _, missing = _resolve_provider_config("maritaca", s)
        assert missing is not None
        assert "Maritaca" in missing

    def test_ollama_without_key_uses_sentinel(self):
        """Ollama nunca precisa de key real — sentinel 'ollama' é válido."""
        s = _FakeSettings(ollama_api_key="")
        api_key, missing = _resolve_provider_config("ollama", s)
        assert missing is None
        assert api_key == "ollama"


class TestUnknownProvider:
    def test_unknown_provider_blocks_with_explicit_reason(self):
        """Provider não mapeado bloqueia com mensagem identificando o problema —
        evita falha silenciosa em runtime se alguém setar provider novo sem
        atualizar este helper."""
        s = _FakeSettings()
        _, missing = _resolve_provider_config("anthropic-claude", s)
        assert missing is not None
        assert "desconhecido" in missing.lower()
        assert "anthropic-claude" in missing
