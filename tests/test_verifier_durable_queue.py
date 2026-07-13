"""Onda 6 — fila de juiz DURÁVEL (verifier_jobs, 33.16.0).

A fila do juiz async era só em memória (perdia no restart, sem retry/dead-letter).
Agora cada dispatch persiste um verifier_jobs (running→done), drops viram
'pending', e o boot-resume re-despacha pending/órfãos. Cobre o ciclo com pool
mockado (sem DB real) + verify mockado (sem LLM).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import app.verifier as vpkg
from app.verifier import async_dispatcher as ad


class _FakeCon:
    def __init__(self, sink, fetch_rows):
        self.sink = sink
        self._rows = fetch_rows

    async def execute(self, sql, *params):
        self.sink.setdefault("execs", []).append((sql, params))

    async def fetch(self, sql, *params):
        self.sink.setdefault("fetches", []).append((sql, params))
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, sink, fetch_rows=None):
        self.sink = sink
        self._rows = fetch_rows or []

    def acquire(self):
        return _FakeCon(self.sink, self._rows)


@pytest.fixture
def clean():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _execs(sink):
    return [s for s, _ in sink.get("execs", [])]


class TestEvidences:
    def test_roundtrip_objeto(self):
        from types import SimpleNamespace
        evs = [SimpleNamespace(relevance_score=0.8, source_name="S", snippet_text="txt")]
        ser = ad._serialize_evidences(evs)
        assert ser == [{"relevance_score": 0.8, "source_name": "S", "snippet_text": "txt"}]
        de = ad._deserialize_evidences(ser)
        assert de[0].relevance_score == 0.8 and de[0].snippet_text == "txt"

    def test_aceita_dict(self):
        ser = ad._serialize_evidences([{"relevance_score": 0.5, "source_name": "x", "snippet_text": "y"}])
        assert ser[0]["relevance_score"] == 0.5


class TestRunVerificationDurable:
    @pytest.mark.asyncio
    async def test_sucesso_running_e_done(self, monkeypatch, clean):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        called: dict = {}

        async def fake_verify(**kw):
            called.update(kw)

        monkeypatch.setattr(vpkg.verifier, "verify", fake_verify)
        await ad._run_verification(
            job_id="j1", draft="d", evidences=[], output_contract="", guardrails="",
            user_question="q", profile="standard", interaction_id="i1",
            agent_id="a1", pipeline_id="p1",
        )
        execs = _execs(sink)
        assert any("INSERT INTO verifier_jobs" in s and "'running'" in s for s in execs)
        assert any("status='done'" in s for s in execs)
        assert called.get("interaction_id") == "i1" and called.get("persist") is True

    @pytest.mark.asyncio
    async def test_falha_marca_pending_ou_dead(self, monkeypatch, clean):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))

        async def boom(**kw):
            raise RuntimeError("judge down")

        monkeypatch.setattr(vpkg.verifier, "verify", boom)
        with pytest.raises(RuntimeError):
            await ad._run_verification(
                job_id="j2", draft="d", evidences=[], output_contract="", guardrails="",
                user_question="q", profile="standard", interaction_id="i2",
            )
        # dead se attempts>=max, senão pending — decisão atômica no Postgres
        assert any("CASE WHEN attempts >= $2 THEN 'dead' ELSE 'pending'" in s for s in _execs(sink))
        # NÃO marcou done
        assert not any("status='done'" in s for s in _execs(sink))


class TestDispatch:
    @pytest.mark.asyncio
    async def test_sob_cap_cria_task(self, monkeypatch, clean):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))

        async def fake_verify(**kw):
            pass

        monkeypatch.setattr(vpkg.verifier, "verify", fake_verify)
        ok = ad.dispatch(
            draft="d", evidences=[], output_contract="", guardrails="", user_question="q",
            profile="standard", interaction_id="i3", max_concurrent=5,
        )
        assert ok is True
        assert ad.stats_snapshot()["sampled"] == 1
        await asyncio.sleep(0.05)  # deixa a task rodar (running→done)
        assert any("status='done'" in s for s in _execs(sink))

    @pytest.mark.asyncio
    async def test_acima_do_cap_persiste_pending_nao_perde(self, monkeypatch, clean):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        # força o cap com uma task ocupando o slot
        ad._pending_tasks.add(asyncio.create_task(asyncio.sleep(0.15)))
        ok = ad.dispatch(
            draft="d", evidences=[], output_contract="", guardrails="", user_question="q",
            profile="standard", interaction_id="i4", max_concurrent=1,
        )
        assert ok is False
        assert ad.stats_snapshot()["dropped"] == 1
        await asyncio.sleep(0.05)  # deixa a task de persist-pending rodar
        assert any("VALUES ($1,$2,$3,$4,$5,'pending')" in s for s in _execs(sink))


class TestResume:
    @pytest.mark.asyncio
    async def test_reseta_running_e_redispatcha_pending(self, monkeypatch, clean):
        payload = json.dumps({"draft": "d", "evidences": [], "profile": "standard", "interaction_id": "ir"})
        sink: dict = {}
        rows = [{"id": "jr", "payload": payload}]
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink, fetch_rows=rows))

        async def fake_verify(**kw):
            pass

        monkeypatch.setattr(vpkg.verifier, "verify", fake_verify)
        n = await ad.resume_jobs(batch=10)
        assert n == 1
        # reset dos órfãos 'running' → 'pending'
        assert any("status='pending'" in s and "WHERE status='running'" in s for s in _execs(sink))
        await asyncio.sleep(0.05)  # a task re-despachada roda (running→done)
        assert any("status='done'" in s for s in _execs(sink))

    @pytest.mark.asyncio
    async def test_sem_pending_zero(self, monkeypatch, clean):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink, fetch_rows=[]))
        n = await ad.resume_jobs(batch=10)
        assert n == 0


class TestSchemaConfigWiring:
    def test_schema_tem_verifier_jobs(self):
        from app.core.database import SCHEMA
        assert "CREATE TABLE IF NOT EXISTS verifier_jobs" in SCHEMA
        assert "idx_verifier_jobs_status" in SCHEMA

    def test_config_max_attempts_default_3(self):
        from app.core.config import Settings
        assert Settings.model_fields["verifier_job_max_attempts"].default == 3

    def test_lifespan_chama_resume(self):
        src = Path("app/main.py").read_text(encoding="utf-8")
        assert "resume_jobs(batch=" in src
