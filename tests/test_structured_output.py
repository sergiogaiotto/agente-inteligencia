"""Testes do structured output (response_format) — Wave atual.

Cobre:
- _extract_json_schema_from_contract: extração de JSON Schema do bloco fenced.
- LLMProvider.supports_structured_output flag por provider.
- DeepAgentHarness._build_response_format: monta payload OpenAI correto.
- DeepAgentHarness._apply_response_format: tolerante a falha de bind.
- Provider Maritaca/Ollama/GPTOSS: propaga response_format ao request HTTP.
- Provider Ollama: traduz response_format json_schema → json_object.

Mocks: httpx via monkeypatch + LangChain via mock simples.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.engine import (
    DeepAgentHarness,
    _extract_json_schema_from_contract,
)
from app.core import config as _config
from app.core import llm_providers


@pytest.fixture
def fresh_settings(monkeypatch):
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


# ═════════════════════════════════════════════════════════════════
# _extract_json_schema_from_contract
# ═════════════════════════════════════════════════════════════════


class TestExtractJsonSchemaFromContract:
    def test_extracts_from_fenced_json_block(self):
        contract = """
Schema esperado:

```json
{
  "type": "object",
  "title": "MyOutput",
  "required": ["foo"],
  "properties": {
    "foo": {"type": "string"}
  }
}
```

Validar via jsonschema.
"""
        schema = _extract_json_schema_from_contract(contract)
        assert schema is not None
        assert schema["type"] == "object"
        assert schema["title"] == "MyOutput"
        assert schema["properties"]["foo"]["type"] == "string"

    def test_extracts_from_fenced_without_language_hint(self):
        """Bloco ``` sem hint 'json' ainda parseia se conteúdo for JSON válido."""
        contract = """```
{"type": "object", "properties": {"x": {"type": "number"}}}
```"""
        schema = _extract_json_schema_from_contract(contract)
        assert schema is not None
        assert schema["properties"]["x"]["type"] == "number"

    def test_extracts_from_raw_json_no_fence(self):
        contract = '{"type": "object", "properties": {"x": {"type": "integer"}}}'
        schema = _extract_json_schema_from_contract(contract)
        assert schema is not None
        assert schema["type"] == "object"

    def test_returns_none_for_invalid_json(self):
        contract = """```json
{ this is not valid JSON: !!!
```"""
        assert _extract_json_schema_from_contract(contract) is None

    def test_returns_none_for_empty_contract(self):
        assert _extract_json_schema_from_contract("") is None
        assert _extract_json_schema_from_contract(None) is None
        assert _extract_json_schema_from_contract("   ") is None

    def test_returns_none_for_non_schema_json(self):
        """JSON válido mas que não parece JSON Schema (sem type/properties/$ref)
        → None pra não enviar lixo pro LLM."""
        contract = '```json\n{"hello": "world"}\n```'
        assert _extract_json_schema_from_contract(contract) is None

    def test_extracts_first_json_block_when_multiple(self):
        """Se contract tem 2 blocos, pega o primeiro (mais comum: schema + exemplo)."""
        contract = """```json
{"type": "object", "properties": {"a": {"type": "string"}}}
```

Exemplo:
```json
{"a": "valor"}
```"""
        schema = _extract_json_schema_from_contract(contract)
        assert schema["properties"]["a"]["type"] == "string"


# ═════════════════════════════════════════════════════════════════
# LLMProvider.supports_structured_output flags
# ═════════════════════════════════════════════════════════════════


class TestProviderStructuredOutputFlags:
    def test_azure_supports(self):
        assert llm_providers.AzureOpenAIProvider.supports_structured_output is True

    def test_maritaca_supports(self):
        assert llm_providers.MaritacaProvider.supports_structured_output is True

    def test_ollama_supports(self):
        assert llm_providers.OllamaProvider.supports_structured_output is True

    def test_gpt_oss_supports(self):
        assert llm_providers.GPTOSSProvider.supports_structured_output is True

    def test_base_class_does_not_support_by_default(self):
        """Default da interface é False (conservador) — força provider a
        declarar explicitamente que suporta."""
        assert llm_providers.LLMProvider.supports_structured_output is False


# ═════════════════════════════════════════════════════════════════
# Harness._build_response_format
# ═════════════════════════════════════════════════════════════════


def _make_harness_with(skill_dict, openai_tools=None, provider_supports=True):
    """Constrói um DeepAgentHarness mockado pra testar _build_response_format
    sem precisar de provider real / Postgres / etc."""
    h = DeepAgentHarness.__new__(DeepAgentHarness)  # bypass __init__
    h.config = {"_parsed_skill": skill_dict}
    h.openai_tools = openai_tools or []
    mock_provider = MagicMock()
    mock_provider.supports_structured_output = provider_supports
    h.provider = mock_provider
    return h


class TestBuildResponseFormat:
    def test_returns_none_when_provider_unsupported(self):
        h = _make_harness_with(
            {"output_contract": '```json\n{"type":"object","properties":{"x":{"type":"string"}}}\n```'},
            provider_supports=False,
        )
        assert h._build_response_format() is None

    def test_returns_none_when_mcp_tools_present(self):
        """Tools + json_schema podem brigar — privilegia tools."""
        h = _make_harness_with(
            {"output_contract": '```json\n{"type":"object","properties":{"x":{"type":"string"}}}\n```'},
            openai_tools=[{"function": {"name": "search"}}],
        )
        assert h._build_response_format() is None

    def test_returns_none_when_no_output_contract(self):
        h = _make_harness_with({"output_contract": ""})
        assert h._build_response_format() is None

    def test_returns_none_when_contract_has_no_json(self):
        h = _make_harness_with({"output_contract": "Texto livre, sem schema."})
        assert h._build_response_format() is None

    def test_builds_correct_openai_payload(self):
        contract = """```json
{
  "type": "object",
  "title": "KnowledgeRetrievalOutput",
  "required": ["query"],
  "properties": {
    "query": {"type": "string"},
    "results": {"type": "array"}
  }
}
```"""
        h = _make_harness_with({"output_contract": contract})
        rf = h._build_response_format()
        assert rf is not None
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "KnowledgeRetrievalOutput"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["schema"]["properties"]["query"]["type"] == "string"

    def test_truncates_name_when_title_too_long(self):
        long_title = "x" * 200
        contract = f'```json\n{{"type":"object","title":"{long_title}","properties":{{"a":{{"type":"string"}}}}}}\n```'
        h = _make_harness_with({"output_contract": contract})
        rf = h._build_response_format()
        assert len(rf["json_schema"]["name"]) <= 64

    def test_uses_fallback_name_when_no_title(self):
        contract = '```json\n{"type":"object","properties":{"a":{"type":"string"}}}\n```'
        h = _make_harness_with({"output_contract": contract})
        rf = h._build_response_format()
        assert rf["json_schema"]["name"] == "SkillOutput"


# ═════════════════════════════════════════════════════════════════
# Harness._apply_response_format
# ═════════════════════════════════════════════════════════════════


class TestApplyResponseFormat:
    def test_returns_llm_unchanged_when_no_response_format(self):
        h = _make_harness_with({"output_contract": ""})
        # _response_format normalmente é setado em __init__; bypass aqui.
        h._response_format = None
        llm = MagicMock()
        result = h._apply_response_format(llm)
        assert result is llm
        llm.bind.assert_not_called()

    def test_binds_response_format_when_present(self):
        h = _make_harness_with({"output_contract": ""})
        h._response_format = {"type": "json_schema", "json_schema": {"name": "X", "schema": {}, "strict": True}}
        llm = MagicMock()
        bound = MagicMock()
        llm.bind.return_value = bound
        result = h._apply_response_format(llm)
        assert result is bound
        llm.bind.assert_called_once_with(response_format=h._response_format)

    def test_fallback_when_bind_raises(self):
        """Versão antiga de langchain pode não suportar bind(response_format=...).
        Não pode quebrar — retorna llm original e segue (caller cai em
        fallback prompt-only)."""
        h = _make_harness_with({"output_contract": ""})
        h._response_format = {"type": "json_schema", "json_schema": {"name": "X", "schema": {}, "strict": True}}
        llm = MagicMock()
        llm.bind.side_effect = TypeError("unexpected keyword 'response_format'")
        result = h._apply_response_format(llm)
        assert result is llm  # original, sem bind


# ═════════════════════════════════════════════════════════════════
# Providers propagam response_format ao request HTTP
# ═════════════════════════════════════════════════════════════════


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._body = json_body
        self.status_code = status_code
        self.text = json.dumps(json_body)

    def json(self):
        return self._body


class TestProvidersPropagateResponseFormat:
    @pytest.mark.asyncio
    async def test_maritaca_passes_response_format_to_body(self, monkeypatch, fresh_settings):
        """Maritaca recebe response_format direto via **kwargs → vai pro body
        do POST sem modificação."""
        monkeypatch.setenv("MARITACA_API_KEY", "sk-test")
        monkeypatch.setenv("MARITACA_API_URL", "https://chat.maritaca.ai")

        captured = {}

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                captured["body"] = json
                return _FakeResponse({
                    "model": "sabia-3",
                    "choices": [{"message": {"content": '{"foo":"bar"}'}}],
                })

        monkeypatch.setattr("app.core.llm_providers.httpx.AsyncClient", _FakeClient)

        provider = llm_providers.MaritacaProvider(model="sabia-3")
        rf = {"type": "json_schema", "json_schema": {"name": "X", "schema": {"type": "object"}, "strict": True}}
        await provider.generate([{"role": "user", "content": "oi"}], response_format=rf)

        assert captured["body"]["response_format"] == rf

    @pytest.mark.asyncio
    async def test_gpt_oss_passes_response_format_to_body(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("OSS120B_URL", "https://hub.example/v1")
        monkeypatch.setenv("OSS120B_MODEL", "openai/gpt-oss-120b")

        captured = {}

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                captured["body"] = json
                return _FakeResponse({
                    "model": "openai/gpt-oss-120b",
                    "choices": [{"message": {"content": '{}'}}],
                })

        monkeypatch.setattr("app.core.llm_providers.httpx.AsyncClient", _FakeClient)

        provider = llm_providers.GPTOSSProvider(size="120b")
        rf = {"type": "json_schema", "json_schema": {"name": "Y", "schema": {"type": "object"}, "strict": True}}
        await provider.generate([{"role": "user", "content": "x"}], response_format=rf)

        assert captured["body"]["response_format"] == rf

    @pytest.mark.asyncio
    async def test_ollama_translates_json_schema_to_json_object(self, monkeypatch, fresh_settings):
        """Ollama nativo aceita só json_object (sem schema), então o provider
        traduz response_format={"type":"json_schema",...} → {"type":"json_object"}."""
        monkeypatch.setenv("OLLAMA_API_URL", "http://localhost:11434")
        monkeypatch.setenv("OLLAMA_MODEL", "gemma3:4b")

        captured = {}

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                captured["body"] = json
                return _FakeResponse({
                    "model": "gemma3:4b",
                    "choices": [{"message": {"content": '{}'}}],
                })

        monkeypatch.setattr("app.core.llm_providers.httpx.AsyncClient", _FakeClient)

        provider = llm_providers.OllamaProvider()
        rf_in = {"type": "json_schema", "json_schema": {"name": "X", "schema": {"type": "object"}, "strict": True}}
        await provider.generate([{"role": "user", "content": "x"}], response_format=rf_in)

        # Body deve ter response_format CONVERTIDO para json_object
        assert captured["body"]["response_format"] == {"type": "json_object"}
        # Schema completo foi descartado (Ollama nativo não honra)
        assert "json_schema" not in captured["body"]["response_format"]
