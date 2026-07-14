"""Bug user 2026-06-01: estava iterando no chat workspace com 2 mensagens
seguidas (mesma sessão, agente _Análise de Texto). Ao recarregar pela
sidebar, só a última apareceu — a primeira sumiu.

Workflow paralelo diagnosticou: `app/routes/workspace.py:598` chamava
`execute_interaction(..., session_id=data.session_id)`, mas
`app/agents/engine.py:1145` invocava `fsm.run_intake(...)` SEM propagar
`session_id`. Daí `state_machine.py:139` criava sempre `uuid.uuid4()`
novo, fragmentando a conversa em interactions distintas.

Comparação com paths que JÁ funcionavam:
- `/chat` declarativo (workspace.py:560-582) — reusa via find_by_id
- `/invoke-binding-direct` (PRs #243 + #246) — reusa via _persist_invoke_turn

Fix: replicar o padrão de reuse no FSM (run_intake aceita session_id) +
propagar de execute_interaction e execute_pipeline.
"""
from __future__ import annotations


import pytest


# ─── Mocks dos repos ────────────────────────────────────────────────


class _RepoStore:
    """Simula interactions_repo + turns_repo capturando estado in-memory."""

    def __init__(self):
        self.interactions = {}    # id → dict
        self.turns = []            # list of dicts
        self.create_count = 0
        self.update_count = 0

    async def int_find_by_id(self, sid):
        return self.interactions.get(sid)

    async def int_create(self, row):
        self.create_count += 1
        self.interactions[row["id"]] = dict(row)
        return row

    async def int_update(self, sid, patch):
        self.update_count += 1
        if sid in self.interactions:
            self.interactions[sid].update(patch)
        return True

    async def turns_find_all(self, interaction_id=None, limit=500, **_):
        return [t for t in self.turns if t.get("interaction_id") == interaction_id]

    async def turns_create(self, row):
        self.turns.append(dict(row))
        return row


@pytest.fixture
def store(monkeypatch):
    s = _RepoStore()
    monkeypatch.setattr("app.agents.state_machine.interactions_repo.find_by_id", s.int_find_by_id)
    monkeypatch.setattr("app.agents.state_machine.interactions_repo.create", s.int_create)
    monkeypatch.setattr("app.agents.state_machine.interactions_repo.update", s.int_update)
    monkeypatch.setattr("app.agents.state_machine.turns_repo.find_all", s.turns_find_all)
    monkeypatch.setattr("app.agents.state_machine.turns_repo.create", s.turns_create)

    # transition() chama audit_repo.create — mock no-op pra não bater no Postgres
    async def fake_audit_create(_row):
        return _row

    monkeypatch.setattr("app.agents.state_machine.audit_repo.create", fake_audit_create)
    return s


# ─── Reusing session — caminho central do bug ────────────────────────


class TestRunIntakeReusesSession:
    @pytest.mark.asyncio
    async def test_no_session_id_creates_new(self, store):
        """Comportamento legado: sem session_id, cria interaction nova."""
        from app.agents.state_machine import InteractionContext, InteractionStateMachine

        fsm = InteractionStateMachine(InteractionContext())
        await fsm.run_intake("ola", agent_id="agent-1")

        assert store.create_count == 1
        assert fsm.ctx.next_user_turn == 1
        assert len(store.turns) == 1
        assert store.turns[0]["turn_number"] == 1

    @pytest.mark.asyncio
    async def test_session_id_existing_reuses_interaction(self, store):
        """REGRESSÃO do bug: session_id apontando para interaction existente
        deve REUSAR, não criar nova."""
        from app.agents.state_machine import InteractionContext, InteractionStateMachine

        # Setup: já existe uma sessão com 2 turns (request 1 + output 2)
        store.interactions["sess-X"] = {
            "id": "sess-X", "agent_id": "agent-1", "state": "LogAndClose",
        }
        store.turns.append({"interaction_id": "sess-X", "turn_number": 1})
        store.turns.append({"interaction_id": "sess-X", "turn_number": 2})

        fsm = InteractionStateMachine(InteractionContext())
        await fsm.run_intake("segunda pergunta", agent_id="agent-1", session_id="sess-X")

        # NÃO criou interaction nova
        assert store.create_count == 0
        # MAS atualizou state da existente
        assert store.update_count >= 1
        # turn_number da request = max(1,2) + 1 = 3
        assert fsm.ctx.next_user_turn == 3
        # Novo turn gravado com numeração correta
        new_turn = [t for t in store.turns if t.get("turn_number") == 3]
        assert len(new_turn) == 1
        assert new_turn[0]["interaction_id"] == "sess-X"

    @pytest.mark.asyncio
    async def test_session_id_inexistent_creates_with_that_id(self, store):
        """session_id que não existe no DB → cria interaction usando esse
        id como id da nova sessão (não muda o id pra UUID novo)."""
        from app.agents.state_machine import InteractionContext, InteractionStateMachine

        fsm = InteractionStateMachine(InteractionContext())
        await fsm.run_intake("ola", agent_id="agent-1", session_id="my-id")

        assert store.create_count == 1
        assert "my-id" in store.interactions
        assert fsm.ctx.interaction_id == "my-id"
        assert fsm.ctx.next_user_turn == 1

    @pytest.mark.asyncio
    async def test_empty_session_id_falls_back_to_uuid(self, store):
        """session_id="" (vazio) trata como ausente."""
        from app.agents.state_machine import InteractionContext, InteractionStateMachine

        fsm = InteractionStateMachine(InteractionContext())
        await fsm.run_intake("ola", agent_id="agent-1", session_id="")

        assert store.create_count == 1
        # ID é UUID, não string vazia
        assert fsm.ctx.interaction_id and len(fsm.ctx.interaction_id) > 10

    @pytest.mark.asyncio
    async def test_db_failure_in_lookup_logs_and_falls_back_to_create(
        self, store, monkeypatch, caplog
    ):
        """find_by_id explodindo (DB down) → cai em criar nova interaction
        + log warning para troubleshooting (segue padrão hardening #250)."""
        import logging

        async def fake_find(_sid):
            raise RuntimeError("DB indisponível")

        monkeypatch.setattr(
            "app.agents.state_machine.interactions_repo.find_by_id", fake_find
        )

        from app.agents.state_machine import InteractionContext, InteractionStateMachine

        fsm = InteractionStateMachine(InteractionContext())
        with caplog.at_level(logging.WARNING, logger="app.agents.state_machine"):
            await fsm.run_intake("ola", agent_id="agent-1", session_id="sess-X")

        # Cai no caminho de criar (não derruba a interação)
        assert store.create_count == 1
        # Log warning emitido
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "state_machine.session_lookup" in events


# ─── run_log_and_close turn dinâmico ────────────────────────────────


class TestLogAndCloseTurnDynamic:
    @pytest.mark.asyncio
    async def test_new_session_output_turn_is_2(self, store):
        """Sessão nova: turn input=1, output=2 (legado)."""
        from app.agents.state_machine import (
            InteractionContext, InteractionStateMachine, State,
        )

        ctx = InteractionContext(
            interaction_id="sess-fresh",
            next_user_turn=1,
            current_state=State.RECOMMEND,  # estado válido pra LogAndClose
            final_output="resposta",
        )
        fsm = InteractionStateMachine(ctx)
        await fsm.run_log_and_close()

        out_turns = [t for t in store.turns if "output_text_redacted" in t]
        assert len(out_turns) == 1
        assert out_turns[0]["turn_number"] == 2

    @pytest.mark.asyncio
    async def test_reused_session_output_turn_pareado(self, store):
        """Sessão reutilizada: turn input=5 → output=6 (sem sobrescrever)."""
        from app.agents.state_machine import (
            InteractionContext, InteractionStateMachine, State,
        )

        ctx = InteractionContext(
            interaction_id="sess-X",
            next_user_turn=5,
            current_state=State.RECOMMEND,
            final_output="resposta",
        )
        fsm = InteractionStateMachine(ctx)
        await fsm.run_log_and_close()

        out_turns = [t for t in store.turns if "output_text_redacted" in t]
        assert len(out_turns) == 1
        assert out_turns[0]["turn_number"] == 6


# ─── Smoke do source (propagação engine.py) ─────────────────────────


class TestEnginePropagatesSessionId:
    def test_execute_interaction_passes_session_id_to_run_intake(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"
        ).read_text(encoding="utf-8")
        # Linha que propaga
        assert "fsm.run_intake(user_input, agent_id, journey, channel, session_id=session_id)" in src

    def test_execute_pipeline_accepts_session_id(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"
        ).read_text(encoding="utf-8")
        # Assinatura com session_id
        assert "session_id: str | None = None," in src
        # Só primeiro agente do pipeline reaproveita
        assert "session_id=session_id if i == 0 else None" in src

    def test_workspace_chat_handler_propagates_session_id_to_pipeline(self):
        """Tanto /chat/stream (linha ~362) quanto /chat (linha ~424) devem
        passar data.session_id quando mode='pipeline'."""
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent / "app" / "routes" / "workspace.py"
        ).read_text(encoding="utf-8")
        # Pelo menos 2 occurrences (uma em /chat/stream, outra em /chat)
        assert src.count("session_id=data.session_id,") >= 2
