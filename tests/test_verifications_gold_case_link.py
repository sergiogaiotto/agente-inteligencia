"""Keystone 33.10.0 — elo harness ↔ produção (`verifications.gold_case_id`).

Liga cada verificação ao caso do Golden Dataset que a originou. Cobre:
- schema: coluna no DDL base (DB fresco/CI), índice SÓ no Alembic (não crash);
- `Verifier._persist` grava a coluna (e NULL quando ausente);
- `Verifier.verify` encaminha o param ao `_persist`;
- revisão Alembic 0002 (add coluna + índice, idempotente, chain 0001→0002);
- `_link_verification_to_gold_case` do harness (UPDATE por interaction_id);
- a rota de re-julgamento propaga o elo do row original.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.verifier.runtime import VerificationResult, Verifier


# ─── Fakes de pool (mesmo padrão do test_audit_verifications_foundation) ──

class _FakeCon:
    def __init__(self, sink: dict):
        self.sink = sink

    async def execute(self, sql, *params):
        self.sink["sql"] = sql
        self.sink["params"] = params

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, sink: dict):
        self.sink = sink

    def acquire(self):
        return _FakeCon(self.sink)


# ─── Schema: coluna no DDL base, índice só no Alembic ────────────────────

class TestSchema:
    def test_ddl_base_tem_gold_case_id(self):
        from app.core.database import SCHEMA
        assert "gold_case_id TEXT" in SCHEMA, "DDL base precisa da coluna p/ DB fresco/CI"

    def test_indice_do_gold_case_nao_vive_no_schema(self):
        """Em DB existente o SCHEMA roda ANTES do Alembic; a coluna ainda não
        existe → CREATE INDEX no SCHEMA seria boot crash (incidente 2026-07-04).
        O índice vive SÓ no Alembic 0002."""
        from app.core.database import SCHEMA
        # checa o STATEMENT (o comentário do DDL cita o nome do índice de propósito)
        assert "CREATE INDEX IF NOT EXISTS idx_verifications_gold_case" not in SCHEMA

    def test_gold_case_id_nao_esta_nas_migracoes_idempotentes(self):
        """Convenção pós-Alembic: migrações de DB EXISTENTE vão em revisão
        Alembic, não mais em _IDEMPOTENT_MIGRATIONS."""
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        assert "verifications ADD COLUMN IF NOT EXISTS gold_case_id" not in migs


# ─── _persist grava a coluna ─────────────────────────────────────────────

class TestPersistGoldCaseId:
    @pytest.mark.asyncio
    async def test_persist_grava_gold_case_id(self, monkeypatch):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await Verifier()._persist(
            VerificationResult(ok=True, confidence=0.7), None, "int-1", "rigorous",
            gold_case_id="gc-42",
        )
        assert "gold_case_id" in sink["sql"]
        assert "$30" in sink["sql"]           # 30ª coluna (última)
        assert sink["params"][29] == "gc-42"  # 0-based $30
        assert len(sink["params"]) == 30

    @pytest.mark.asyncio
    async def test_persist_sem_gold_case_grava_null(self, monkeypatch):
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await Verifier()._persist(
            VerificationResult(ok=True, confidence=0.5), None, "int-2", "standard",
        )
        assert sink["params"][29] is None


# ─── verify() encaminha o param ao _persist ──────────────────────────────

class TestVerifyEncaminha:
    def test_verify_aceita_e_encaminha_gold_case_id(self):
        """verify() aceita o param e o repassa na chamada de _persist.
        (o _persist funcional acima prova a ESCRITA; isto prova o REPASSE.)"""
        src = Path("app/verifier/runtime.py").read_text(encoding="utf-8")
        assert "gold_case_id: Optional[str] = None" in src   # param em verify E _persist
        assert "gold_case_id=gold_case_id," in src           # repasse na chamada de _persist


# ─── Revisão Alembic 0002 ────────────────────────────────────────────────

def _load_revision_0002():
    p = Path("alembic/versions/0002_verifications_gold_case_id.py")
    spec = importlib.util.spec_from_file_location("rev0002_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAlembicRevision:
    def test_chain_0001_para_0002(self):
        mod = _load_revision_0002()
        assert mod.revision == "0002_verifications_gold_case_id"
        assert mod.down_revision == "0001_baseline"

    def test_upgrade_adiciona_coluna_e_indice(self, monkeypatch):
        import alembic.op
        calls: list[str] = []
        monkeypatch.setattr(alembic.op, "execute", lambda sql: calls.append(sql))
        _load_revision_0002().upgrade()
        joined = "\n".join(calls)
        assert "ADD COLUMN IF NOT EXISTS gold_case_id" in joined
        assert "CREATE INDEX IF NOT EXISTS idx_verifications_gold_case" in joined

    def test_downgrade_remove_coluna_e_indice(self, monkeypatch):
        import alembic.op
        calls: list[str] = []
        monkeypatch.setattr(alembic.op, "execute", lambda sql: calls.append(sql))
        _load_revision_0002().downgrade()
        joined = "\n".join(calls)
        assert "DROP INDEX IF EXISTS idx_verifications_gold_case" in joined
        assert "DROP COLUMN IF EXISTS gold_case_id" in joined


# ─── Harness liga a verification ao gold case ────────────────────────────

class TestHarnessLink:
    @pytest.mark.asyncio
    async def test_link_emite_update_por_interaction(self, monkeypatch):
        from app.harness.evaluator import _link_verification_to_gold_case
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await _link_verification_to_gold_case("int-7", "gc-7")
        assert "UPDATE verifications SET gold_case_id" in sink["sql"]
        assert "gold_case_id IS NULL" in sink["sql"]  # não sobrescreve elo já gravado
        assert sink["params"] == ("gc-7", "int-7")

    @pytest.mark.asyncio
    async def test_link_noop_sem_ids(self, monkeypatch):
        from app.harness.evaluator import _link_verification_to_gold_case
        sink: dict = {}
        monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(sink))
        await _link_verification_to_gold_case(None, "gc-7")
        await _link_verification_to_gold_case("int-7", None)
        assert sink == {}  # nenhuma query emitida

    def test_run_evaluation_chama_o_link(self):
        src = Path("app/harness/evaluator.py").read_text(encoding="utf-8")
        assert '_link_verification_to_gold_case(result.get("interaction_id"), case["id"])' in src


# ─── Rejudge propaga o elo ───────────────────────────────────────────────

class TestRejudgePropaga:
    def test_rejudge_encaminha_gold_case_do_row(self):
        src = Path("app/routes/dashboard.py").read_text(encoding="utf-8")
        assert 'gold_case_id=dict(row).get("gold_case_id")' in src
