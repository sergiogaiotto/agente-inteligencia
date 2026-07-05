"""Postura de auditoria por pipeline (PR-3 do tuning, 26.1.0).

4 opções escolhíveis por pipeline: inherit|sync|async|disabled. `inherit`
preserva o gate atual byte-a-byte; as outras 3 sobrescrevem a decisão do
verifier no `execute_interaction`. O async grava a verification NO MASTER
p/ não virar linha órfã na consolidação.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ─── schema Pydantic ────────────────────────────────────────────────

class TestAuditPostureSchema:
    def test_create_default_inherit(self):
        from app.models.schemas import PipelineCreate
        assert PipelineCreate(name="x").audit_posture == "inherit"

    @pytest.mark.parametrize("val", ["inherit", "sync", "async", "disabled"])
    def test_create_aceita_valores_validos(self, val):
        from app.models.schemas import PipelineCreate
        assert PipelineCreate(name="x", audit_posture=val).audit_posture == val

    def test_create_rejeita_valor_invalido(self):
        from app.models.schemas import PipelineCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PipelineCreate(name="x", audit_posture="banana")

    def test_update_default_none_e_valida_pattern(self):
        from app.models.schemas import PipelineUpdate
        from pydantic import ValidationError
        assert PipelineUpdate().audit_posture is None
        assert PipelineUpdate(audit_posture="sync").audit_posture == "sync"
        with pytest.raises(ValidationError):
            PipelineUpdate(audit_posture="x")


# ─── serialize / migração ───────────────────────────────────────────

class TestAuditPostureWiring:
    def test_serialize_expoe_e_default_inherit(self):
        from app.routes.pipelines import _serialize
        s = _serialize({"id": "p1", "name": "P", "audit_posture": "sync"}, [])
        assert s["audit_posture"] == "sync"
        # linha antiga sem a coluna → default inherit
        s2 = _serialize({"id": "p2", "name": "P"}, [])
        assert s2["audit_posture"] == "inherit"

    def test_migracao_e_schema(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS, SCHEMA
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        assert "ADD COLUMN IF NOT EXISTS audit_posture" in migs
        assert "audit_posture TEXT DEFAULT 'inherit'" in SCHEMA
        assert "CHECK (audit_posture IN ('inherit','sync','async','disabled'))" in SCHEMA


# ─── wiring do gate no engine (asserção de fonte) ───────────────────

class TestAuditPostureGate:
    """O gate do verifier é inline no execute_interaction (crítico). Asserções
    de fonte garantem que as 3 posturas estão ligadas com as condições certas e
    que o `inherit` cai no cascade legado (intacto)."""

    def _src(self):
        return Path("app/agents/engine.py").read_text(encoding="utf-8")

    def test_param_na_assinatura(self):
        src = self._src()
        assert "audit_posture: str = \"inherit\"" in src
        assert "master_interaction_id: str | None = None" in src

    def test_branch_disabled_autopassa(self):
        src = self._src()
        assert 'elif audit_posture == "disabled" and not _rigorous_locked:' in src

    def test_branch_sync_roda_verifier_v2(self):
        src = self._src()
        assert 'elif audit_posture == "sync" and _pg_settings.verifier_v2_enabled:' in src

    def test_branch_async_grava_no_master(self):
        src = self._src()
        assert 'elif audit_posture == "async" and _pg_settings.verifier_v2_enabled and not _rigorous_locked:' in src
        # evita órfã: grava no master (ou no próprio id do entry, que vira master)
        assert "_audit_iid = master_interaction_id or ctx.interaction_id" in src

    def test_execute_pipeline_resolve_e_repassa(self):
        src = self._src()
        assert "_audit_posture = \"inherit\"" in src
        assert "audit_posture=_audit_posture" in src
        assert "master_interaction_id=master_interaction_id" in src

    def test_inherit_nao_quebra_cascade_legado(self):
        # o ramo _verify_autopass (inherit) segue existindo APÓS as posturas
        src = self._src()
        i_disabled = src.find('elif audit_posture == "disabled"')
        i_autopass = src.find("elif _verify_autopass(")
        assert 0 < i_disabled < i_autopass  # posturas ANTES do cascade legado

    def test_rigorous_nao_pode_ser_rebaixado(self):
        # rigorous+v2 é auditado SÍNCRONO independentemente da postura: `disabled`
        # e `async` NÃO se aplicam (removeriam / mandariam pro background o rigor).
        src = self._src()
        assert '_rigorous_locked = (exec_profile == "rigorous" and _pg_settings.verifier_v2_enabled)' in src
        assert 'elif audit_posture == "disabled" and not _rigorous_locked:' in src
        assert 'elif audit_posture == "async" and _pg_settings.verifier_v2_enabled and not _rigorous_locked:' in src
        # `sync` NÃO tem o lock de propósito (sync já é auditoria síncrona segura)
        assert 'elif audit_posture == "sync" and _pg_settings.verifier_v2_enabled:' in src

    def test_lock_rigorous_respeita_escape_hatch_skip_evidence(self):
        """O lock de rigorous protege o caso NORMAL (require_evidence on) — que
        cai no cascade e é auditado. Mas `skip_evidence` (require_evidence off /
        profile fast) é um escape hatch PRÉ-EXISTENTE e DOCUMENTADO de
        `_verify_autopass` (auto-passa sempre). Este teste fixa o contrato: o
        PR-3 NÃO altera esse comportamento global do verifier."""
        import app.agents.engine as engine
        # rigorous SEM skip_evidence → auditado (lock efetivo no cascade)
        assert engine._verify_autopass(True, False, "rigorous", True) is False
        # rigorous COM skip_evidence → auto-passa (contrato pré-existente intacto)
        assert engine._verify_autopass(True, True, "rigorous", True) is True


# ─── UI ─────────────────────────────────────────────────────────────

class TestAuditPostureUI:
    def test_select_e_metodo_no_estudio(self):
        html = Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")
        assert "pipeline-audit-posture" in html
        assert "setAuditPosture" in html
        for val in ("inherit", "sync", "async", "disabled"):
            assert f'value="{val}"' in html
