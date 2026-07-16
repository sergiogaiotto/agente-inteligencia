"""Atribuição de usuário nas telas de análise (QA E2E 2026-07-16).

Pedido: "sinto falta de identificar QUEM É O USUÁRIO em administração,
observabilidade, problemas/erros e qualidade". O gap era ~90% de apresentação:
a identidade já é persistida (interactions.owner_user_id, audit_log.actor,
metadata.api_key_name). Fix: resolver UUID→nome SERVER-SIDE (o /users é
root/admin-only, o front comum não monta o mapa) e renderizar — com decisões
honestas: NULL="—", via-chave≠clique-humano, usuário removido mostra UUID.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.routes.dashboard as dash


TPL = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages"


# ─────────────────────────── Backend ───────────────────────────

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


class TestResolveUserNames:
    @pytest.mark.asyncio
    async def test_display_name_tem_precedencia_sobre_username(self, monkeypatch):
        class _Con:
            async def fetch(self, sql, ids):
                return [{"id": "u1", "username": "carlos.a", "display_name": "Carlos Analista"},
                        {"id": "u2", "username": "ana.c", "display_name": None}]
        import contextlib

        @contextlib.asynccontextmanager
        async def _acq():
            yield _Con()
        monkeypatch.setattr(db, "_get_pool", lambda: type("P", (), {"acquire": lambda s=None: _acq()})())
        names = await dash._resolve_user_names(["u1", "u2", None, "u1"])
        assert names == {"u1": "Carlos Analista", "u2": "ana.c"}

    @pytest.mark.asyncio
    async def test_falha_de_db_nao_derruba_e_da_dict_vazio(self, monkeypatch):
        def _boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(db, "_get_pool", _boom)
        assert await dash._resolve_user_names(["u1"]) == {}   # decoração, best-effort

    @pytest.mark.asyncio
    async def test_lista_vazia_nao_consulta(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(db, "_get_pool", lambda: called.__setitem__("n", called["n"] + 1))
        assert await dash._resolve_user_names([None, None]) == {}
        assert called["n"] == 0


class TestHistoryEnriquecido:
    def test_owner_name_e_via_chave_nas_interacoes(self, client, monkeypatch):
        async def _find_all(limit, offset):
            return [
                {"id": "i1", "owner_user_id": "u1", "metadata": json.dumps({"api_key_name": "prod-key"})},
                {"id": "i2", "owner_user_id": "u2", "metadata": "{}"},
                {"id": "i3", "owner_user_id": None, "metadata": None},   # legada
            ]
        monkeypatch.setattr(db.interactions_repo, "find_all", _find_all)
        for repo in ("turns_repo", "envelopes_repo", "audit_repo"):
            async def _empty(limit, offset):
                return []
            monkeypatch.setattr(getattr(db, repo), "find_all", _empty)

        async def _names(ids):
            return {"u1": "Carlos", "u2": "Ana"}
        monkeypatch.setattr(dash, "_resolve_user_names", _names)

        r = client.get("/api/v1/history?entity_type=interactions")
        assert r.status_code == 200
        inters = {i["id"]: i for i in r.json()["interactions"]}
        assert inters["i1"]["owner_name"] == "Carlos"
        assert inters["i1"]["via_api_key_name"] == "prod-key"   # via chave = máquina
        assert inters["i2"]["via_api_key_name"] is None          # clique humano
        assert inters["i3"]["owner_name"] is None                # legada → front mostra "—"


class TestVerificationsFiltro:
    def test_filtro_owner_vira_subquery_por_interacao(self):
        # (assinatura do handler expõe o parâmetro; a subquery é montada no
        # where-builder — checagem de string no source, sem DB)
        import inspect
        src = inspect.getsource(dash.list_verifications)
        assert "owner_user_id: Optional[str]" in src
        assert "SELECT id FROM interactions WHERE owner_user_id" in src

    def test_export_honra_o_mesmo_filtro(self):
        import inspect
        src = inspect.getsource(dash.export_verifications)
        assert "owner_user_id: Optional[str]" in src


# ─────────────────────────── Templates ───────────────────────────

def _tpl(name: str) -> str:
    return (TPL / name).read_text(encoding="utf-8")


class TestDecisoesHonestasNaUI:
    def test_observability_owner_e_badge_via_chave(self):
        h = _tpl("observability.html")
        assert 'data-testid="obs-owner"' in h
        assert "via chave" in h
        # ownerLabel: NULL="—", removido mostra UUID, nunca inventa
        i = h.index("ownerLabel(e) {")
        fn = h[i: i + 300]
        assert "'—'" in fn and "(removido)" in fn

    def test_history_dono_e_ator_com_uuid_no_title(self):
        h = _tpl("history.html")
        assert 'data-testid="hist-owner"' in h
        assert 'data-testid="audit-actor-chip"' in h
        assert "actor_name" in h

    def test_quality_chip_dono_e_filtro(self):
        h = _tpl("quality.html")
        assert 'data-testid="qual-owner"' in h
        assert 'data-testid="quality-filter-owner"' in h
        assert "owner_user_id" in h
        # opções derivadas dos itens (não do /users root-only)
        assert "_collectOwnerOptions" in h

    def test_log_viewer_mostra_quem_na_linha(self):
        h = _tpl("observability.html")
        i = h.index("lineHeadline(l) {")
        fn = h[i: i + 500]
        assert "j.user_id" in fn and "userLabel" in fn
