"""Custo do juiz + contract-retry no ledger (33.8.0, FIN-1/2).

Parte unit: prova que `_retry_contract_with_llm` agora DEVOLVE o usage (antes
descartado — FIN-2). O SSOT (source='judge' no _persist, FIN-1) e a soma
juiz+retry são verificados end-to-end no Docker (LLM + Postgres reais).
"""
from __future__ import annotations

import pytest


class _FakeProvider:
    supports_structured_output = False

    async def generate(self, messages, **kw):
        return {
            "content": '{"fixed": true}',
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }


@pytest.mark.asyncio
async def test_retry_contract_devolve_content_e_usage(monkeypatch):
    from app.verifier.runtime import Verifier
    from app.core import llm_providers

    monkeypatch.setattr(llm_providers, "get_provider", lambda *a, **k: _FakeProvider())

    content, usage = await Verifier()._retry_contract_with_llm(
        original_draft="draft ruim",
        errors=["campo X ausente"],
        output_contract="```json\n{}\n```",
        user_question="q",
        llm_provider_name="azure",
        llm_model="gpt-4o",
    )
    assert content == '{"fixed": true}'
    # FIN-2: o usage (antes descartado) volta pro caller computar o custo do retry.
    assert usage == {"prompt_tokens": 12, "completion_tokens": 7}


@pytest.mark.asyncio
async def test_retry_content_vazio_ainda_devolve_tupla(monkeypatch):
    from app.verifier.runtime import Verifier
    from app.core import llm_providers

    class _Empty(_FakeProvider):
        async def generate(self, messages, **kw):
            return {"content": None}  # sem usage

    monkeypatch.setattr(llm_providers, "get_provider", lambda *a, **k: _Empty())
    content, usage = await Verifier()._retry_contract_with_llm(
        original_draft="x", errors=["e"], output_contract="c",
        user_question="q", llm_provider_name="azure", llm_model="gpt-4o",
    )
    assert content == ""
    assert usage == {}
