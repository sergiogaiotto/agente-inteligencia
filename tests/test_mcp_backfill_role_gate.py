"""Gate de papel no backfill de descoberta (39.x — item 3 PR5).

O endpoint existia sem gate, mas sem NENHUMA superfície de UI — só por curl.
Ao ganhar o botão "Descobrir pendentes" em /mcp, ele virou uma operação de
FROTA ao alcance de qualquer autenticado: dispara egress com credencial
decifrada em cada conector HTTP e SPAWNA processo local em cada stdio, tudo em
paralelo. O controle irmão desta mesma feature (o toggle global
MCP_PER_TOOL_ENABLED) já exige root/admin em /settings.

Lição da casa (test_parameters_module): gate só no template é COSMÉTICO.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.dashboard as dash


def _client(user):
    app = FastAPI()
    app.include_router(dash.router)
    # require_role é factory: cada rota tem a SUA dependência. O override
    # precisa alcançar a instância real usada pela rota do backfill.
    for route in app.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            call = getattr(dep, "call", None)
            if call and getattr(call, "__qualname__", "").startswith("require_role"):
                app.dependency_overrides[call] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_real_backfill(monkeypatch):
    """O backfill real spawna processo e faz egress — nunca num teste."""
    chamou = {"n": 0}

    async def _fake(tools_repo, force=False, **kw):
        chamou["n"] += 1
        return {"backfilled": 0, "skipped": 0, "failed": 0, "total": 0}
    monkeypatch.setattr("app.mcp.runtime.backfill_discovered_tools", _fake)
    return chamou


class TestGateReal:
    def test_rota_declara_require_role(self):
        """O gate tem que estar na ROTA, não só no template: o botão escondido
        no HTML não impede um POST direto."""
        import inspect
        sig = inspect.signature(dash.backfill_mcp_discovered)
        assert "user" in sig.parameters, "backfill sem dependência de papel"

    @pytest.mark.parametrize("role", ["root", "admin"])
    def test_root_e_admin_passam(self, role, _no_real_backfill):
        c = _client({"id": "u1", "role": role})
        r = c.post("/api/v1/tools/backfill-discovered", json={})
        assert r.status_code == 200, r.text
        assert _no_real_backfill["n"] == 1

    def test_dependencia_e_a_mesma_familia_do_settings(self):
        """Pina que o gate usado é o require_role da casa (root/admin), o mesmo
        que protege o toggle global desta feature."""
        import inspect
        src = inspect.getsource(dash.backfill_mcp_discovered)
        assert 'require_role("root", "admin")' in src


class TestUI:
    def test_botao_escondido_para_nao_admin(self):
        from pathlib import Path
        html = Path("app/templates/pages/tools.html").read_text(encoding="utf-8")
        i = html.find('data-testid="btn-backfill-discovered"')
        assert i > 0
        antes = html[max(0, i - 400):i]
        assert "{% if user_role in ['root', 'admin'] %}" in antes, (
            "botão sem gate no template vira 403 morto para membro"
        )
