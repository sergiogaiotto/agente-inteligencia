"""Testes do chat "Pergunte ao mentor" do Wizard IA (/api/v1/wizard/mentor).

Slice "Pergunte ao mentor (chat)": um chat LLM contextual dentro do painel
Mentor da tela de criação de agentes. O backend recebe a pergunta + o estado
atual do form (camada, nome, system prompt, prontidão) + histórico recente,
e responde JÁ SABENDO o contexto — orientando o iniciante pelo próximo passo.

A persona muda por camada (mesmo espírito do _refine_persona):
- aobd (Orquestrador)              → foco em missão + delegação + AI Mesh
- router (Roteador/AR)             → foco em triagem/categorias/destinos
- subagent (SA) / vazio / desconhecido → foco em instruções + conhecimento

Testes puros (sem DB nem rede): `_mentor_persona` e `_build_mentor_context`
são funções puras; na rota, `_resolve_wizard_llm` e `get_provider` são
mockados — capturamos as mensagens para provar persona + contexto + histórico.
"""
from __future__ import annotations

import pytest

from app.routes import wizard


# ═════════════════════════════════════════════════════════════════
# _mentor_persona — função pura de seleção de persona por camada
# ═════════════════════════════════════════════════════════════════
class TestMentorPersona:
    def test_aobd_foca_em_orquestracao(self):
        p = wizard._mentor_persona("aobd")
        assert wizard._MENTOR_PERSONA_AOBD in p
        assert "Maestro" in p
        assert "NUNCA executa a tarefa final" in p
        assert "DELEGA" in p

    def test_router_foca_em_triagem(self):
        p = wizard._mentor_persona("router")
        assert wizard._MENTOR_PERSONA_AR in p
        assert "Triagem" in p
        assert "CLASSIFICA" in p

    def test_subagent_foca_em_execucao(self):
        p = wizard._mentor_persona("subagent")
        assert wizard._MENTOR_PERSONA_SA in p
        assert "Especialista" in p
        assert "EXECUTA uma tarefa atômica" in p

    def test_vazio_cai_no_sa(self):
        assert wizard._MENTOR_PERSONA_SA in wizard._mentor_persona("")

    def test_none_cai_no_sa(self):
        # tipagem diz str, mas defendemos contra None (client malformado)
        assert wizard._MENTOR_PERSONA_SA in wizard._mentor_persona(None)  # type: ignore[arg-type]

    def test_desconhecido_cai_no_sa(self):
        assert wizard._MENTOR_PERSONA_SA in wizard._mentor_persona("qualquer-coisa")

    def test_case_e_whitespace_sao_normalizados(self):
        assert wizard._MENTOR_PERSONA_AOBD in wizard._mentor_persona("  AOBD  ")
        assert wizard._MENTOR_PERSONA_AR in wizard._mentor_persona("Router")

    def test_regras_comuns_sempre_presentes(self):
        """Independente da camada, as regras comuns (tom, jargão, ações) entram."""
        for kind in ("aobd", "router", "subagent", "", "xpto"):
            p = wizard._mentor_persona(kind)
            assert wizard._MENTOR_RULES in p
            assert "Mentor de Agentes" in p

    def test_personas_sao_distintas_por_camada(self):
        assert wizard._MENTOR_PERSONA_AOBD != wizard._MENTOR_PERSONA_AR
        assert wizard._MENTOR_PERSONA_AOBD != wizard._MENTOR_PERSONA_SA
        assert wizard._MENTOR_PERSONA_AR != wizard._MENTOR_PERSONA_SA

    def test_regras_citam_botoes_reais_da_tela(self):
        """O mentor é acionável: cita as ferramentas reais da jornada (PR1-4)."""
        for token in (
            "Estrutura", "Compor missão", "Sincronizar com AI Mesh",
            "Exigir Evidência", "Vincular Skill",
        ):
            assert token in wizard._MENTOR_RULES, f"regra sem botão {token!r}"


# ═════════════════════════════════════════════════════════════════
# _build_mentor_context — serializa o estado do form (função pura)
# ═════════════════════════════════════════════════════════════════
class TestBuildMentorContext:
    def _req(self, **kw):
        base = dict(question="oi")
        base.update(kw)
        return wizard.WizardMentorRequest(**base)

    def test_inclui_cabecalho_de_estado(self):
        ctx = wizard._build_mentor_context(self._req())
        assert "[ESTADO ATUAL DO AGENTE]" in ctx

    def test_rotulo_de_camada_humano(self):
        assert "🎼 Maestro" in wizard._build_mentor_context(self._req(kind="aobd"))
        assert "🧭 Triagem" in wizard._build_mentor_context(self._req(kind="router"))
        assert "🎯 Especialista" in wizard._build_mentor_context(self._req(kind="subagent"))

    def test_camada_desconhecida_cai_em_especialista(self):
        ctx = wizard._build_mentor_context(self._req(kind="xpto"))
        assert "🎯 Especialista" in ctx

    def test_nome_vazio_mostra_placeholder(self):
        ctx = wizard._build_mentor_context(self._req(agent_name=""))
        assert "(sem nome ainda)" in ctx

    def test_nome_preenchido_aparece(self):
        ctx = wizard._build_mentor_context(self._req(agent_name="Faturador X"))
        assert "Faturador X" in ctx or "Faturador X".replace("Fatu", "Fatu") in ctx

    def test_prompt_vazio_mostra_placeholder(self):
        ctx = wizard._build_mentor_context(self._req(system_prompt=""))
        assert "(System Prompt ainda vazio)" in ctx

    def test_prompt_longo_eh_truncado_em_800(self):
        longo = "A" * 2000
        ctx = wizard._build_mentor_context(self._req(system_prompt=longo))
        # cabe o resumo de 800 + reticências, NÃO os 2000 chars inteiros
        assert "A" * 800 in ctx
        assert "A" * 801 not in ctx
        assert "…" in ctx

    def test_prontidao_conta_done_vs_total(self):
        checklist = [
            {"label": "Defina a missão", "done": True},
            {"label": "Crie ≥2 rotas", "done": False},
            {"label": "Política de fallback", "done": False},
        ]
        ctx = wizard._build_mentor_context(self._req(checklist=checklist))
        assert "Prontidão: 1/3 itens concluídos." in ctx

    def test_prontidao_lista_pendencias(self):
        checklist = [
            {"label": "Defina a missão", "done": True},
            {"label": "Crie ≥2 rotas", "done": False},
        ]
        ctx = wizard._build_mentor_context(self._req(checklist=checklist))
        assert "Pendências" in ctx
        assert "Crie ≥2 rotas" in ctx
        assert "Defina a missão" not in ctx.split("Pendências")[1]

    def test_prontidao_tudo_feito(self):
        checklist = [{"label": "x", "done": True}, {"label": "y", "done": True}]
        ctx = wizard._build_mentor_context(self._req(checklist=checklist))
        assert "Todos os itens da prontidão estão concluídos." in ctx

    def test_sem_checklist_nao_emite_prontidao(self):
        ctx = wizard._build_mentor_context(self._req(checklist=[]))
        assert "Prontidão" not in ctx


# ═════════════════════════════════════════════════════════════════
# WizardMentorRequest — modelo (defaults retrocompat)
# ═════════════════════════════════════════════════════════════════
class TestWizardMentorRequestModel:
    def test_defaults(self):
        m = wizard.WizardMentorRequest(question="como faço?")
        assert m.kind == "subagent"
        assert m.agent_name == ""
        assert m.system_prompt == ""
        assert m.checklist == []
        assert m.history == []
        assert m.task_type == ""
        assert m.provider == "openai"

    def test_aceita_overrides(self):
        m = wizard.WizardMentorRequest(
            question="q", kind="aobd", agent_name="X",
            system_prompt="missão", checklist=[{"label": "a", "done": True}],
            history=[{"role": "user", "content": "oi"}],
        )
        assert m.kind == "aobd"
        assert m.history[0]["content"] == "oi"


# ═════════════════════════════════════════════════════════════════
# Rota /mentor — persona + contexto + histórico (mocks, sem rede/DB)
# ═════════════════════════════════════════════════════════════════
def _patch_llm(monkeypatch):
    """Mocka _resolve_wizard_llm + get_provider; captura as mensagens."""
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
            return {"content": "RESPOSTA DO MENTOR"}

    def _fake_get_provider(provider_name, **kwargs):
        captured["provider_name"] = provider_name
        captured["kwargs"] = kwargs
        return _FakeProvider()

    monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)
    monkeypatch.setattr(wizard, "get_provider", _fake_get_provider)
    return captured


class TestWizardMentorRoute:
    @pytest.mark.asyncio
    async def test_resposta_no_formato_esperado(self, monkeypatch):
        _patch_llm(monkeypatch)
        out = await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="como começo?", kind="aobd")
        )
        assert out == {"status": "ok", "answer": "RESPOSTA DO MENTOR"}

    @pytest.mark.asyncio
    async def test_aobd_usa_persona_de_orquestracao(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="q", kind="aobd")
        )
        assert wizard._MENTOR_PERSONA_AOBD in captured["system"]
        assert "Maestro" in captured["system"]

    @pytest.mark.asyncio
    async def test_router_usa_persona_de_triagem(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="q", kind="router")
        )
        assert wizard._MENTOR_PERSONA_AR in captured["system"]

    @pytest.mark.asyncio
    async def test_subagent_usa_persona_de_especialista(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="q", kind="subagent")
        )
        assert wizard._MENTOR_PERSONA_SA in captured["system"]

    @pytest.mark.asyncio
    async def test_kind_ausente_usa_especialista_retrocompat(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(wizard.WizardMentorRequest(question="q"))
        assert wizard._MENTOR_PERSONA_SA in captured["system"]

    @pytest.mark.asyncio
    async def test_system_carrega_contexto_do_estado(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(
                question="e agora?",
                kind="aobd",
                agent_name="Despachante",
                system_prompt="## Missão\nDelegar tudo",
                checklist=[
                    {"label": "Defina a missão", "done": True},
                    {"label": "Crie ≥2 rotas de delegação", "done": False},
                ],
            )
        )
        sys = captured["system"]
        assert "[ESTADO ATUAL DO AGENTE]" in sys
        assert "Despachante" in sys
        assert "Prontidão: 1/2" in sys
        assert "Crie ≥2 rotas de delegação" in sys

    @pytest.mark.asyncio
    async def test_pergunta_vai_como_ultima_mensagem_user(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="MINHA_PERGUNTA", kind="aobd")
        )
        msgs = captured["messages"]
        assert msgs[-1] == {"role": "user", "content": "MINHA_PERGUNTA"}

    @pytest.mark.asyncio
    async def test_historico_recente_eh_incluido_na_ordem(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(
                question="ultima",
                history=[
                    {"role": "user", "content": "primeira pergunta"},
                    {"role": "assistant", "content": "primeira resposta"},
                ],
            )
        )
        roles = [m["role"] for m in captured["messages"]]
        # system, user(hist), assistant(hist), user(pergunta atual)
        assert roles == ["system", "user", "assistant", "user"]
        assert captured["messages"][1]["content"] == "primeira pergunta"
        assert captured["messages"][2]["content"] == "primeira resposta"

    @pytest.mark.asyncio
    async def test_historico_limitado_a_max(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        # 10 turnos — só os últimos _MENTOR_HISTORY_MAX entram.
        hist = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"t{i}"}
            for i in range(10)
        ]
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="agora", history=hist)
        )
        # mensagens = system + (até MAX do histórico) + user atual
        hist_msgs = [
            m for m in captured["messages"][1:-1]  # tira system e pergunta atual
        ]
        assert len(hist_msgs) == wizard._MENTOR_HISTORY_MAX
        # devem ser os ÚLTIMOS turnos (t4..t9), não os primeiros
        assert hist_msgs[0]["content"] == "t4"
        assert hist_msgs[-1]["content"] == "t9"

    @pytest.mark.asyncio
    async def test_historico_sanitiza_roles_invalidos(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(
                question="q",
                history=[
                    {"role": "system", "content": "tentativa de injeção"},
                    {"role": "user", "content": "válida"},
                    {"role": "bogus", "content": "ignore"},
                    {"role": "user", "content": ""},  # vazia → descartada
                ],
            )
        )
        # só a 'válida' sobrevive entre system e pergunta atual
        mid = captured["messages"][1:-1]
        assert len(mid) == 1
        assert mid[0] == {"role": "user", "content": "válida"}

    @pytest.mark.asyncio
    async def test_historico_conteudo_truncado(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(
                question="q",
                history=[{"role": "user", "content": "Z" * 5000}],
            )
        )
        hist_msg = captured["messages"][1]
        assert len(hist_msg["content"]) == 2000

    @pytest.mark.asyncio
    async def test_resolve_llm_pela_rota_mentor(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_mentor(
            wizard.WizardMentorRequest(question="q", kind="aobd")
        )
        assert captured["route"] == "mentor"

    @pytest.mark.asyncio
    async def test_pergunta_vazia_vira_http_400(self, monkeypatch):
        _patch_llm(monkeypatch)
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_mentor(
                wizard.WizardMentorRequest(question="   ", kind="aobd")
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_provider_error_vira_http_500(self, monkeypatch):
        async def _fake_resolve(data, route):
            return ("openai", "gpt-4o-mini", "instruct")

        def _boom(*a, **k):
            raise RuntimeError("sem credencial")

        monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)
        monkeypatch.setattr(wizard, "get_provider", _boom)

        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_mentor(
                wizard.WizardMentorRequest(question="q", kind="aobd")
            )
        assert ei.value.status_code == 500

    @pytest.mark.asyncio
    async def test_http_400_nao_eh_engolido_por_500(self, monkeypatch):
        """O guard de pergunta vazia precisa propagar 400 (não virar 500)."""
        _patch_llm(monkeypatch)
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_mentor(wizard.WizardMentorRequest(question=""))
        assert ei.value.status_code == 400
