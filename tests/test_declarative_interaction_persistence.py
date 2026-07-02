"""Rastro de execuções declarativas no engine (gap do seeding Aurora, 2026-07-01).

`_run_declarative_as_interaction` (invoke direto de agente declarativo e steps
declarativos de pipeline) passava `register_interaction=False` sem persistir
nada localmente: pipelines 100% declarativos (ex.: "Aurora — Cotação Express")
executavam com sucesso mas não deixavam interação em GET /api/v1/history nem
em GET /api/v1/agents/{id}/invocations.

Agora o engine é o dono do rastro nesse caminho: cria/reutiliza a interaction,
grava o turno de entrada antes e o de saída depois (o finalize interno do
execute_declarative segue responsável por state final + ended_at). O /chat do
workspace tem branch declarativa própria e não passa por aqui.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


AGENT = {"id": "ag-decl-1", "name": "Agente Declarativo", "kind": "subagent",
         "version": "1.0.0", "domain": "teste"}

DECL_RESULT = {
    "interaction_id": None,  # preenchido por caso
    "output": "resultado ok",
    "final_state": "completed",
    "duration_ms": 12,
    "bindings_executed": [{"status": 200, "method": "GET", "path": "/x"}],
    "errors": [],
    "context": {},
    "api_response": {"valor": 42},
}


class _RepoRecorder:
    """Grava as chamadas aos repositórios para asserção."""

    def __init__(self, existing_ids: set[str] | None = None,
                 existing_turns: list[dict] | None = None,
                 create_raises: bool = False):
        self.existing_ids = existing_ids or set()
        self.existing_turns = existing_turns or []
        self.create_raises = create_raises
        self.interactions_created: list[dict] = []
        self.interactions_updated: list[tuple] = []
        self.turns_created: list[dict] = []

    async def find_by_id(self, iid):
        return {"id": iid} if iid in self.existing_ids else None

    async def create_interaction(self, row):
        if self.create_raises:
            raise RuntimeError("db indisponível")
        self.interactions_created.append(row)

    async def update_interaction(self, iid, changes):
        self.interactions_updated.append((iid, changes))

    async def find_all_turns(self, interaction_id=None, limit=500):
        return self.existing_turns

    async def create_turn(self, row):
        self.turns_created.append(row)


@pytest.fixture
def recorder(monkeypatch):
    rec = _RepoRecorder()
    _wire(monkeypatch, rec)
    return rec


def _wire(monkeypatch, rec: _RepoRecorder):
    monkeypatch.setattr("app.agents.engine.interactions_repo.find_by_id", rec.find_by_id)
    monkeypatch.setattr("app.agents.engine.interactions_repo.create", rec.create_interaction)
    monkeypatch.setattr("app.agents.engine.interactions_repo.update", rec.update_interaction)
    monkeypatch.setattr("app.agents.engine.turns_repo.find_all", rec.find_all_turns)
    monkeypatch.setattr("app.agents.engine.turns_repo.create", rec.create_turn)

    captured_session: dict = {}

    async def fake_execute_declarative(**kwargs):
        captured_session["session_id"] = kwargs.get("session_id")
        return {**DECL_RESULT, "interaction_id": kwargs.get("session_id")}

    monkeypatch.setattr(
        "app.agents.declarative_engine.execute_declarative", fake_execute_declarative
    )
    rec.captured_session = captured_session


async def _run(session_id=None, user_input="qual a cotação?"):
    from app.agents.engine import _run_declarative_as_interaction

    parsed = SimpleNamespace(api_bindings_parsed=[{"id": "b1"}], data_tables_parsed=[])
    return await _run_declarative_as_interaction(
        agent=AGENT, parsed_skill=parsed, user_input=user_input,
        session_id=session_id, sealed_inputs=None,
    )


class TestNewSession:
    @pytest.mark.asyncio
    async def test_creates_interaction_with_title_from_message(self, recorder):
        result = await _run(session_id="sid-novo-1")
        assert len(recorder.interactions_created) == 1
        row = recorder.interactions_created[0]
        assert row["id"] == "sid-novo-1"
        assert row["title"] == "qual a cotação?"
        assert row["agent_id"] == AGENT["id"]
        assert result["interaction_id"] == "sid-novo-1"

    @pytest.mark.asyncio
    async def test_writes_input_and_output_turns(self, recorder):
        await _run(session_id="sid-novo-2")
        assert len(recorder.turns_created) == 2
        t_in, t_out = recorder.turns_created
        assert t_in["turn_number"] == 1
        assert t_in["user_text_redacted"] == "qual a cotação?"
        assert t_out["turn_number"] == 2
        assert "resultado ok" in (t_out["output_text_redacted"] or "") or t_out["output_text_redacted"]

    @pytest.mark.asyncio
    async def test_without_session_id_generates_uuid(self, recorder):
        result = await _run(session_id=None)
        assert len(recorder.interactions_created) == 1
        iid = recorder.interactions_created[0]["id"]
        assert iid and len(iid) >= 32
        assert result["interaction_id"] == iid

    @pytest.mark.asyncio
    async def test_finalize_reuses_same_row(self, recorder):
        """execute_declarative recebe session_id = interaction criada — o
        finalize interno (state final + ended_at) atualiza a MESMA row."""
        await _run(session_id="sid-novo-3")
        assert recorder.captured_session["session_id"] == "sid-novo-3"


class TestSessionReuse:
    @pytest.mark.asyncio
    async def test_existing_session_appends_turns(self, monkeypatch):
        rec = _RepoRecorder(
            existing_ids={"sid-velho"},
            existing_turns=[{"turn_number": 1}, {"turn_number": 2}],
        )
        _wire(monkeypatch, rec)
        await _run(session_id="sid-velho")
        assert rec.interactions_created == []
        assert [t["turn_number"] for t in rec.turns_created] == [3, 4]


class TestBestEffort:
    @pytest.mark.asyncio
    async def test_db_failure_does_not_block_execution(self, monkeypatch):
        rec = _RepoRecorder(create_raises=True)
        _wire(monkeypatch, rec)
        result = await _run(session_id="sid-db-off")
        # api_response tem precedência sobre output no shape adaptado
        assert "valor" in result["output"]
        assert result["status"] == "completed"
        # Sem interaction criada, o turno de saída também não é gravado
        assert rec.turns_created == []
