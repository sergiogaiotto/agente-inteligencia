"""Testes do retry on contract failure (Verifier — Wave atual).

Quando ContractValidator marca compliant=false, Verifier re-chama o LLM
1x com instrução de correção. Este arquivo cobre os caminhos:

- Setting desabilitado → no-op
- llm_provider_name ausente → no-op
- Compliant na 1ª tentativa → no-op
- Retry corrige → contract_compliant=True, draft substituído
- Retry falha → mantém errors do 2º attempt, original em contract_original_errors
- Exceção no provider durante retry → segue com result original

Mocks: provider.generate via monkeypatch. Não toca LLM real nem Postgres.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import config as _config
from app.verifier.runtime import Verifier


@pytest.fixture
def fresh_settings(monkeypatch):
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


@pytest.fixture
def v2_enabled(monkeypatch, fresh_settings):
    """Habilita Verifier v2 (default OFF na config)."""
    monkeypatch.setenv("VERIFIER_V2_ENABLED", "true")
    yield


# Schema simples pra testar retry: requer campo "answer".
_SIMPLE_CONTRACT = """```json
{
  "type": "object",
  "title": "TestOutput",
  "required": ["answer"],
  "properties": {"answer": {"type": "string"}},
  "additionalProperties": false
}
```"""


def _mock_provider_returning(content: str):
    """Cria um get_provider mock que devolve `content` no .generate()."""
    fake_provider = MagicMock()
    fake_provider.supports_structured_output = False  # simplifica path
    fake_provider.generate = AsyncMock(return_value={"content": content, "model": "fake", "usage": {}})
    return fake_provider


# ═════════════════════════════════════════════════════════════════
# Casos onde retry NÃO deve rodar
# ═════════════════════════════════════════════════════════════════


class TestNoRetryConditions:
    @pytest.mark.asyncio
    async def test_setting_disabled_no_retry(self, monkeypatch, v2_enabled):
        """verifier_contract_retry_enabled=false → mesmo com falha, sem retry."""
        monkeypatch.setenv("VERIFIER_CONTRACT_RETRY_ENABLED", "false")

        provider_called = {"n": 0}
        def _fake_get_provider(*a, **kw):
            provider_called["n"] += 1
            return _mock_provider_returning('{"answer":"ok"}')
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        # Mock judge desabilitado pra simplificar
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        # Draft inválido — não tem "answer" requerido
        result = await v.verify(
            draft='{"foo": "bar"}',
            output_contract=_SIMPLE_CONTRACT,
            user_question="teste",
            profile="fast",
            persist=False,
            llm_provider_name="azure",
            llm_model="gpt-4o",
        )
        # Retry NÃO foi tentado
        assert result.contract_retried is False
        assert provider_called["n"] == 0
        assert result.contract_compliant is False

    @pytest.mark.asyncio
    async def test_no_llm_provider_no_retry(self, monkeypatch, v2_enabled):
        """llm_provider_name=None → não tem como chamar LLM, retry desabilitado."""
        provider_called = {"n": 0}
        def _fake_get_provider(*a, **kw):
            provider_called["n"] += 1
            return _mock_provider_returning('{"answer":"ok"}')
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        result = await v.verify(
            draft='{"foo": "bar"}',
            output_contract=_SIMPLE_CONTRACT,
            user_question="teste",
            profile="fast",
            persist=False,
            # llm_provider_name OMITIDO → retry desabilitado
        )
        assert result.contract_retried is False
        assert provider_called["n"] == 0

    @pytest.mark.asyncio
    async def test_compliant_first_attempt_no_retry(self, monkeypatch, v2_enabled):
        """Draft válido na primeira → não roda retry."""
        provider_called = {"n": 0}
        def _fake_get_provider(*a, **kw):
            provider_called["n"] += 1
            return _mock_provider_returning('whatever')
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        result = await v.verify(
            draft='{"answer": "tudo certo"}',  # válido
            output_contract=_SIMPLE_CONTRACT,
            user_question="teste",
            profile="fast",
            persist=False,
            llm_provider_name="azure",
        )
        assert result.contract_compliant is True
        assert result.contract_retried is False
        assert provider_called["n"] == 0


# ═════════════════════════════════════════════════════════════════
# Casos onde retry RODA — sucesso, falha, exceção
# ═════════════════════════════════════════════════════════════════


class TestRetryFlow:
    @pytest.mark.asyncio
    async def test_retry_corrects_compliance(self, monkeypatch, v2_enabled):
        """1ª tentativa inválida → retry → LLM corrige → compliant=True final.
        contract_original_errors mantém erros do 1º attempt."""
        # Provider retorna JSON corrigido
        fake = _mock_provider_returning('{"answer": "corrigido pelo retry"}')
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        result = await v.verify(
            draft='{"foo": "bar"}',  # sem "answer"
            output_contract=_SIMPLE_CONTRACT,
            user_question="qual a resposta?",
            profile="fast",
            persist=False,
            llm_provider_name="azure",
            llm_model="gpt-4o",
        )

        assert result.contract_retried is True
        assert result.contract_compliant is True  # corrigido!
        assert result.contract_errors == []
        # Original preservado pra audit
        assert len(result.contract_original_errors) > 0
        # Draft corrigido capturado
        assert "corrigido" in result.contract_retry_draft

        # Provider foi chamado exatamente 1x (sem loop infinito)
        fake.generate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_also_fails(self, monkeypatch, v2_enabled):
        """LLM ignora a correção e devolve mesmo JSON ruim → final permanece
        compliant=False. contract_original_errors == contract_errors (mesmos).
        contract_retry_draft tem o output do 2º attempt pra forensics."""
        # Provider retorna JSON AINDA inválido (sem "answer")
        fake = _mock_provider_returning('{"still": "broken"}')
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        result = await v.verify(
            draft='{"foo": "bar"}',
            output_contract=_SIMPLE_CONTRACT,
            user_question="x",
            profile="fast",
            persist=False,
            llm_provider_name="azure",
        )

        assert result.contract_retried is True
        assert result.contract_compliant is False  # AINDA falho
        assert len(result.contract_errors) > 0
        assert len(result.contract_original_errors) > 0
        # Capturou o draft do 2º attempt pra operador analisar
        assert "broken" in result.contract_retry_draft

    @pytest.mark.asyncio
    async def test_retry_provider_raises_falls_back_gracefully(self, monkeypatch, v2_enabled):
        """Provider lança exceção durante retry (rede, timeout, etc) →
        Verifier não propaga, segue com erros do 1º attempt."""
        fake = MagicMock()
        fake.supports_structured_output = False
        fake.generate = AsyncMock(side_effect=RuntimeError("network down"))
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        result = await v.verify(
            draft='{"foo": "bar"}',
            output_contract=_SIMPLE_CONTRACT,
            user_question="x",
            profile="fast",
            persist=False,
            llm_provider_name="azure",
        )

        # Retry foi tentado mas falhou — não substituiu o draft
        assert result.contract_compliant is False
        assert len(result.contract_errors) > 0
        # contract_retried=False porque new_draft veio vazio (exception)
        assert result.contract_retried is False
        # original_errors permanece vazio (não houve segundo attempt válido)


# ═════════════════════════════════════════════════════════════════
# _retry_contract_with_llm — função helper isolada
# ═════════════════════════════════════════════════════════════════


class TestRetryHelper:
    @pytest.mark.asyncio
    async def test_builds_correction_prompt_with_errors_and_contract(self, monkeypatch):
        """Verifica que o prompt do retry contém: erros, contrato, draft anterior."""
        captured = {}
        async def _capture(messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return {"content": '{"answer":"ok"}', "model": "x", "usage": {}}

        fake = MagicMock()
        fake.supports_structured_output = False
        fake.generate = _capture
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)

        out, usage = await Verifier._retry_contract_with_llm(
            original_draft='{"wrong": true}',
            errors=["missing required field: answer"],
            output_contract=_SIMPLE_CONTRACT,
            user_question="qual a meta?",
            llm_provider_name="azure",
            llm_model="gpt-4o",
            max_tokens=2000,
        )

        # FIN-2 (33.8.0): retorna (content, usage) — antes só content.
        assert out == '{"answer":"ok"}'
        assert usage == {}
        # Sanity sobre o prompt construído
        all_text = " ".join(m["content"] for m in captured["messages"])
        assert "missing required field: answer" in all_text
        assert "qual a meta?" in all_text
        assert "wrong" in all_text  # draft anterior incluído
        assert "TestOutput" in all_text  # contrato incluído
        assert captured["kwargs"]["max_tokens"] == 2000

    @pytest.mark.asyncio
    async def test_uses_structured_output_when_provider_supports(self, monkeypatch):
        """Provider com supports_structured_output=True → response_format
        é enviado junto pra garantir JSON válido também no retry."""
        captured = {}
        async def _capture(messages, **kwargs):
            captured["kwargs"] = kwargs
            return {"content": '{"answer":"ok"}', "model": "x", "usage": {}}

        fake = MagicMock()
        fake.supports_structured_output = True
        fake.generate = _capture
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)

        await Verifier._retry_contract_with_llm(
            original_draft='{"wrong": true}',
            errors=["missing answer"],
            output_contract=_SIMPLE_CONTRACT,
            user_question="x",
            llm_provider_name="azure",
            llm_model="gpt-4o",
        )

        # response_format foi passado com schema extraído
        rf = captured["kwargs"].get("response_format")
        assert rf is not None
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"]["required"] == ["answer"]
