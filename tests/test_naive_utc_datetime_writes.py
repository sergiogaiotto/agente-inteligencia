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
        r"\"(?:ended_at|started_at|created_at|updated_at)\":\s*datetime\.(?:now|utcnow)\(\)"
    )

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
