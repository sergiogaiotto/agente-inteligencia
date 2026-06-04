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
