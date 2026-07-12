"""OBS-1 + OBS-2 — métricas Prometheus (/metrics) + probes /livez /readyz.

Os handlers vivem no app do módulo (com lifespan real) → chamados DIRETO para
evitar init_db no teste. /readyz é exercitado com um pool asyncpg fake.
"""

from __future__ import annotations

import asyncio


def test_route_paths_registered():
    import app.main as m
    paths = {getattr(r, "path", None) for r in m.app.routes}
    assert {"/livez", "/readyz", "/metrics"} <= paths


class TestLivez:
    def test_livez_sempre_vivo_sem_io(self):
        from app.main import livez
        assert asyncio.run(livez()) == {"status": "alive"}


class TestReadyz:
    def test_503_quando_pool_nao_inicializado(self, monkeypatch):
        import app.core.database as db
        from app.main import readyz

        monkeypatch.setattr(db, "_pool", None)
        resp = asyncio.run(readyz())
        assert resp.status_code == 503

    def test_200_quando_pool_acquirable(self, monkeypatch):
        import app.core.database as db
        from app.main import readyz

        class _Con:
            async def fetchval(self, q):
                return 1

        class _Acq:
            async def __aenter__(self):
                return _Con()
            async def __aexit__(self, *a):
                return False

        class _Pool:
            def acquire(self):
                return _Acq()

        monkeypatch.setattr(db, "_pool", _Pool())
        assert asyncio.run(readyz()) == {"status": "ready"}

    def test_503_quando_db_indisponivel(self, monkeypatch):
        import app.core.database as db
        from app.main import readyz

        class _Acq:
            async def __aenter__(self):
                raise RuntimeError("db down")
            async def __aexit__(self, *a):
                return False

        class _Pool:
            def acquire(self):
                return _Acq()

        monkeypatch.setattr(db, "_pool", _Pool())
        resp = asyncio.run(readyz())
        assert resp.status_code == 503


class TestMetrics:
    def test_record_e_render(self):
        from app.core.metrics import record_invocation, render_latest

        record_invocation(kind="invoke", status="completed", duration_s=1.5,
                          escalated=True, error=False)
        record_invocation(kind="invoke", status="failed", duration_s=0.2, error=True)
        # Recusa (OBS-4) — contador dedicado p/ o rate() do Grafana.
        record_invocation(kind="invoke", status="refused", duration_s=0.3, refused=True)
        payload, content_type = render_latest()
        text = payload.decode("utf-8")
        assert "maestro_invocations_total" in text
        assert "maestro_invocation_duration_seconds" in text
        assert "maestro_invocation_errors_total" in text
        assert "maestro_escalations_total" in text
        assert "maestro_refusals_total" in text
        assert "text/plain" in content_type  # formato de exposição Prometheus

    def test_refusal_incrementa_so_quando_refused(self):
        from app.core import metrics

        before = metrics.REFUSALS.labels(kind="invoke")._value.get()
        metrics.record_invocation(kind="invoke", status="completed", duration_s=0.1)
        assert metrics.REFUSALS.labels(kind="invoke")._value.get() == before  # sem refused
        metrics.record_invocation(kind="invoke", status="refused", duration_s=0.1, refused=True)
        assert metrics.REFUSALS.labels(kind="invoke")._value.get() == before + 1

    def test_endpoint_metrics_retorna_200_prometheus(self):
        from app.main import metrics

        resp = asyncio.run(metrics())
        assert resp.status_code == 200
        assert b"maestro_" in resp.body


class TestRateLimitExempt:
    def test_probes_e_metrics_isentos(self):
        # A dispatch do rate-limit isenta esses paths (batidos por
        # Docker/orquestrador/Prometheus, não contam contra o balde).
        import inspect
        from app.core import ratelimit

        src = inspect.getsource(ratelimit.RateLimitMiddleware.dispatch)
        assert "/livez" in src and "/readyz" in src and "/metrics" in src
