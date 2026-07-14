"""35.2.0 — fechamento do arco escopo por-key (#585) + posse IDOR (#581).

- Key ESCOPADA a pipelines não invoca agente avulso (gate em apikey_scope —
  comportamento coberto em test_apikey_scope.py; aqui a fiação).
- Carimbos de dono nos criadores de interaction que ficavam órfãos:
  branch declarativo do invoke de agente, slash-invoke de binding do
  workspace (_persist_invoke_turn), harness (run_evaluation) e executor de
  recipes/pipeline-entry do Catálogo.
- UI de escopo no painel de API keys (badges + modal + PATCH /scope).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


class TestCarimbosDePosse:
    def test_branch_declarativo_do_invoke_agente_carimba(self):
        src = Path("app/routes/agents.py").read_text(encoding="utf-8")
        # 3 stamps: pipe (775), LLM (841) e agora o declarativo
        assert src.count("await stamp_interaction_owner(") == 3

    def test_persist_invoke_turn_recebe_e_carimba_dono(self):
        src = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert "owner_user_id: str | None = None," in src
        assert 'await stamp_interaction_owner(sid, owner_user_id)' in src
        # os 3 callers do slash-invoke passam o dono
        assert src.count("owner_user_id=user.get(\"id\")") >= 3  # rota + 2 helpers
        assert src.count("owner_user_id=owner_user_id") >= 2    # dentro dos helpers

    def test_executor_recipes_e_pipeline_entry_carimbam(self):
        src = Path("app/catalog/executor.py").read_text(encoding="utf-8")
        assert 'stamp_interaction_owner(inv.get("interaction_id"), consumer_user_id)' in src
        assert 'stamp_interaction_owner(result.get("interaction_id"), consumer_user.get("id"))' in src

    def test_harness_carimba_quem_disparou(self):
        src = Path("app/harness/evaluator.py").read_text(encoding="utf-8")
        assert "owner_user_id: str | None = None," in src
        assert 'stamp_interaction_owner(result.get("interaction_id"), owner_user_id)' in src
        rota = Path("app/routes/dashboard.py").read_text(encoding="utf-8")
        assert 'owner_user_id=_caller.get("id")' in rota


class TestHarnessOwnerComportamento:
    @pytest.mark.asyncio
    async def test_stamp_por_caso_quando_owner_presente(self, monkeypatch):
        """Reusa a fiação de mocks do modo-agente: com owner_user_id, cada caso
        avaliado carimba a interaction criada."""
        from types import SimpleNamespace
        from app.harness import evaluator

        monkeypatch.setattr(evaluator, "get_settings", lambda: SimpleNamespace(
            harness_use_verifier=False, verifier_v2_enabled=False,
            ragas_ground_truth_enabled=False,
            harness_min_accuracy=0.0, harness_min_avg_factuality=0.0,
            harness_min_avg_completeness=0.0, harness_min_avg_tone=0.0,
            harness_max_safety_violation_rate=1.0, harness_min_contract_compliance=0.0,
            harness_max_hallucination_rate=1.0, harness_max_dim_regression_pct=100.0,
            harness_max_regression_pct=100.0,
        ))
        monkeypatch.setattr(evaluator, "releases_repo",
                            SimpleNamespace(find_by_id=AsyncMock(return_value={"id": "r1"})))
        monkeypatch.setattr(evaluator, "agents_repo",
                            SimpleNamespace(find_by_id=AsyncMock(return_value={"id": "a1", "name": "A"})))
        monkeypatch.setattr(evaluator, "gold_cases_repo", SimpleNamespace(
            find_all=AsyncMock(return_value=[{
                "id": "c1", "input_text": "oi", "expected_output": "",
                "expected_state": "", "case_type": "normal", "weight": 1.0,
            }])))
        created = {}
        monkeypatch.setattr(evaluator, "eval_runs_repo", SimpleNamespace(
            create=AsyncMock(side_effect=lambda d: created.update(d) or d),
            update=AsyncMock(), find_all=AsyncMock(return_value=[])))
        monkeypatch.setattr(evaluator, "execute_interaction", AsyncMock(return_value={
            "output": "resp", "final_state": "Done", "interaction_id": "i-77",
            "duration_ms": 1, "transitions": [], "trace": {}}))
        monkeypatch.setattr(evaluator, "_write_drift_events", AsyncMock(return_value=0))
        stamp = AsyncMock()
        monkeypatch.setattr("app.core.interaction_access.stamp_interaction_owner", stamp)

        out = await evaluator.run_evaluation("r1", agent_id="a1", owner_user_id="u-dono")
        assert out.get("status") != "invalid_target"
        stamp.assert_awaited_with("i-77", "u-dono")


class TestUiDeEscopo:
    def test_lista_de_keys_expoe_escopo(self):
        src = Path("app/routes/api_keys.py").read_text(encoding="utf-8")
        assert '"read_only": bool(r.get("read_only"))' in src
        assert "_parse_scope_list" in src

    def test_modal_e_botao_no_settings(self):
        html = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert 'data-testid="apikey-edit-scope"' in html
        assert 'data-testid="apikey-scope-save"' in html
        assert 'data-testid="apikey-scope-readonly"' in html
        assert "/scope'" in html and "api.patch(" in html
        assert "scopeModal" in html and "scopePipelines" in html

    def test_api_helper_ganhou_patch(self):
        base = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")
        assert "async patch(url, body)" in base
