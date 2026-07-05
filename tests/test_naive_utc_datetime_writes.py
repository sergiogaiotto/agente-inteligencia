"""Higiene de datetime em writes de colunas TIMESTAMP (naive).

Regressão do incidente "started_at > ended_at" no /history: started_at vinha do
DEFAULT now() do Postgres (UTC) e ended_at de datetime.now() do app (hora LOCAL
do container, UTC-3) — interações terminavam "3 horas antes" de começar. E do
500 em GET /agents/{id}/stats: cutoff tz-aware bindado em coluna TIMESTAMP
(asyncpg: "can't subtract offset-naive and offset-aware datetimes").

Convenção única: app.core.datetime_utils.naive_utc_now() em TODO write de
coluna TIMESTAMP. O teste de convenção varre app/ para impedir reincidência
(mesmo espírito do teste dos helpers tz* de exibição).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.datetime_utils import naive_utc_now


class TestNaiveUtcNow:
    def test_returns_naive(self):
        assert naive_utc_now().tzinfo is None

    def test_matches_utc_instant(self):
        """Preserva o instante UTC (não a hora local) — em máquinas fora de UTC
        a diferença para a hora local seria de horas inteiras."""
        got = naive_utc_now()
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
        assert abs((ref - got).total_seconds()) < 60


class TestWritesUseNaiveUtc:
    """Captura o dict passado aos repositórios e valida o ended_at."""

    def _assert_utc_naive(self, dt: datetime):
        assert isinstance(dt, datetime)
        assert dt.tzinfo is None, "ended_at deve ser naive (coluna TIMESTAMP)"
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
        # Tolerância curta: pega escrita em hora LOCAL quando a máquina do CI
        # não está em UTC (skew de fuso = horas inteiras).
        assert abs((ref - dt).total_seconds()) < 60, (
            f"ended_at {dt.isoformat()} não é o instante UTC corrente "
            f"({ref.isoformat()}) — gravou hora local?"
        )

    @pytest.mark.asyncio
    async def test_fsm_log_and_close_ended_at(self, monkeypatch):
        from app.agents.state_machine import InteractionStateMachine, InteractionContext

        captured: dict = {}

        async def fake_update(_id, changes):
            captured.update(changes)

        async def fake_create(_row):
            return None

        monkeypatch.setattr("app.agents.state_machine.interactions_repo.update", fake_update)
        monkeypatch.setattr("app.agents.state_machine.turns_repo.create", fake_create)

        ctx = InteractionContext()
        ctx.interaction_id = "it-test-tz"
        ctx.final_output = "ok"
        fsm = InteractionStateMachine(ctx)
        await fsm.run_log_and_close()

        assert "ended_at" in captured
        self._assert_utc_naive(captured["ended_at"])

    @pytest.mark.asyncio
    async def test_fsm_transition_timestamp_is_utc(self):
        """O timestamp do transition_log (que vira result['transitions'] e
        pipeline_steps[].transitions[].timestamp) deve ser UTC — antes usava
        time.strftime() sem gmtime = hora LOCAL (BRT), divergindo do ended_at
        UTC e produzindo "terminou antes de começar" no dashboard."""
        from app.agents.state_machine import (
            InteractionStateMachine,
            InteractionContext,
            State,
        )

        # interaction_id None → transition NÃO persiste (pula repos), só popula
        # o transition_log em memória, que é o que a UI consome.
        ctx = InteractionContext()
        ctx.current_state = State.INTAKE
        fsm = InteractionStateMachine(ctx)
        await fsm.transition(State.POLICY_CHECK)  # 1ª transição válida (§15)

        assert ctx.transition_log, "transition_log deveria ter 1 entrada"
        ts = ctx.transition_log[-1]["timestamp"]
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        self._assert_utc_naive(parsed)

    @pytest.mark.asyncio
    async def test_declarative_finalize_ended_at(self, monkeypatch):
        from app.agents import declarative_engine

        captured: dict = {}

        async def fake_update(_id, changes):
            captured.update(changes)

        monkeypatch.setattr(
            "app.agents.declarative_engine.interactions_repo.update", fake_update
        )
        await declarative_engine._finalize_declarative_interaction("it-x", "completed")

        assert "ended_at" in captured
        self._assert_utc_naive(captured["ended_at"])


class TestConventionNoLocalNowInWrites:
    """Nenhum write de timestamp com datetime.now()/utcnow() em app/ —
    reincidência deve usar naive_utc_now()."""

    FORBIDDEN = re.compile(
        r"\"(?:ended_at|started_at|created_at|updated_at|last_updated|timestamp)\":"
        r"\s*datetime\.(?:now|utcnow)\(\)"
    )

    # time.strftime(fmt) SEM 2º argumento usa time.localtime() implícito = hora
    # LOCAL do container. Para persistir, use naive_utc_now().strftime(...) ou,
    # se precisar de time, time.strftime(fmt, time.gmtime()). Este padrão só casa
    # a forma de-um-argumento (a de dois args, com gmtime, tem vírgula e escapa).
    LOCAL_STRFTIME = re.compile(r"time\.strftime\(\s*[\"'][^\"']*[\"']\s*\)")

    def test_no_forbidden_timestamp_writes(self):
        offenders = []
        for path in Path("app").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in self.FORBIDDEN.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path}:{line} → {m.group(0)}")
        assert not offenders, (
            "Writes de TIMESTAMP com datetime.now()/utcnow() — use "
            "app.core.datetime_utils.naive_utc_now():\n" + "\n".join(offenders)
        )

    def test_no_local_strftime_without_gmtime(self):
        """time.strftime(fmt) sem gmtime grava hora LOCAL — regressão do
        transition_log em BRT (state_machine.py). Use naive_utc_now().strftime()
        ou time.strftime(fmt, time.gmtime())."""
        offenders = []
        for path in Path("app").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in self.LOCAL_STRFTIME.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path}:{line} → {m.group(0)}")
        assert not offenders, (
            "time.strftime(fmt) sem 2º argumento grava hora LOCAL — use "
            "naive_utc_now().strftime(...) ou time.strftime(fmt, time.gmtime()):\n"
            + "\n".join(offenders)
        )
