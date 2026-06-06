"""Memória de conversa multi-turno (2026-06-06).

Bug real ("Doc Analise", turno 2): a 1ª iteração funcionou (anexo + "liste em
bullets" → boa resposta pedindo detalhes), mas o follow-up do usuário ("sobre
qual o tema") chegou SEM contexto — o pipeline era stateless por turno (o seed
do grafo levava só o turno ATUAL). O roteador respondia "não consegui entender".

A causa: o histórico ERA persistido (`turns`: user/output em linhas separadas) e
o workspace reconstruía pra UI, mas nunca voltava ao LLM nem ao gate. Fix em duas
frentes que compartilham o MESMO carregador de turnos (`conversation_memory`):

- PR-A: `build_history_messages` reinjeta a janela recente (escopada por camada:
  router médio / aobd leve / subagent off) no seed do grafo.
- PR-B: `session_text_window` vira o sinal pegajoso que o gate condicional mistura
  em `text_all` — follow-up vago casa a keyword de turnos anteriores.

`context_mode` ('auto' default / 'none' stateless) controla os dois. Toda falha é
fail-open (histórico é melhoria, nunca derruba a execução).

Testes a nível de helper (igual test_mesh_default_and_attachments.py): a lógica
vive em funções puras/mockáveis; a integração no seed de execute_interaction
depende de DB+LLM e é exercida no smoke manual / homolog.
"""
from __future__ import annotations

import json
import logging

import pytest


# ─── Saneamento de modo / janela por camada (funções puras) ──────────


class TestContextModeHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("auto", "auto"),
            ("none", "none"),
            ("client", "client"),
            ("summary", "summary"),
            ("AUTO", "auto"),       # normaliza caixa
            ("  none  ", "none"),   # normaliza espaços
            ("", "auto"),           # vazio → default seguro
            (None, "auto"),         # None → default seguro
            ("lixo", "auto"),       # valor arbitrário → default seguro
        ],
    )
    def test_normalize_context_mode(self, raw, expected):
        from app.agents.conversation_memory import normalize_context_mode
        assert normalize_context_mode(raw) == expected

    @pytest.mark.parametrize(
        "mode,enabled",
        [
            ("none", False),
            ("auto", True),
            ("client", True),
            ("summary", True),
            ("", True),       # default 'auto' → habilitado
            (None, True),     # default 'auto' → habilitado
            ("lixo", True),   # cai em 'auto' → habilitado
        ],
    )
    def test_context_enabled(self, mode, enabled):
        from app.agents.conversation_memory import context_enabled
        assert context_enabled(mode) is enabled

    @pytest.mark.parametrize(
        "kind,window",
        [
            ("router", 8),     # AR — médio (resolve anáfora do follow-up)
            ("aobd", 4),       # AOBD — leve
            ("subagent", 0),   # SA — off (tarefa-folha)
            ("ROUTER", 8),     # normaliza caixa
            ("desconhecido", 4),  # kind fora do mapa → DEFAULT (leve)
            (None, 4),
        ],
    )
    def test_history_window_for_kind(self, kind, window):
        from app.agents.conversation_memory import history_window_for_kind
        assert history_window_for_kind(kind) == window


# ─── Carregador de turnos (anti-drift com a UI do workspace) ─────────


def _patch_turns(monkeypatch, rows, *, raise_exc=None):
    """Mocka turns_repo.find_all com as linhas dadas (ou exception)."""
    async def fake_find_all(interaction_id=None, limit=200, **_):
        if raise_exc is not None:
            raise raise_exc
        return rows

    monkeypatch.setattr("app.core.database.turns_repo.find_all", fake_find_all)


def _row(turn_number, *, user=None, output=None):
    """Linha de `turns` no formato persistido (user e output em LINHAS separadas
    no fluxo real: Intake grava o user, LogAndClose grava o output em turn+1)."""
    return {
        "turn_number": turn_number,
        "user_text_redacted": user,
        "output_text_redacted": output,
    }


class TestLoadConversationTurns:
    @pytest.mark.asyncio
    async def test_empty_session_id_returns_empty(self, monkeypatch):
        from app.agents.conversation_memory import load_conversation_turns
        # Nem chega a tocar o DB — short-circuit por session_id vazio
        assert await load_conversation_turns("") == []

    @pytest.mark.asyncio
    async def test_db_error_is_fail_open(self, monkeypatch, caplog):
        """DB down → [] (histórico é opcional) + warning estruturado."""
        from app.agents.conversation_memory import load_conversation_turns
        _patch_turns(monkeypatch, [], raise_exc=RuntimeError("db down"))
        with caplog.at_level(logging.WARNING, logger="app.agents.conversation_memory"):
            out = await load_conversation_turns("sess-1")
        assert out == []
        rec = next((r for r in caplog.records if getattr(r, "event", None) == "context.load"), None)
        assert rec is not None
        assert getattr(rec, "session_id", None) == "sess-1"

    @pytest.mark.asyncio
    async def test_reconstruction_interleaves_and_sorts(self, monkeypatch):
        """Linhas separadas (user@1, output@2, user@3, output@4) viram mensagens
        cronológicas user/assistant intercaladas. Ordena por turn_number asc."""
        from app.agents.conversation_memory import load_conversation_turns
        rows = [
            _row(3, user="pergunta 2"),
            _row(1, user="pergunta 1"),
            _row(4, output="resposta 2"),
            _row(2, output="resposta 1"),
        ]
        _patch_turns(monkeypatch, rows)
        out = await load_conversation_turns("sess-1")
        assert [(m["role"], m["content"]) for m in out] == [
            ("user", "pergunta 1"),
            ("assistant", "resposta 1"),
            ("user", "pergunta 2"),
            ("assistant", "resposta 2"),
        ]

    @pytest.mark.asyncio
    async def test_row_with_both_fields(self, monkeypatch):
        """Linha legada com user E output juntos → 2 mensagens no mesmo turno."""
        from app.agents.conversation_memory import load_conversation_turns
        _patch_turns(monkeypatch, [_row(1, user="oi", output="olá")])
        out = await load_conversation_turns("sess-1")
        assert [(m["role"], m["content"]) for m in out] == [
            ("user", "oi"),
            ("assistant", "olá"),
        ]

    @pytest.mark.asyncio
    async def test_blank_fields_skipped(self, monkeypatch):
        """Campos vazios/whitespace não viram mensagem."""
        from app.agents.conversation_memory import load_conversation_turns
        _patch_turns(monkeypatch, [_row(1, user="   ", output=""), _row(2, user="real")])
        out = await load_conversation_turns("sess-1")
        assert [(m["role"], m["content"]) for m in out] == [("user", "real")]

    @pytest.mark.asyncio
    async def test_before_turn_excludes_current(self, monkeypatch):
        """before_turn=3 → exclui turnos >= 3 (não duplica o turno atual que o
        FSM já persistiu no Intake antes do grafo rodar)."""
        from app.agents.conversation_memory import load_conversation_turns
        rows = [
            _row(1, user="p1"), _row(2, output="r1"),
            _row(3, user="p2 atual"), _row(4, output="r2"),
        ]
        _patch_turns(monkeypatch, rows)
        out = await load_conversation_turns("sess-1", before_turn=3)
        assert [(m["role"], m["content"]) for m in out] == [
            ("user", "p1"),
            ("assistant", "r1"),
        ]

    @pytest.mark.asyncio
    async def test_legacy_refusal_decoded(self, monkeypatch):
        """JSON legado de recusa vira texto legível (espelha workspace, anti-drift)."""
        from app.agents.conversation_memory import load_conversation_turns
        refusal = json.dumps({"type": "refusal", "reason": "fora de escopo", "next_step": "procure o RH"})
        _patch_turns(monkeypatch, [_row(2, output=refusal)])
        out = await load_conversation_turns("sess-1")
        assert out[0]["role"] == "assistant"
        assert "Recusa controlada" in out[0]["content"]
        assert "fora de escopo" in out[0]["content"]
        assert "procure o RH" in out[0]["content"]

    @pytest.mark.asyncio
    async def test_legacy_escalation_decoded(self, monkeypatch):
        from app.agents.conversation_memory import load_conversation_turns
        esc = json.dumps({"type": "escalation", "reason": "precisa de humano"})
        _patch_turns(monkeypatch, [_row(2, output=esc)])
        out = await load_conversation_turns("sess-1")
        assert "Escalação" in out[0]["content"]
        assert "precisa de humano" in out[0]["content"]


# ─── Teto de caracteres ──────────────────────────────────────────────


class TestApplyCharBudget:
    def test_keeps_recent_drops_oldest(self):
        from app.agents.conversation_memory import _apply_char_budget
        msgs = [
            {"role": "user", "content": "A" * 100},
            {"role": "assistant", "content": "B" * 100},
            {"role": "user", "content": "C" * 100},
        ]
        out = _apply_char_budget(msgs, 150)
        # Mantém o mais recente; corta do mais antigo
        assert out[-1]["content"] == "C" * 100
        assert len(out) < 3

    def test_budget_zero_is_noop(self):
        from app.agents.conversation_memory import _apply_char_budget
        msgs = [{"role": "user", "content": "x"}]
        assert _apply_char_budget(msgs, 0) == msgs

    def test_always_keeps_at_least_most_recent(self):
        """Mesmo a última msg estourando o budget, mantém ela (não zera tudo)."""
        from app.agents.conversation_memory import _apply_char_budget
        msgs = [{"role": "user", "content": "Z" * 9999}]
        out = _apply_char_budget(msgs, 10)
        assert len(out) == 1


# ─── build_history_messages (PR-A: histórico no seed do LLM) ─────────


class TestBuildHistoryMessages:
    @pytest.mark.asyncio
    async def test_context_none_returns_empty(self, monkeypatch):
        from app.agents.conversation_memory import build_history_messages
        _patch_turns(monkeypatch, [_row(1, user="oi")])
        assert await build_history_messages("sess-1", "router", "none") == []

    @pytest.mark.asyncio
    async def test_no_session_returns_empty(self, monkeypatch):
        from app.agents.conversation_memory import build_history_messages
        assert await build_history_messages("", "router", "auto") == []

    @pytest.mark.asyncio
    async def test_subagent_window_zero_returns_empty(self, monkeypatch):
        """Subagente: janela 0 → não carrega histórico mesmo com turnos."""
        from app.agents.conversation_memory import build_history_messages
        _patch_turns(monkeypatch, [_row(1, user="oi"), _row(2, output="olá")])
        assert await build_history_messages("sess-1", "subagent", "auto") == []

    @pytest.mark.asyncio
    async def test_router_returns_langchain_messages(self, monkeypatch):
        from langchain_core.messages import AIMessage, HumanMessage
        from app.agents.conversation_memory import build_history_messages
        _patch_turns(monkeypatch, [_row(1, user="qual o tema?"), _row(2, output="o relatório X")])
        out = await build_history_messages("sess-1", "router", "auto")
        assert len(out) == 2
        assert isinstance(out[0], HumanMessage) and out[0].content == "qual o tema?"
        assert isinstance(out[1], AIMessage) and out[1].content == "o relatório X"

    @pytest.mark.asyncio
    async def test_window_slices_to_layer_size(self, monkeypatch):
        """aobd janela=4: 6 mensagens → mantém só as 4 mais recentes."""
        from app.agents.conversation_memory import build_history_messages
        rows = [
            _row(1, user="p1"), _row(2, output="r1"),
            _row(3, user="p2"), _row(4, output="r2"),
            _row(5, user="p3"), _row(6, output="r3"),
        ]
        _patch_turns(monkeypatch, rows)
        out = await build_history_messages("sess-1", "aobd", "auto")
        contents = [m.content for m in out]
        assert contents == ["p2", "r2", "p3", "r3"]

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty(self, monkeypatch):
        from app.agents.conversation_memory import build_history_messages
        _patch_turns(monkeypatch, [])
        assert await build_history_messages("sess-1", "router", "auto") == []

    @pytest.mark.asyncio
    async def test_before_turn_excludes_current(self, monkeypatch):
        from app.agents.conversation_memory import build_history_messages
        rows = [_row(1, user="p1"), _row(2, output="r1"), _row(3, user="p2 atual")]
        _patch_turns(monkeypatch, rows)
        out = await build_history_messages("sess-1", "router", "auto", before_turn=3)
        assert [m.content for m in out] == ["p1", "r1"]


# ─── session_text_window (PR-B: sinal pegajoso do gate) ──────────────


class TestSessionTextWindow:
    @pytest.mark.asyncio
    async def test_context_none_returns_empty(self, monkeypatch):
        from app.agents.conversation_memory import session_text_window
        _patch_turns(monkeypatch, [_row(1, user="oi")])
        assert await session_text_window("sess-1", "none") == ""

    @pytest.mark.asyncio
    async def test_user_text_only_no_assistant(self, monkeypatch):
        """Só texto do USUÁRIO — o output do agente NÃO entra (senão o roteador
        perpetuaria um ramo pelo próprio texto)."""
        from app.agents.conversation_memory import session_text_window
        _patch_turns(monkeypatch, [_row(1, user="Relatório de Rentabilidade"), _row(2, output="aqui está o resumo")])
        out = await session_text_window("sess-1", "auto")
        assert "rentabilidade" in out          # texto do usuário, lowercase
        assert "resumo" not in out             # output do agente fora

    @pytest.mark.asyncio
    async def test_empty_when_no_user_turns(self, monkeypatch):
        from app.agents.conversation_memory import session_text_window
        _patch_turns(monkeypatch, [_row(2, output="só output")])
        assert await session_text_window("sess-1", "auto") == ""

    @pytest.mark.asyncio
    async def test_char_budget_keeps_recent(self, monkeypatch):
        from app.agents.conversation_memory import session_text_window, SESSION_TEXT_CHAR_BUDGET
        big = "x" * (SESSION_TEXT_CHAR_BUDGET + 500)
        _patch_turns(monkeypatch, [_row(1, user=big), _row(3, user="palavra_recente")])
        out = await session_text_window("sess-1", "auto")
        assert len(out) <= SESSION_TEXT_CHAR_BUDGET
        assert "palavra_recente" in out  # o mais recente é preservado


# ─── PR-B no gate condicional do engine (sinal pegajoso → text_all) ──


class TestGateStickySessionSignal:
    def test_build_context_folds_session_text_into_text_all(self):
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(
            user_input="sobre qual o tema",  # turno atual VAGO
            session_text="relatório de rentabilidade 2026",  # turno anterior
        )
        assert "sobre qual o tema" in ctx["text_all"]
        assert "rentabilidade" in ctx["text_all"]  # keyword pegajosa do histórico
        assert ctx["session_text"] == "relatório de rentabilidade 2026"

    def test_build_context_no_session_text_is_legacy(self):
        """Sem session_text → text_all byte-idêntico ao legado (só o input)."""
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(user_input="oi")
        assert ctx["text_all"] == "oi"
        assert ctx["session_text"] == ""

    def test_session_text_var_in_meta(self):
        """A UI do vars panel lê CONDITIONAL_VARS_META — sem drift com runtime."""
        from app.agents.engine import CONDITIONAL_VARS_META
        names = {v["name"] for v in CONDITIONAL_VARS_META}
        assert "session_text" in names

    @pytest.mark.asyncio
    async def test_followup_matches_via_session_text(self, monkeypatch):
        """REGRESSÃO turno-2: pergunta atual vaga ("sobre qual o tema") + expr de
        keyword. Sem o sinal pegajoso, NÃO casaria e o SA seria pulado (dead-end).
        Com session_text do turno anterior, a keyword casa → NÃO pula."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router",
                "target_agent_id": "docs",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "'rentab' in text_all"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        # Sem session_text → pula (keyword não está no turno atual)
        skip_no_ctx = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="sobre qual o tema",
        )
        assert skip_no_ctx is True

        # Com session_text (turno anterior falou de rentabilidade) → NÃO pula
        skip_with_ctx = await eng._should_skip_conditional(
            source_id="router", target_id="docs",
            last_output="...", last_final_state="",
            user_input="sobre qual o tema",
            session_text="relatorio de rentabilidade do trimestre",
        )
        assert skip_with_ctx is False


# ─── Defaults de schema (API + workspace) ────────────────────────────


class TestSchemaContextModeDefaults:
    def test_chat_message_default_auto(self):
        from app.models.schemas import ChatMessage
        m = ChatMessage(agent_id="a", message="oi")
        assert m.context_mode == "auto"

    def test_chat_message_accepts_none(self):
        from app.models.schemas import ChatMessage
        m = ChatMessage(agent_id="a", message="oi", context_mode="none")
        assert m.context_mode == "none"

    def test_invoke_request_default_auto(self):
        from app.models.schemas import AgentInvokeRequest
        assert AgentInvokeRequest().context_mode == "auto"

    def test_invoke_request_accepts_none(self):
        from app.models.schemas import AgentInvokeRequest
        assert AgentInvokeRequest(context_mode="none").context_mode == "none"
