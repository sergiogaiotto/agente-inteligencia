"""Testes da resiliência de LLM do Wizard (mentor + refine).

PR "wizard: fallback hospedado quando o modelo roteado está inacessível":
o default de `instruct` aponta para o hub interno (GPT-OSS), que fica
inacessível fora da rede corporativa/VPN. Antes o wizard morria com 500
genérico após ~21s. Agora:

- detecta falha de ALCANCE (conexão/timeout/URL não configurada) — não de
  request malformado;
- cai no modelo HOSPEDADO do `multimodal_fallback` do Roteamento LLM
  (azure/gpt-4o por padrão — acessível pela internet);
- se nem o fallback responder → HTTPException 503 com mensagem ACIONÁVEL
  (não o 500 genérico).

Testes puros (sem DB/rede): os helpers são funções; nas rotas, mockamos
`_resolve_wizard_llm`, `get_provider` e `load_routing`.
"""
from __future__ import annotations

import httpx
import pytest

from app.routes import wizard


# ═════════════════════════════════════════════════════════════════
# _is_llm_unreachable — classifica falha de ALCANCE vs erro qualquer
# ═════════════════════════════════════════════════════════════════
class TestIsLlmUnreachable:
    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("All connection attempts failed"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("pool"),
        httpx.TimeoutException("generic timeout"),
    ])
    def test_erros_de_rede_sao_inacessivel(self, exc):
        assert wizard._is_llm_unreachable(exc) is True

    def test_url_nao_configurada_e_inacessivel(self):
        exc = RuntimeError("gpt-oss-20b: URL não configurada. Configure em /settings.")
        assert wizard._is_llm_unreachable(exc) is True

    def test_url_nao_configurada_case_insensitive(self):
        assert wizard._is_llm_unreachable(RuntimeError("X: URL NÃO CONFIGURADA")) is True

    @pytest.mark.parametrize("exc", [
        RuntimeError("sem credencial"),
        ValueError("Provedor 'x' não suportado"),
        KeyError("content"),
        Exception("falha genérica"),
    ])
    def test_outros_erros_nao_sao_inacessivel(self, exc):
        assert wizard._is_llm_unreachable(exc) is False


# ═════════════════════════════════════════════════════════════════
# _is_llm_auth_error — 401/credencial recusada (≠ inacessível)
# ═════════════════════════════════════════════════════════════════
class _AuthErr(Exception):
    status_code = 401


class TestIsLlmAuthError:
    @pytest.mark.parametrize("exc", [
        _AuthErr("denied"),
        Exception("Error code: 401 - Incorrect API key provided: sk-proj-xxx"),
        Exception("invalid_api_key"),
        Exception("HTTP 401 Unauthorized"),
        type("AuthenticationError", (Exception,), {})("bad key"),
    ])
    def test_detecta_401_e_credencial(self, exc):
        assert wizard._is_llm_auth_error(exc) is True

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("All connection attempts failed"),
        RuntimeError("URL não configurada"),
        RuntimeError("sem credencial"),
        Exception("falha genérica 500"),
    ])
    def test_rede_e_genericos_nao_sao_auth(self, exc):
        assert wizard._is_llm_auth_error(exc) is False


# ═════════════════════════════════════════════════════════════════
# _wizard_unreachable_message — texto acionável
# ═════════════════════════════════════════════════════════════════
class TestUnreachableMessage:
    def test_inclui_provider_model_e_orientacao(self):
        msg = wizard._wizard_unreachable_message("gpt-oss-20b", "openai/gpt-oss-20b")
        assert "gpt-oss-20b/openai/gpt-oss-20b" in msg
        assert "inacessível" in msg
        assert "VPN" in msg
        assert "Roteamento LLM" in msg

    def test_sem_model_mostra_so_provider(self):
        msg = wizard._wizard_unreachable_message("azure", "")
        assert "azure" in msg
        assert "azure/" not in msg

    def test_auth_message_inclui_provider_401_e_orientacao(self):
        msg = wizard._wizard_auth_message("openai_public", "openai/gpt-4.1")
        assert "openai_public/openai/gpt-4.1" in msg
        assert "401" in msg
        assert "API key" in msg
        assert "Roteamento LLM" in msg


# ═════════════════════════════════════════════════════════════════
# _wizard_hosted_fallback — lê multimodal_fallback do routing
# ═════════════════════════════════════════════════════════════════
def _patch_routing(monkeypatch, target: str = "azure/gpt-4o"):
    async def _fake_load_routing():
        return {"multimodal_fallback": target, "instruct": "gpt-oss-20b/openai/gpt-oss-20b"}
    monkeypatch.setattr(wizard, "load_routing", _fake_load_routing)


class TestHostedFallback:
    @pytest.mark.asyncio
    async def test_retorna_alvo_do_multimodal_fallback(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        p, m = await wizard._wizard_hosted_fallback("gpt-oss-20b")
        assert (p, m) == ("azure", "gpt-4o")

    @pytest.mark.asyncio
    async def test_nao_cai_no_mesmo_provider_que_falhou(self, monkeypatch):
        # fallback aponta pro MESMO provider que falhou → sem alternativa.
        _patch_routing(monkeypatch, "azure/gpt-4o")
        p, m = await wizard._wizard_hosted_fallback("azure")
        assert (p, m) == (None, None)

    @pytest.mark.asyncio
    async def test_normaliza_case_e_espacos(self, monkeypatch):
        _patch_routing(monkeypatch, "  Azure / gpt-4o  ")
        p, m = await wizard._wizard_hosted_fallback("gpt-oss-120b")
        assert (p, m) == ("azure", "gpt-4o")

    @pytest.mark.asyncio
    async def test_target_vazio_ou_sem_barra_nao_tem_fallback(self, monkeypatch):
        _patch_routing(monkeypatch, "")
        assert await wizard._wizard_hosted_fallback("gpt-oss-20b") == (None, None)
        _patch_routing(monkeypatch, "azuregpt4o")  # sem "/"
        assert await wizard._wizard_hosted_fallback("gpt-oss-20b") == (None, None)

    @pytest.mark.asyncio
    async def test_routing_indisponivel_degrada_sem_fallback(self, monkeypatch):
        async def _boom():
            raise RuntimeError("db offline")
        monkeypatch.setattr(wizard, "load_routing", _boom)
        assert await wizard._wizard_hosted_fallback("gpt-oss-20b") == (None, None)


# ═════════════════════════════════════════════════════════════════
# Fakes de provider para _wizard_llm_complete e rotas
# ═════════════════════════════════════════════════════════════════
class _FakeProvider:
    def __init__(self, behavior: str):
        self.behavior = behavior

    async def generate(self, messages, **kwargs):
        kind, _, val = self.behavior.partition(":")
        if kind == "connect":
            raise httpx.ConnectError("All connection attempts failed")
        if kind == "timeout":
            raise httpx.ReadTimeout("slow")
        if kind == "url":
            raise RuntimeError("gpt-oss-20b: URL não configurada")
        if kind == "auth":
            # simula AuthenticationError do provider (401 — chave inválida)
            raise Exception(
                val or "Error code: 401 - {'error': {'message': "
                "'Incorrect API key provided: sk-proj-xxx'}}"
            )
        if kind == "runtime":
            raise RuntimeError(val or "erro genérico")
        if kind == "ok":
            return {"content": val}
        raise AssertionError(f"behavior desconhecido: {self.behavior!r}")


def _patch_providers(monkeypatch, behaviors: dict[str, str], captured: dict | None = None):
    """Mocka get_provider → _FakeProvider por nome de provider."""
    def _fake_get_provider(provider_name, **kwargs):
        if captured is not None:
            captured.setdefault("providers", []).append(provider_name)
            captured.setdefault("models", []).append(kwargs.get("model"))
            captured.setdefault("efforts", []).append(kwargs.get("reasoning_effort"))
        if provider_name not in behaviors:
            raise AssertionError(f"provider inesperado: {provider_name}")
        return _FakeProvider(behaviors[provider_name])
    monkeypatch.setattr(wizard, "get_provider", _fake_get_provider)


_MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


# ═════════════════════════════════════════════════════════════════
# _wizard_llm_complete — orquestra primário + fallback hospedado
# ═════════════════════════════════════════════════════════════════
class TestWizardLlmComplete:
    @pytest.mark.asyncio
    async def test_primario_ok_usa_primario(self, monkeypatch):
        _patch_providers(monkeypatch, {"gpt-oss-20b": "ok:PRIMARIO"})
        content, p, m = await wizard._wizard_llm_complete(
            _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="mentor"
        )
        assert (content, p, m) == ("PRIMARIO", "gpt-oss-20b", "openai/gpt-oss-20b")

    @pytest.mark.asyncio
    async def test_primario_inacessivel_cai_no_fallback(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "ok:FALLBACK"})
        content, p, m = await wizard._wizard_llm_complete(
            _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="mentor"
        )
        assert (content, p, m) == ("FALLBACK", "azure", "gpt-4o")

    @pytest.mark.asyncio
    async def test_url_nao_configurada_tambem_cai_no_fallback(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "url", "azure": "ok:FB"})
        content, p, m = await wizard._wizard_llm_complete(
            _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="refine"
        )
        assert content == "FB" and p == "azure"

    @pytest.mark.asyncio
    async def test_primario_e_fallback_inacessiveis_viram_503(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "connect"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard._wizard_llm_complete(
                _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="mentor"
            )
        assert ei.value.status_code == 503
        assert "inacessível" in ei.value.detail
        assert "gpt-oss-20b" in ei.value.detail

    @pytest.mark.asyncio
    async def test_sem_fallback_configurado_vira_503(self, monkeypatch):
        _patch_routing(monkeypatch, "")  # sem multimodal_fallback
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard._wizard_llm_complete(
                _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="mentor"
            )
        assert ei.value.status_code == 503

    @pytest.mark.asyncio
    async def test_erro_nao_de_alcance_propaga_nao_vira_503(self, monkeypatch):
        # Erro de credencial NÃO é de alcance → propaga (caller mapeia p/ 500).
        _patch_providers(monkeypatch, {"gpt-oss-20b": "runtime:sem credencial"})
        with pytest.raises(RuntimeError):
            await wizard._wizard_llm_complete(
                _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="mentor"
            )

    @pytest.mark.asyncio
    async def test_fallback_com_erro_nao_de_alcance_propaga(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "runtime:no key"})
        with pytest.raises(RuntimeError):
            await wizard._wizard_llm_complete(
                _MSGS, "gpt-oss-20b", "openai/gpt-oss-20b", route="mentor"
            )

    # ── C1b: 401/credencial recusada também cai no fallback hospedado ──
    @pytest.mark.asyncio
    async def test_primario_401_cai_no_fallback(self, monkeypatch):
        # Cenário real do deploy: skill_generation com chave OpenAI inválida (401);
        # antes virava 500. Agora cai no multimodal_fallback (azure) saudável.
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"openai_public": "auth", "azure": "ok:FB_AUTH"})
        content, p, m = await wizard._wizard_llm_complete(
            _MSGS, "openai_public", "openai/gpt-4.1", route="skill"
        )
        assert (content, p) == ("FB_AUTH", "azure")

    @pytest.mark.asyncio
    async def test_primario_e_fallback_401_viram_503_de_credencial(self, monkeypatch):
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"openai_public": "auth", "azure": "auth"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard._wizard_llm_complete(
                _MSGS, "openai_public", "openai/gpt-4.1", route="skill"
            )
        assert ei.value.status_code == 503
        # mensagem de CREDENCIAL (não a de inacessível)
        assert "401" in ei.value.detail and "credenc" in ei.value.detail.lower()

    @pytest.mark.asyncio
    async def test_primario_401_sem_fallback_vira_503_de_credencial(self, monkeypatch):
        _patch_routing(monkeypatch, "")  # sem multimodal_fallback distinto
        _patch_providers(monkeypatch, {"openai_public": "auth"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard._wizard_llm_complete(
                _MSGS, "openai_public", "openai/gpt-4.1", route="skill"
            )
        assert ei.value.status_code == 503
        assert "credenc" in ei.value.detail.lower()


# ═════════════════════════════════════════════════════════════════
# Rotas — fallback de ponta a ponta (mentor + refine)
# ═════════════════════════════════════════════════════════════════
def _patch_resolve_instruct(monkeypatch):
    async def _fake_resolve(data, route):
        return ("gpt-oss-20b", "openai/gpt-oss-20b", "instruct")
    monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)


class TestMentorRouteFallback:
    @pytest.mark.asyncio
    async def test_mentor_inacessivel_responde_via_fallback(self, monkeypatch):
        _patch_resolve_instruct(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "ok:MENTOR_FB"})
        out = await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="como faço?", kind="subagent")
        )
        assert out == {"status": "ok", "answer": "MENTOR_FB"}

    @pytest.mark.asyncio
    async def test_mentor_sem_alcance_nenhum_vira_503_acionavel(self, monkeypatch):
        _patch_resolve_instruct(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "connect"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_mentor(
                wizard.WizardMentorRequest(question="como faço?", kind="aobd")
            )
        assert ei.value.status_code == 503
        assert "Roteamento LLM" in ei.value.detail


class TestRefineRouteFallback:
    @pytest.mark.asyncio
    async def test_refine_inacessivel_responde_via_fallback(self, monkeypatch):
        _patch_resolve_instruct(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "ok:REFINE_FB"})
        out = await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="x", instruction="y", field="system_prompt"
            )
        )
        assert out == {"status": "ok", "refined": "REFINE_FB"}

    @pytest.mark.asyncio
    async def test_refine_503_passa_direto_nao_vira_500(self, monkeypatch):
        # /refine tem `except Exception → 500`; o 503 do helper precisa
        # passar pelo `except HTTPException: raise` ANTES disso.
        _patch_resolve_instruct(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-20b": "connect", "azure": "connect"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_refine(
                wizard.WizardRefineRequest(
                    current_content="x", instruction="y", field="system_prompt"
                )
            )
        assert ei.value.status_code == 503


# ═════════════════════════════════════════════════════════════════
# Rotas — fallback de ponta a ponta (/skill + /agent)
# Incidente req_c60a15302ffd (2026-07-03): as duas rotas chamavam
# get_provider().generate() CRU — hub gpt-oss fora da VPN → ConnectError
# virava 500 genérico sem nenhuma tentativa de fallback.
# ═════════════════════════════════════════════════════════════════
def _patch_resolve_reasoning(monkeypatch):
    async def _fake_resolve(data, route):
        return ("gpt-oss-120b", "openai/gpt-oss-120b", "reasoning")
    monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)


def _patch_validation_ok(monkeypatch):
    """Validação da SKILL gerada passa limpa — isola o teste no FALLBACK
    (conteúdo fake tipo "SKILL_FB" parsea, e o validador real flagaria
    crítico → retry → chamadas extras de provider não-determinísticas)."""
    import app.skill_parser.wizard_validator as validator_mod

    class _OkValidation:
        ok = True
        critical_count = 0
        warning_count = 0
        violations = []
        def critical_suggestions(self):
            return []
        def to_dict(self):
            return {"ok": True}

    monkeypatch.setattr(
        validator_mod, "validate_generated_skill",
        lambda parsed, bindings, raw_md=None: _OkValidation(),
    )


class TestSkillRouteFallback:
    @pytest.mark.asyncio
    async def test_skill_inacessivel_responde_via_fallback(self, monkeypatch):
        _patch_resolve_reasoning(monkeypatch)
        _patch_validation_ok(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-120b": "connect", "azure": "ok:SKILL_FB"})
        out = await wizard.wizard_skill(wizard.WizardSkillRequest(description="skill de teste"))
        assert out["status"] == "ok"
        assert out["skill_md"] == "SKILL_FB"
        # `resolved` reporta o LLM que REALMENTE respondeu (pós-fallback).
        assert out["resolved"]["llm_provider"] == "azure"
        assert out["resolved"]["llm_model"] == "gpt-4o"
        assert out["resolved"]["llm_fallback"] is True

    @pytest.mark.asyncio
    async def test_skill_primario_ok_nao_marca_fallback(self, monkeypatch):
        _patch_resolve_reasoning(monkeypatch)
        _patch_validation_ok(monkeypatch)
        _patch_providers(monkeypatch, {"gpt-oss-120b": "ok:DIRETO"})
        out = await wizard.wizard_skill(wizard.WizardSkillRequest(description="skill"))
        assert out["resolved"]["llm_provider"] == "gpt-oss-120b"
        assert out["resolved"]["llm_fallback"] is False

    @pytest.mark.asyncio
    async def test_skill_503_passa_direto_nao_vira_500(self, monkeypatch):
        _patch_resolve_reasoning(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-120b": "connect", "azure": "connect"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_skill(wizard.WizardSkillRequest(description="skill"))
        assert ei.value.status_code == 503
        assert "inacessível" in ei.value.detail

    @pytest.mark.asyncio
    async def test_skill_pede_reasoning_ao_primario_e_ao_fallback(self, monkeypatch):
        # O wizard PASSA reasoning_effort ao factory nas duas tentativas; o
        # gate por modelo (dropar p/ gpt-4o, manter p/ gpt-oss) vive no
        # get_provider real — coberto em test_reasoning_effort.py.
        _patch_resolve_reasoning(monkeypatch)
        _patch_validation_ok(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        captured: dict = {}
        _patch_providers(
            monkeypatch, {"gpt-oss-120b": "connect", "azure": "ok:FB"}, captured
        )
        await wizard.wizard_skill(wizard.WizardSkillRequest(description="skill"))
        assert captured["providers"] == ["gpt-oss-120b", "azure"]
        assert captured["efforts"] == ["high", "high"]


class TestAgentRouteFallback:
    @pytest.mark.asyncio
    async def test_agent_inacessivel_responde_via_fallback(self, monkeypatch):
        _patch_resolve_reasoning(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {
            "gpt-oss-120b": "connect",
            "azure": 'ok:{"name": "Agente X", "kind": "subagent"}',
        })
        out = await wizard.wizard_agent(wizard.WizardAgentRequest(description="agente"))
        assert out["status"] == "ok"
        assert out["agent"]["name"] == "Agente X"

    @pytest.mark.asyncio
    async def test_agent_503_passa_direto_nao_vira_500(self, monkeypatch):
        _patch_resolve_reasoning(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        _patch_providers(monkeypatch, {"gpt-oss-120b": "connect", "azure": "connect"})
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_agent(wizard.WizardAgentRequest(description="agente"))
        assert ei.value.status_code == 503
        assert "Roteamento LLM" in ei.value.detail

    @pytest.mark.asyncio
    async def test_agent_pede_reasoning_effort(self, monkeypatch):
        _patch_resolve_reasoning(monkeypatch)
        captured: dict = {}
        _patch_providers(
            monkeypatch, {"gpt-oss-120b": 'ok:{"name": "A"}'}, captured
        )
        await wizard.wizard_agent(wizard.WizardAgentRequest(description="agente"))
        assert captured["efforts"] == ["high"]


def _patch_validation_critical(monkeypatch):
    """Validador sempre reprova com crítico → força o retry de validação."""
    import app.skill_parser.parser as parser_mod
    import app.skill_parser.wizard_validator as validator_mod

    class _CriticalValidation:
        ok = False
        critical_count = 1
        warning_count = 0
        violations = []
        def critical_suggestions(self):
            return ["corrija X"]
        def to_dict(self):
            return {"ok": False}

    monkeypatch.setattr(parser_mod, "parse_skill_md", lambda md: {"parsed": True})
    monkeypatch.setattr(
        validator_mod, "validate_generated_skill",
        lambda parsed, bindings, raw_md=None: _CriticalValidation(),
    )


class TestSkillRetryReusesRespondingProvider:
    @pytest.mark.asyncio
    async def test_retry_de_validacao_reusa_o_par_que_respondeu(self, monkeypatch):
        # Primário caiu → fallback respondeu a geração inicial. Se o validador
        # pedir retry, o retry vai DIRETO ao par que respondeu (azure) — sem
        # re-pagar o timeout do primário morto.
        _patch_resolve_reasoning(monkeypatch)
        _patch_validation_critical(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        captured: dict = {}
        _patch_providers(
            monkeypatch, {"gpt-oss-120b": "connect", "azure": "ok:GERADO"}, captured
        )

        out = await wizard.wizard_skill(wizard.WizardSkillRequest(description="skill"))
        assert out["status"] == "ok"
        # 3 chamadas ao factory: primário (connect), fallback (ok), retry.
        # O retry NÃO volta ao gpt-oss-120b morto — vai direto ao azure.
        assert captured["providers"] == ["gpt-oss-120b", "azure", "azure"]
        assert out["validation"]["retries_used"] == 1

    @pytest.mark.asyncio
    async def test_retry_que_caiu_no_fallback_atualiza_resolved(self, monkeypatch):
        # Hub INTERMITENTE: geração inicial responde no gpt-oss; o retry de
        # validação encontra o hub morto e cai no azure. O conteúdo FINAL é o
        # do retry → `resolved` reporta o par que o gerou (azure) + flag.
        _patch_resolve_reasoning(monkeypatch)
        _patch_validation_critical(monkeypatch)
        _patch_routing(monkeypatch, "azure/gpt-4o")
        state = {"oss_calls": 0}

        class _FlakyOss:
            async def generate(self, messages, **kwargs):
                state["oss_calls"] += 1
                if state["oss_calls"] == 1:
                    return {"content": "GERADO_OSS"}
                raise httpx.ConnectError("All connection attempts failed")

        class _AzureOk:
            async def generate(self, messages, **kwargs):
                return {"content": "RETRY_AZURE"}

        monkeypatch.setattr(
            wizard, "get_provider",
            lambda name, **kw: _FlakyOss() if name == "gpt-oss-120b" else _AzureOk(),
        )

        out = await wizard.wizard_skill(wizard.WizardSkillRequest(description="skill"))
        assert out["skill_md"] == "RETRY_AZURE"
        assert out["resolved"]["llm_provider"] == "azure"
        assert out["resolved"]["llm_model"] == "gpt-4o"
        assert out["resolved"]["llm_fallback"] is True
        assert out["validation"]["retries_used"] == 1
        assert state["oss_calls"] == 2  # inicial ok + retry no hub morto


# ═════════════════════════════════════════════════════════════════
# Rejeição de PARÂMETRO no wizard — strip-retry de reasoning_effort
# (backend OpenAI-compatible estrito rejeita campo desconhecido c/ 400;
#  não é unreachable nem auth → antes viraria 500)
# ═════════════════════════════════════════════════════════════════
class TestWizardParamRejectionStripRetry:
    @pytest.mark.asyncio
    async def test_400_de_parametro_re_tenta_sem_reasoning_effort(self, monkeypatch):
        calls = {"efforts": []}

        class _PickyProvider:
            def __init__(self, effort):
                self.effort = effort
            async def generate(self, messages, **kwargs):
                if self.effort:
                    raise RuntimeError(
                        "gpt-oss-120b HTTP 400: Extra inputs are not permitted"
                    )
                return {"content": "SEM_EFFORT"}

        def _fake_get_provider(provider_name, **kwargs):
            calls["efforts"].append(kwargs.get("reasoning_effort"))
            return _PickyProvider(kwargs.get("reasoning_effort"))

        monkeypatch.setattr(wizard, "get_provider", _fake_get_provider)
        content, p, m = await wizard._wizard_llm_complete(
            _MSGS, "gpt-oss-120b", "openai/gpt-oss-120b", route="skill",
            reasoning_effort="high",
        )
        assert (content, p) == ("SEM_EFFORT", "gpt-oss-120b")
        assert calls["efforts"] == ["high", None]

    @pytest.mark.asyncio
    async def test_sem_effort_pedido_400_de_parametro_propaga(self, monkeypatch):
        # Sem reasoning_effort no pedido não há o que despir — o 400 propaga
        # (erro de config/uso que o operador deve ver, não mascarar).
        _patch_providers(monkeypatch, {
            "gpt-oss-120b": "runtime:HTTP 400: Extra inputs are not permitted",
        })
        with pytest.raises(RuntimeError):
            await wizard._wizard_llm_complete(
                _MSGS, "gpt-oss-120b", "openai/gpt-oss-120b", route="skill"
            )


def test_toast_de_erro_e_legivel_e_dispensavel():
    """O 503 acionável do wizard tem ~330 chars — em 3,5s fixos ninguém lê.
    Toasts type='error' escalam a duração com o tamanho e fecham no clique
    (showToast em base.html); sucesso/info mantêm os 3,5s."""
    from pathlib import Path
    src = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")
    assert "Math.min(15000, Math.max(6000, msg.length * 45)) : 3500" in src
    assert "Clique para fechar" in src
