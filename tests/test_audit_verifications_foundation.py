"""Fundação de dados da Auditoria (PR3 do arco LLM-as-Judge, 24.10.0).

- `verifications` ganha dono (agent_id/pipeline_id), o par pergunta/resposta
  JULGADO (DLP-redacted) e o rastro do contract-retry;
- steps de PIPELINE com profile `rigorous` passam a rodar o verifier
  (decisão 2026-07-04: auditoria por step só onde o operador pediu rigor);
- snapshot da verification em cada item de pipeline_steps + re-apontamento
  das verifications das interactions filhas pro master (filhas são deletadas);
- workspace persiste `verification` no trace_data (painel sobrevive a reload).
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.engine import _verify_autopass
from app.verifier.runtime import VerificationResult, Verifier


# ─── Gate: steps rigorous de pipeline agora são julgados ────────────

class TestVerifyAutopass:
    def test_skip_evidence_sempre_autopassa(self):
        assert _verify_autopass(None, True, "rigorous", True) is True
        assert _verify_autopass("ctx upstream", True, "fast", True) is True

    def test_step_de_pipeline_standard_autopassa(self):
        assert _verify_autopass("ctx upstream", False, "standard", True) is True
        assert _verify_autopass("ctx upstream", False, "fast", True) is True

    def test_step_de_pipeline_rigorous_com_v2_NAO_autopassa(self):
        # decisão (b) 2026-07-04: julgar steps só no profile rigorous
        assert _verify_autopass("ctx upstream", False, "rigorous", True) is False

    def test_step_rigorous_com_v2_OFF_autopassa(self):
        """Finding HIGH da revisão 24.10.0: com v2 OFF (default), o step
        rigorous cairia nos ramos LEGACY — judge sem persistência + risco de
        Refuse no meio do pipeline, pagando custo SEM gerar auditoria.
        Comportamento 24.9.0 (auto-pass) preservado quando v2 está OFF."""
        assert _verify_autopass("ctx upstream", False, "rigorous", False) is True

    def test_fora_de_pipeline_nao_autopassa(self):
        assert _verify_autopass(None, False, "standard", True) is False
        assert _verify_autopass(None, False, "rigorous", False) is False
        assert _verify_autopass("", False, "standard", True) is False


# ─── Schema + migrações idempotentes ────────────────────────────────

class TestSchemaEMigracoes:
    def test_create_table_tem_colunas_novas(self):
        from app.core.database import SCHEMA
        for col in ("agent_id TEXT", "pipeline_id TEXT", "question_redacted TEXT",
                    "draft_redacted TEXT", "contract_retried BOOLEAN",
                    "contract_original_errors TEXT"):
            assert col in SCHEMA, f"SCHEMA sem coluna nova: {col}"

    def test_indices_novos_vivem_so_nas_migracoes(self):
        """Incidente 2026-07-04 (pego no smoke): CREATE INDEX das colunas
        novas dentro do SCHEMA roda ANTES dos ALTERs — em DB existente a
        coluna ainda não existe → UndefinedColumnError → boot crash. Os
        índices novos devem viver SÓ em _IDEMPOTENT_MIGRATIONS."""
        from app.core.database import SCHEMA
        # o SCHEMA não pode indexar colunas que só existem via migração
        schema_verifications = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS verifications"):]
        schema_verifications = schema_verifications[:schema_verifications.index("-- ═")]
        assert "CREATE INDEX IF NOT EXISTS idx_verifications_agent" not in schema_verifications
        assert "CREATE INDEX IF NOT EXISTS idx_verifications_pipeline" not in schema_verifications

    def test_migracoes_idempotentes_para_dbs_existentes(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        for col in ("agent_id", "pipeline_id", "question_redacted",
                    "draft_redacted", "contract_retried", "contract_original_errors"):
            assert f"ALTER TABLE verifications ADD COLUMN IF NOT EXISTS {col}" in migs, (
                f"migração faltando p/ verifications.{col}"
            )
        assert "idx_verifications_agent" in migs
        assert "idx_verifications_pipeline" in migs


# ─── _persist grava dono + par pergunta/resposta DLP-redacted ───────

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


class TestPersistAuditFields:
    @pytest.mark.asyncio
    async def test_persist_grava_dono_e_par_redacted(self, monkeypatch):
        sink: dict = {}
        monkeypatch.setattr(
            "app.core.database._get_pool", lambda: _FakePool(sink)
        )
        result = VerificationResult(
            ok=True, confidence=0.8,
            dimensions={"factuality": {"score": 4, "reason": "ok"}},
            contract_retried=True,
            contract_original_errors=["campo x ausente"],
            judge_model="gpt-4o",
        )
        await Verifier()._persist(
            result, None, "int-1", "rigorous",
            agent_id="ag-1", pipeline_id="pl-1",
            user_question="CPF do cliente é 123.456.789-09, qual o limite?",
            draft="O limite do CPF 123.456.789-09 é R$ 5.000.",
        )
        sql = sink["sql"]
        params = sink["params"]
        assert "agent_id" in sql and "pipeline_id" in sql
        assert "question_redacted" in sql and "draft_redacted" in sql
        assert "contract_retried" in sql and "contract_original_errors" in sql
        # ordem: id, turn_id, interaction_id, agent_id, pipeline_id, q, draft...
        assert params[2] == "int-1"
        assert params[3] == "ag-1"
        assert params[4] == "pl-1"
        # DLP: CPF nunca chega cru na tabela de auditoria
        assert "[CPF]" in params[5] and "123.456.789-09" not in params[5]
        assert "[CPF]" in params[6] and "123.456.789-09" not in params[6]
        # rastro do contract-retry persistido
        assert True in params  # contract_retried
        assert any(isinstance(p, str) and "campo x ausente" in p for p in params)

    @pytest.mark.asyncio
    async def test_persist_sem_dono_grava_null(self, monkeypatch):
        sink: dict = {}
        monkeypatch.setattr(
            "app.core.database._get_pool", lambda: _FakePool(sink)
        )
        await Verifier()._persist(
            VerificationResult(ok=True, confidence=0.5), None, "int-2", "standard",
        )
        assert sink["params"][3] is None  # agent_id
        assert sink["params"][4] is None  # pipeline_id


# ─── Dispatcher assíncrono propaga o dono ───────────────────────────

class TestAsyncDispatcherPropagaDono:
    @pytest.mark.asyncio
    async def test_dispatch_propaga_agent_e_pipeline(self, monkeypatch):
        from app.verifier import async_dispatcher as ad
        import app.verifier as vpkg
        captured: dict = {}

        async def fake_verify(**kw):
            captured.update(kw)

        monkeypatch.setattr(vpkg.verifier, "verify", fake_verify)
        ok = ad.dispatch(
            draft="d", evidences=[], output_contract="", guardrails="",
            user_question="q", profile="rigorous", interaction_id="i1",
            max_concurrent=5, agent_id="ag-1", pipeline_id="pl-1",
        )
        assert ok is True
        await asyncio.sleep(0.05)  # deixa a task rodar
        assert captured.get("agent_id") == "ag-1"
        assert captured.get("pipeline_id") == "pl-1"


# ─── Rastro no engine + workspace (invariantes de fonte) ────────────

class TestEngineEWorkspaceInvariantes:
    def test_step_de_pipeline_carrega_snapshot_da_verification(self):
        from pathlib import Path
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert '"verification": result.get("verification")' in src

    def test_consolidacao_reaponta_verifications_pro_master(self):
        from pathlib import Path
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert "UPDATE verifications SET interaction_id = $1" in src

    def test_workspace_persiste_verification_no_trace_data(self):
        from pathlib import Path
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        # 35.3.0: o allowlist ganhou output_agent (autoria no reload da sessão)
        assert '"mode","verification","output_agent","decision"]' in src.replace("'", '"')  # decision: 36.1.0

    def test_ramo_async_exclui_steps_de_pipeline(self):
        """Finding MEDIUM da revisão: o judge async persistiria DEPOIS da
        consolidação (que re-aponta e deleta as filhas) → linha órfã. Step
        rigorous de pipeline deve cair no ramo SÍNCRONO.

        26.0.0 (fast-routing): o sinal virou `pipeline_step or bool(ctx)` —
        downstream com upstream pulado (pipeline_context vazio) TAMBÉM é step
        de pipeline e continua fora do async."""
        from pathlib import Path
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert (
            "verifier_production_async\n"
            "        and not (pipeline_step or bool(pipeline_context))"
        ) in src

    def test_pipeline_id_inferido_filtra_por_membership(self):
        """Finding LOW da revisão: run de mesh LIVRE pode percorrer agentes
        FORA do pipeline do entry — steps não-membros não podem ser
        atribuídos ao pipeline inferido."""
        from pathlib import Path
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert "_pipeline_members is None or agent_id in _pipeline_members" in src


# ─── Retenção do payload (cleanup) ──────────────────────────────────

class TestCleanupVerificationsPayload:
    @pytest.mark.asyncio
    async def test_role_comum_recebe_403(self):
        from fastapi import HTTPException
        from app.routes.dashboard import cleanup_verifications_payload
        with pytest.raises(HTTPException) as ei:
            await cleanup_verifications_payload(days=90, user={"role": "comum"})
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_root_limpa_payload_antigo(self, monkeypatch):
        sink: dict = {}
        monkeypatch.setattr(
            "app.core.database._get_pool", lambda: _FakePool(sink)
        )
        from app.routes.dashboard import cleanup_verifications_payload
        out = await cleanup_verifications_payload(days=30, user={"role": "root"})
        assert out["status"] == "ok"
        assert "question_redacted = NULL" in sink["sql"]
        assert "draft_redacted = NULL" in sink["sql"]
        assert sink["params"] == ("30",)

    @pytest.mark.asyncio
    async def test_days_clampado(self, monkeypatch):
        sink: dict = {}
        monkeypatch.setattr(
            "app.core.database._get_pool", lambda: _FakePool(sink)
        )
        from app.routes.dashboard import cleanup_verifications_payload
        out = await cleanup_verifications_payload(days=999999, user={"role": "admin"})
        assert out["days"] == 3650
