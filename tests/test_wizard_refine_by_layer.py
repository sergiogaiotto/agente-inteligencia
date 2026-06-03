"""Testes do refino por camada do Wizard IA (/api/v1/wizard/refine).

PR "IA, refine por camada": a persona/diretriz do refino muda conforme o
tipo (camada) do agente — `WizardRefineRequest.kind`:

- aobd (Orquestrador)              → diretiva de orquestração (missão + delegação)
- router (Roteador/AR)             → diretiva de classificação/roteamento
- subagent (SA) / vazio / desconhecido → comportamento histórico (retrocompat)

Testes puros (sem DB nem rede): `_refine_persona` é função pura; na rota,
`_resolve_wizard_llm` e `get_provider` são mockados — capturamos a system
message para provar que a persona certa foi escolhida por camada.
"""
from __future__ import annotations

import pytest

from app.routes import wizard


# ═════════════════════════════════════════════════════════════════
# _refine_persona — função pura de seleção de persona por camada
# ═════════════════════════════════════════════════════════════════
class TestRefinePersona:
    def test_aobd_retorna_persona_de_orquestracao(self):
        p = wizard._refine_persona("aobd")
        assert p == wizard._REFINE_PERSONA_AOBD
        assert "ORQUESTRADORES" in p
        assert "NUNCA executa a tarefa" in p
        assert "delega" in p

    def test_router_retorna_persona_de_roteamento(self):
        p = wizard._refine_persona("router")
        assert p == wizard._REFINE_PERSONA_AR
        assert "ROTEADORES" in p
        assert "classificação e roteamento" in p

    def test_subagent_mantem_comportamento_historico(self):
        p = wizard._refine_persona("subagent")
        assert p == wizard._REFINE_PERSONA_SA
        assert "refinamento de configurações de IA" in p

    def test_vazio_cai_no_sa(self):
        assert wizard._refine_persona("") == wizard._REFINE_PERSONA_SA

    def test_none_cai_no_sa(self):
        # tipagem diz str, mas defendemos contra None (client malformado)
        assert wizard._refine_persona(None) == wizard._REFINE_PERSONA_SA  # type: ignore[arg-type]

    def test_desconhecido_cai_no_sa(self):
        assert wizard._refine_persona("qualquer-coisa") == wizard._REFINE_PERSONA_SA

    def test_case_e_whitespace_sao_normalizados(self):
        assert wizard._refine_persona("  AOBD  ") == wizard._REFINE_PERSONA_AOBD
        assert wizard._refine_persona("Router") == wizard._REFINE_PERSONA_AR

    def test_personas_sao_distintas_por_camada(self):
        assert wizard._REFINE_PERSONA_AOBD != wizard._REFINE_PERSONA_AR
        assert wizard._REFINE_PERSONA_AOBD != wizard._REFINE_PERSONA_SA
        assert wizard._REFINE_PERSONA_AR != wizard._REFINE_PERSONA_SA


# ═════════════════════════════════════════════════════════════════
# WizardRefineRequest — modelo (kind opcional, default retrocompat)
# ═════════════════════════════════════════════════════════════════
class TestWizardRefineRequestModel:
    def test_kind_default_subagent(self):
        m = wizard.WizardRefineRequest(current_content="x", instruction="y")
        assert m.kind == "subagent"

    def test_kind_aceita_override(self):
        m = wizard.WizardRefineRequest(
            current_content="x", instruction="y", kind="aobd"
        )
        assert m.kind == "aobd"


# ═════════════════════════════════════════════════════════════════
# Rota /refine — escolhe a persona por kind (mocks, sem rede/DB)
# ═════════════════════════════════════════════════════════════════
def _patch_llm(monkeypatch):
    """Mocka _resolve_wizard_llm + get_provider; captura a system message."""
    captured: dict = {}

    async def _fake_resolve(data, route):
        captured["route"] = route
        return ("openai", "gpt-4o-mini", "instruct")

    class _FakeProvider:
        async def generate(self, messages, **kwargs):
            captured["messages"] = messages
            captured["system"] = next(
                (m["content"] for m in messages if m["role"] == "system"), None
            )
            return {"content": "REFINADO"}

    def _fake_get_provider(provider_name, **kwargs):
        captured["provider_name"] = provider_name
        captured["kwargs"] = kwargs
        return _FakeProvider()

    monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)
    monkeypatch.setattr(wizard, "get_provider", _fake_get_provider)
    return captured


class TestWizardRefineRoute:
    @pytest.mark.asyncio
    async def test_aobd_usa_persona_de_orquestracao(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        out = await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="faça tudo sozinho",
                instruction="melhore",
                field="system_prompt",
                kind="aobd",
            )
        )
        assert out == {"status": "ok", "refined": "REFINADO"}
        assert captured["system"] == wizard._REFINE_PERSONA_AOBD
        assert "ORQUESTRADORES" in captured["system"]

    @pytest.mark.asyncio
    async def test_router_usa_persona_de_roteamento(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="x", instruction="y",
                field="system_prompt", kind="router",
            )
        )
        assert captured["system"] == wizard._REFINE_PERSONA_AR

    @pytest.mark.asyncio
    async def test_subagent_usa_persona_historica(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="x", instruction="y",
                field="system_prompt", kind="subagent",
            )
        )
        assert captured["system"] == wizard._REFINE_PERSONA_SA

    @pytest.mark.asyncio
    async def test_kind_ausente_usa_sa_retrocompat(self, monkeypatch):
        # Client antigo não envia kind → default subagent → persona SA.
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="x", instruction="y", field="system_prompt"
            )
        )
        assert captured["system"] == wizard._REFINE_PERSONA_SA

    @pytest.mark.asyncio
    async def test_user_message_carrega_conteudo_instrucao_e_campo(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="CONTEUDO_ATUAL",
                instruction="INSTRUCAO_X",
                field="system_prompt",
                kind="aobd",
            )
        )
        user = next(
            m["content"] for m in captured["messages"] if m["role"] == "user"
        )
        assert "CONTEUDO_ATUAL" in user
        assert "INSTRUCAO_X" in user
        assert "system_prompt" in user

    @pytest.mark.asyncio
    async def test_refine_resolve_llm_pela_rota_refine(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_refine(
            wizard.WizardRefineRequest(
                current_content="x", instruction="y", kind="aobd"
            )
        )
        # /refine resolve o LLM via task_type=instruct (rota "refine").
        assert captured["route"] == "refine"

    @pytest.mark.asyncio
    async def test_provider_error_vira_http_500(self, monkeypatch):
        async def _fake_resolve(data, route):
            return ("openai", "gpt-4o-mini", "instruct")

        def _boom(*a, **k):
            raise RuntimeError("sem credencial")

        monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)
        monkeypatch.setattr(wizard, "get_provider", _boom)

        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_refine(
                wizard.WizardRefineRequest(
                    current_content="x", instruction="y", kind="aobd"
                )
            )
        assert ei.value.status_code == 500
