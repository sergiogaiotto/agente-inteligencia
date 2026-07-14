"""RED cego a falhas (35.14.3, achado de auditoria adversarial).

`record_invocation` (o único alimentador de maestro_invocations_total/errors)
só rodava no caminho de SUCESSO — dentro do recorder agendado APÓS
execute_pipeline retornar. Todos os handlers de exceção (409/500 no sync, o
stream_error, e 7 dos 8 error branches do worker async — só o timeout contava)
NÃO incrementavam nada: o dashboard RED e o alerta HighErrorRate ficavam CEGOS
a falhas. Fix: helpers síncronos in-memory nos handlers de exceção.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock



class TestSyncStreamFailures:
    def test_helper_registra_com_error_true(self, monkeypatch):
        import app.routes.pipelines as pl
        rec = MagicMock()
        monkeypatch.setattr("app.core.metrics.record_invocation", rec)
        pl._record_invoke_failure("invoke", "error")
        assert rec.call_args.kwargs == {"kind": "invoke", "status": "error",
                                        "duration_s": 0.0, "error": True}

    def test_helper_nunca_propaga(self, monkeypatch):
        import app.routes.pipelines as pl
        monkeypatch.setattr("app.core.metrics.record_invocation",
                            MagicMock(side_effect=RuntimeError("prometheus down")))
        pl._record_invoke_failure("invoke", "error")  # não levanta

    def test_sync_e_stream_chamam_nos_handlers(self):
        src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        assert '_record_invoke_failure("invoke", "rejected")' in src   # 409
        assert '_record_invoke_failure("invoke", "error")' in src      # 500
        assert '_record_invoke_failure("invoke_stream", "error")' in src


class TestAsyncWorkerFailures:
    def test_helper_do_worker(self, monkeypatch):
        import app.core.invoke_jobs as ij
        rec = MagicMock()
        monkeypatch.setattr("app.core.metrics.record_invocation", rec)
        ij._record_async_failure("timeout")
        assert rec.call_args.kwargs["kind"] == "invoke_async"
        assert rec.call_args.kwargs["error"] is True

    def test_todos_os_error_branches_contam(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        for status in ["payload_corrupt", "pipeline_not_invocable", "session_not_accessible",
                       "api_key_revoked", "cost_budget_exceeded", "timeout", "rejected", "error"]:
            assert f'_record_async_failure("{status}")' in src, f"branch {status} não conta"


class TestFalhaGeraMetrica:
    """Comportamental: um invoke que estoura ValueError incrementa o contador
    de erro (antes: silêncio)."""

    def test_sync_valueerror_gera_metrica_de_erro(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.core.auth import require_user
        from app.routes.pipelines import router
        from app.core import database as db

        monkeypatch.setattr("app.core.config.get_settings",
                            lambda: SimpleNamespace(api_key_invoke_published_only=False))
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            AsyncMock(return_value={"id": "p1", "status": "publicado", "name": "P"}))
        monkeypatch.setattr("app.catalog.pipeline_defs._build_subgraph",
                            AsyncMock(return_value={"root_agent_id": "a1", "nodes": [{"id": "a1"}]}))
        monkeypatch.setattr("app.agents.engine.execute_pipeline",
                            AsyncMock(side_effect=ValueError("sem raiz roteável")))
        monkeypatch.setattr("app.core.interaction_access.assert_can_access_interaction", AsyncMock())
        rec = MagicMock()
        monkeypatch.setattr("app.core.metrics.record_invocation", rec)

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user] = lambda: {"id": "u1", "role": "comum"}
        r = TestClient(app).post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 409
        # a métrica de erro foi registrada (antes: rec nunca chamado)
        assert rec.called
        assert rec.call_args.kwargs["error"] is True
        assert rec.call_args.kwargs["status"] == "rejected"
