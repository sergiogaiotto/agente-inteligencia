"""Testes do wizard de importação de Tools MCP (/api/v1/tools/wizard).

Garante o comportamento pedido pelo usuário: "em Configurações → Plataforma
temos o modelo primário e deve ser usado". O wizard deve:

1. Usar o **Modelo Primário** (settings.primary_provider/primary_model) via
   get_provider quando configurado — precedência sobre qualquer chave legada.
2. Cair pras chaves legadas (openai_key/maritaca_key no settings_store) só
   quando NÃO há Modelo Primário.
3. Quando nada está configurado, devolver erro orientando a configurar o
   Modelo Primário (não mais a mensagem antiga "Configure uma API key...").

Testes puros (sem DB nem rede): a resolução de LLM é função pura e a chamada
ao provider/httpx é mockada.
"""

from __future__ import annotations

import pytest

from app.routes import dashboard


class _FakeSettings:
    """Stub do objeto retornado por get_settings()."""

    def __init__(self, primary_provider: str = "", primary_model: str = ""):
        self.primary_provider = primary_provider
        self.primary_model = primary_model


# ═════════════════════════════════════════════════════════════════
# _resolve_mcp_wizard_llm — função pura de resolução de precedência
# ═════════════════════════════════════════════════════════════════
class TestResolveMcpWizardLlm:
    def test_primary_configurado_tem_precedencia(self):
        desc = dashboard._resolve_mcp_wizard_llm(
            _FakeSettings("gpt-oss-120b", "openai/gpt-oss-120b"),
            {"openai_key": "sk-legacy", "maritaca_key": "mrt-legacy"},
        )
        assert desc == {
            "mode": "primary",
            "provider": "gpt-oss-120b",
            "model": "openai/gpt-oss-120b",
        }

    def test_primary_com_whitespace_eh_strippado(self):
        desc = dashboard._resolve_mcp_wizard_llm(
            _FakeSettings("  azure  ", "  gpt-4o  "), {}
        )
        assert desc["mode"] == "primary"
        assert desc["provider"] == "azure"
        assert desc["model"] == "gpt-4o"

    def test_so_provider_sem_model_nao_eh_primary(self):
        # primary incompleto + sem legacy → None
        desc = dashboard._resolve_mcp_wizard_llm(_FakeSettings("gpt-oss-120b", ""), {})
        assert desc is None

    def test_fallback_legacy_openai_quando_sem_primary(self):
        desc = dashboard._resolve_mcp_wizard_llm(
            _FakeSettings("", ""),
            {"openai_key": "sk-abc", "openai_model": "gpt-4.1"},
        )
        assert desc["mode"] == "legacy"
        assert desc["source"] == "openai"
        assert desc["api_key"] == "sk-abc"
        assert desc["model"] == "gpt-4.1"
        assert desc["base_url"] == "https://api.openai.com/v1"

    def test_fallback_legacy_maritaca_quando_sem_openai(self):
        desc = dashboard._resolve_mcp_wizard_llm(
            _FakeSettings("", ""),
            {"maritaca_key": "mrt-xyz", "maritaca_model": "sabia-4",
             "maritaca_url": "https://chat.maritaca.ai/api/"},
        )
        assert desc["mode"] == "legacy"
        assert desc["source"] == "maritaca"
        assert desc["api_key"] == "mrt-xyz"
        assert desc["model"] == "sabia-4"
        # rstrip da barra evita //v1
        assert desc["base_url"] == "https://chat.maritaca.ai/api/v1"

    def test_legacy_openai_tem_precedencia_sobre_maritaca(self):
        desc = dashboard._resolve_mcp_wizard_llm(
            _FakeSettings("", ""),
            {"openai_key": "sk-1", "maritaca_key": "mrt-1"},
        )
        assert desc["source"] == "openai"

    def test_nada_configurado_retorna_none(self):
        assert dashboard._resolve_mcp_wizard_llm(_FakeSettings("", ""), {}) is None

    def test_chaves_legacy_vazias_sao_ignoradas(self):
        desc = dashboard._resolve_mcp_wizard_llm(
            _FakeSettings("", ""), {"openai_key": "  ", "maritaca_key": ""}
        )
        assert desc is None


# ═════════════════════════════════════════════════════════════════
# _mcp_wizard_complete — completa via get_provider (modo primary)
# ═════════════════════════════════════════════════════════════════
class TestMcpWizardComplete:
    @pytest.mark.asyncio
    async def test_primary_usa_get_provider_com_args_corretos(self, monkeypatch):
        captured = {}

        class _FakeProvider:
            async def generate(self, messages, **kwargs):
                captured["messages"] = messages
                return {"content": '[{"name":"ok"}]', "model": "openai/gpt-oss-120b"}

        def _fake_get_provider(provider_name, **kwargs):
            captured["provider_name"] = provider_name
            captured["kwargs"] = kwargs
            return _FakeProvider()

        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)

        out = await dashboard._mcp_wizard_complete(
            {"mode": "primary", "provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b"},
            "PROMPT_DE_TESTE",
        )

        assert out == '[{"name":"ok"}]'
        assert captured["provider_name"] == "gpt-oss-120b"
        assert captured["kwargs"]["model"] == "openai/gpt-oss-120b"
        assert captured["kwargs"]["temperature"] == 0.3
        assert captured["messages"] == [{"role": "user", "content": "PROMPT_DE_TESTE"}]

    @pytest.mark.asyncio
    async def test_primary_content_none_vira_string_vazia(self, monkeypatch):
        class _FakeProvider:
            async def generate(self, messages, **kwargs):
                return {"content": None}

        monkeypatch.setattr(
            "app.core.llm_providers.get_provider", lambda *a, **k: _FakeProvider()
        )
        out = await dashboard._mcp_wizard_complete(
            {"mode": "primary", "provider": "azure", "model": "gpt-4o"}, "x"
        )
        assert out == ""


# ═════════════════════════════════════════════════════════════════
# Rota /tools/wizard — comportamento ponta-a-ponta (mocks)
# ═════════════════════════════════════════════════════════════════
def _patch_store(monkeypatch, store: dict):
    async def _fake_get_all():
        return store

    monkeypatch.setattr("app.routes.dashboard.settings_store.get_all", _fake_get_all)


class TestMcpWizardRoute:
    @pytest.mark.asyncio
    async def test_sem_llm_retorna_dica_do_modelo_primario(self, monkeypatch):
        _patch_store(monkeypatch, {})
        monkeypatch.setattr("app.core.config.get_settings", lambda: _FakeSettings("", ""))

        out = await dashboard.mcp_wizard(dashboard.MCPWizardQuery(query="github mcp"))

        assert out["results"] == []
        # mensagem nova: orienta o Modelo Primário (não a antiga "Configure uma API key")
        assert "Modelo Primário" in out["error"]
        assert "API key" not in out["error"].split("Plataforma")[0]

    @pytest.mark.asyncio
    async def test_primary_roteia_e_parseia_resultados(self, monkeypatch):
        _patch_store(monkeypatch, {})
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: _FakeSettings("gpt-oss-120b", "openai/gpt-oss-120b"),
        )

        captured = {}

        class _FakeProvider:
            async def generate(self, messages, **kwargs):
                return {
                    "content": (
                        "```json\n"
                        '[{"name":"GitHub MCP","description":"d","endpoint":"npx -y x",'
                        '"operations":["o"],"install_cmd":"npx -y x",'
                        '"source_url":"https://github.com/x","auth":"none",'
                        '"sensitivity":"internal"}]\n```'
                    )
                }

        def _fake_get_provider(provider_name, **kwargs):
            captured["provider_name"] = provider_name
            captured["model"] = kwargs.get("model")
            return _FakeProvider()

        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)

        out = await dashboard.mcp_wizard(
            dashboard.MCPWizardQuery(query="servidor mcp do github")
        )

        # roteou pelo Modelo Primário
        assert captured["provider_name"] == "gpt-oss-120b"
        assert captured["model"] == "openai/gpt-oss-120b"
        # parseou a resposta (mesmo com cercas ```json)
        assert "error" not in out
        assert len(out["results"]) == 1
        assert out["results"][0]["name"] == "GitHub MCP"

    @pytest.mark.asyncio
    async def test_legacy_openai_usado_quando_sem_primary(self, monkeypatch):
        _patch_store(monkeypatch, {"openai_key": "sk-abc", "openai_model": "gpt-4o"})
        monkeypatch.setattr("app.core.config.get_settings", lambda: _FakeSettings("", ""))

        seen = {}

        async def _fake_complete(llm_desc, prompt):
            seen["desc"] = llm_desc
            return "[]"

        monkeypatch.setattr("app.routes.dashboard._mcp_wizard_complete", _fake_complete)

        out = await dashboard.mcp_wizard(dashboard.MCPWizardQuery(query="qualquer coisa"))

        assert out["results"] == []
        assert "error" not in out
        assert seen["desc"]["mode"] == "legacy"
        assert seen["desc"]["source"] == "openai"
        assert seen["desc"]["api_key"] == "sk-abc"
