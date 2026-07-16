"""Gate de papel no backfill de descoberta (39.x — item 3 PR5).

O endpoint existia sem gate, mas sem NENHUMA superfície de UI — só por curl.
Ao ganhar o botão "Descobrir pendentes" em /mcp, virou uma operação de FROTA
ao alcance de qualquer autenticado: dispara egress com credencial decifrada em
cada conector HTTP e SPAWNA processo local em cada stdio, tudo em paralelo. O
controle irmão desta mesma feature (o toggle global MCP_PER_TOOL_ENABLED) já
exige root/admin em /settings.

Lição da casa (test_parameters_module): gate só no template é COSMÉTICO.

Estes testes NÃO fazem `dependency_overrides` do `require_role`: substituem só
o `require_user` (a autenticação), deixando a checagem de PAPEL rodar de
verdade. Contornar o gate para testar o gate provaria nada — e o override por
introspecção de internals do FastAPI passava local e quebrava no CI.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.dashboard as dash


@pytest.fixture
def client(monkeypatch):
    """Autentica como `role`; o require_role real decide o resto."""
    def _as(role):
        async def _fake_require_user(request):
            return {"id": "u1", "role": role, "status": "active"}
        # _dep resolve `require_user` dos globals de app.core.auth em tempo de
        # chamada — o patch alcança o gate sem desmontá-lo.
        monkeypatch.setattr("app.core.auth.require_user", _fake_require_user)
        app = FastAPI()
        app.include_router(dash.router)
        return TestClient(app, raise_server_exceptions=False)
    return _as


@pytest.fixture(autouse=True)
def backfill_spy(monkeypatch):
    """O backfill real spawna processo e faz egress — nunca num teste."""
    chamou = {"n": 0}

    async def _fake(tools_repo, force=False, **kw):
        chamou["n"] += 1
        return {"backfilled": 0, "skipped": 0, "failed": 0, "total": 0}
    monkeypatch.setattr("app.mcp.runtime.backfill_discovered_tools", _fake)
    monkeypatch.setattr("app.mcp.runtime.per_tool_enabled", lambda: False)
    return chamou


class TestGateReal:
    @pytest.mark.parametrize("role", ["root", "admin"])
    def test_root_e_admin_passam(self, client, backfill_spy, role):
        r = client(role).post("/api/v1/tools/backfill-discovered", json={})
        assert r.status_code == 200, r.text
        assert backfill_spy["n"] == 1

    @pytest.mark.parametrize("role", ["comum", "membro", ""])
    def test_papel_insuficiente_e_403_e_nao_executa(self, client, backfill_spy, role):
        """O ponto do gate: um autenticado qualquer não dispara operação de
        frota. E o 403 tem que vir ANTES de qualquer efeito colateral."""
        r = client(role).post("/api/v1/tools/backfill-discovered", json={})
        assert r.status_code == 403, r.text
        assert backfill_spy["n"] == 0, "backfill executou apesar do 403"

    def test_gate_esta_na_rota_nao_so_no_template(self):
        """Botão escondido no HTML não impede um POST direto — foi exatamente
        o bug do PUT /settings que a casa já corrigiu uma vez."""
        import inspect
        assert 'require_role("root", "admin")' in inspect.getsource(
            dash.backfill_mcp_discovered)


class TestUI:
    def test_botao_escondido_para_nao_admin(self):
        from pathlib import Path
        html = Path("app/templates/pages/tools.html").read_text(encoding="utf-8")
        i = html.find('data-testid="btn-backfill-discovered"')
        assert i > 0
        antes = html[max(0, i - 400):i]
        assert "{% if user_role in ['root', 'admin'] %}" in antes, (
            "sem gate no template o botão vira 403 morto para membro"
        )
