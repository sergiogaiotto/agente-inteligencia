"""Regressão pro bug 'preflight reclama de API key de azure quando agente usa task_type'.

Cenário real (UI passo 4 Revisão, 2026-05-27):
- User cria agente com task_type=tool_calling
- Routing global resolve pra `gpt-oss-120b/openai/gpt-oss-120b`
- Mas preflight ignora task_type, olha `payload['llm_provider']='azure'` (default)
- Mostra erro vermelho "API key do 'azure' não configurada" — falso positivo
  que bloqueia o save mesmo o agente nunca planejando usar azure.

Fix: `run_preflight` chama `_resolve_effective_payload` ANTES dos checks. Quando
task_type setado, o payload tem `llm_provider`/`model` substituídos pelo
resultado de `resolve_llm_for_task` (mesmo caminho que o engine usa em runtime).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.preflight import (
    _resolve_effective_payload,
    check_api_key,
    run_preflight,
)


# ─── _resolve_effective_payload ─────────────────────────────────────


class TestResolveEffectivePayload:
    @pytest.mark.asyncio
    async def test_no_task_type_returns_payload_intact(self):
        """Legacy path: sem task_type, payload passa direto sem chamar routing."""
        p = {"llm_provider": "azure", "model": "gpt-4o"}
        out = await _resolve_effective_payload(p)
        assert out is p
        assert out["llm_provider"] == "azure"

    @pytest.mark.asyncio
    async def test_task_type_replaces_provider_and_model(self):
        """Com task_type setado, payload retorna com provider/model do routing."""
        p = {"llm_provider": "azure", "model": "gpt-4o", "task_type": "tool_calling"}
        with patch(
            "app.llm_routing.resolve_llm_for_task",
            new=AsyncMock(return_value=("gpt-oss-120b", "openai/gpt-oss-120b")),
        ):
            out = await _resolve_effective_payload(p)
        assert out["llm_provider"] == "gpt-oss-120b"
        assert out["model"] == "openai/gpt-oss-120b"
        # Original intacto (cópia defensiva)
        assert p["llm_provider"] == "azure"

    @pytest.mark.asyncio
    async def test_resolve_failure_falls_back_to_snapshot(self):
        """Se routing explode (DB fora, settings corrompido), preserva snapshot —
        preflight não deve falhar inteiro por causa disso."""
        p = {"llm_provider": "azure", "model": "gpt-4o", "task_type": "tool_calling"}
        with patch(
            "app.llm_routing.resolve_llm_for_task",
            new=AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            out = await _resolve_effective_payload(p)
        assert out["llm_provider"] == "azure"


# ─── check_api_key contra providers GPT-OSS ─────────────────────────


class _FakeSettings:
    """Stub mínimo do Settings — só os attrs que check_api_key lê."""
    def __init__(self, **kw):
        self.azure_openai_api_key = kw.get("azure_openai_api_key", "")
        self.maritaca_api_key = kw.get("maritaca_api_key", "")
        self.ollama_api_key = kw.get("ollama_api_key", "ollama")
        self.oss20b_api_key = kw.get("oss20b_api_key", "")
        self.oss20b_url = kw.get("oss20b_url", "")
        self.oss120b_api_key = kw.get("oss120b_api_key", "")
        self.oss120b_url = kw.get("oss120b_url", "")


class TestCheckApiKeyGptOss:
    def test_gpt_oss_120b_with_url_set_passes(self):
        """gpt-oss-120b com URL configurada e key='not-needed' (proxy interno) — OK."""
        s = _FakeSettings(oss120b_url="https://hub.internal/v1", oss120b_api_key="not-needed")
        r = check_api_key({"llm_provider": "gpt-oss-120b"}, s)
        assert r is None

    def test_gpt_oss_120b_without_url_fails(self):
        """Sem URL configurada, o agente não chama nada — sinaliza."""
        s = _FakeSettings(oss120b_url="", oss120b_api_key="not-needed")
        r = check_api_key({"llm_provider": "gpt-oss-120b"}, s)
        assert r is not None
        assert r.severity == "error"
        assert "URL" in r.title

    def test_gpt_oss_20b_with_real_key_passes(self):
        s = _FakeSettings(oss20b_url="https://hub.internal/v1", oss20b_api_key="sk-real-key-abc123")
        r = check_api_key({"llm_provider": "gpt-oss-20b"}, s)
        assert r is None

    def test_azure_still_requires_key(self):
        """Regressão: caminho legacy continua funcionando — azure sem key falha."""
        s = _FakeSettings(azure_openai_api_key="")
        r = check_api_key({"llm_provider": "azure"}, s)
        assert r is not None
        assert r.severity == "error"
        assert "azure" in r.title.lower()


# ─── run_preflight: integração end-to-end ────────────────────────────


class TestRunPreflightWithTaskType:
    @pytest.mark.asyncio
    async def test_tool_calling_agent_does_not_fail_on_missing_azure_key(self):
        """O bug em questão: agente declara task_type=tool_calling, routing
        resolve gpt-oss-120b, mas preflight reclamava de azure key.

        Cenário: settings sem azure_openai_api_key (placeholder), mas com URL
        gpt-oss configurada. ANTES do fix: erro C1_api_key. DEPOIS: nenhum
        erro de api_key — possíveis warnings de skill_id, mas isso é outro
        check.
        """
        payload = {
            "name": "QMeuKB",
            "kind": "subagent",
            "task_type": "tool_calling",
            "llm_provider": "azure",  # snapshot legacy default
            "model": "gpt-4o",        # snapshot legacy default
            "system_prompt": "Você é um subagent dedicado a consulta KB usando MCP tools com instruções detalhadas e específicas pro domínio.",
            "temperature": 0.7,
            "version": "1.0.0",
        }

        with patch(
            "app.llm_routing.resolve_llm_for_task",
            new=AsyncMock(return_value=("gpt-oss-120b", "openai/gpt-oss-120b")),
        ), patch("app.core.config.get_settings") as gs:
            # Azure SEM key (placeholder), mas gpt-oss-120b configurado.
            gs.return_value = _FakeSettings(
                azure_openai_api_key="sk-your-placeholder",
                oss120b_url="https://hub.internal/v1",
                oss120b_api_key="not-needed",
            )
            report = await run_preflight(payload)

        # Nenhum erro de api_key — o que o user reclamou.
        api_key_errors = [
            c for c in report.checks
            if c.id == "C1_api_key" and c.severity == "error"
        ]
        assert not api_key_errors, (
            f"Preflight ainda reclamando de api_key apesar do task_type resolver "
            f"pra gpt-oss-120b: {[c.detail for c in api_key_errors]}"
        )

    @pytest.mark.asyncio
    async def test_legacy_agent_without_task_type_still_validates_azure(self):
        """Garantia de não-regressão do caminho legacy: agente sem task_type,
        usando llm_provider=azure, sem azure_openai_api_key → C1_api_key error."""
        payload = {
            "name": "Legacy",
            "kind": "subagent",
            "task_type": None,  # sem task_type
            "llm_provider": "azure",
            "model": "gpt-4o",
            "system_prompt": "Você é um subagent legacy com prompt suficientemente longo pra passar do check de passthrough.",
            "temperature": 0.7,
            "version": "1.0.0",
        }
        with patch("app.core.config.get_settings") as gs:
            gs.return_value = _FakeSettings(azure_openai_api_key="")
            report = await run_preflight(payload)

        api_key_errors = [
            c for c in report.checks
            if c.id == "C1_api_key" and c.severity == "error"
        ]
        assert api_key_errors, (
            "Legacy agent sem azure key DEVERIA falhar — preflight não pode passar batido"
        )
