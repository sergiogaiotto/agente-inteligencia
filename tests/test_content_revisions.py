"""Histórico de revisões + rollback (46.0.0, PR1 do arco Otimização).

Cobre:
1. Módulo puro-ish (app/core/revisions.py) com FakePool: dedup por hash,
   poda além de KEEP_LAST, backfill só na 1ª edição, allowlist de
   entity_type, best-effort (safe_* engolem exceção).
2. Hooks de rota: PUT /skills/{id} grava backfill do ANTIGO + snapshot do
   novo; PUT /agents/{id} idem para system_prompt (e NÃO grava quando o
   prompt não muda); rollback restaura como SAVE NOVO com source='rollback'
   e parent na revisão restaurada.
3. Endpoints de listagem: 404 de entidade e de revisão de OUTRA entidade.
4. Templates: painel presente nas duas páginas + componente no base.

Mocks nos módulos — sem DB real, convenção da suíte.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.revisions as revisions
import app.routes.agents as agents_routes
import app.routes.skills as skills_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ═══ 1. Módulo com FakePool ══════════════════════════════════════════════

class _FakeCon:
    def __init__(self, *, last_row=None, count=0):
        self.last_row = last_row
        self.count = count
        self.executed = []

    async def fetchrow(self, sql, *a):
        return self.last_row

    async def fetchval(self, sql, *a):
        return self.count

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return "OK"

    async def fetch(self, sql, *a):
        return []


class _FakePool:
    def __init__(self, con):
        self._con = con

    def acquire(self):
        con = self._con

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class TestModulo:
    @pytest.mark.asyncio
    async def test_record_insere_e_poda(self, monkeypatch):
        con = _FakeCon(last_row=None)
        monkeypatch.setattr(revisions, "_pool", lambda: _FakePool(con))
        rid = await revisions.record_revision(
            entity_type="skill", entity_id="s1", content="# Skill\ncorpo",
            version="1.0.1", source="update")
        assert rid and rid.startswith("rev_")
        sqls = [s for (s, _) in con.executed]
        assert any("INSERT INTO content_revisions" in s for s in sqls)
        assert any("DELETE FROM content_revisions" in s and "OFFSET" in s
                   for s in sqls)

    @pytest.mark.asyncio
    async def test_record_dedup_por_hash(self, monkeypatch):
        h = revisions.content_hash("mesmo conteúdo")
        con = _FakeCon(last_row={"id": "rev_x", "content_hash": h})
        monkeypatch.setattr(revisions, "_pool", lambda: _FakePool(con))
        rid = await revisions.record_revision(
            entity_type="skill", entity_id="s1", content="mesmo conteúdo")
        assert rid == "rev_x"
        assert con.executed == []  # nem INSERT nem poda

    @pytest.mark.asyncio
    async def test_record_valida_entity_type_e_conteudo_vazio(self, monkeypatch):
        monkeypatch.setattr(revisions, "_pool", lambda: _FakePool(_FakeCon()))
        with pytest.raises(ValueError):
            await revisions.record_revision(
                entity_type="hack", entity_id="x", content="c")
        assert await revisions.record_revision(
            entity_type="skill", entity_id="x", content="   ") is None

    @pytest.mark.asyncio
    async def test_backfill_so_na_primeira(self, monkeypatch):
        recorded = []

        async def _rec(**kw):
            recorded.append(kw)
            return "rev_b"

        monkeypatch.setattr(revisions, "record_revision", _rec)
        con = _FakeCon(count=0)
        monkeypatch.setattr(revisions, "_pool", lambda: _FakePool(con))
        await revisions.backfill_if_first(
            entity_type="skill", entity_id="s1", old_content="antigo",
            version="1.0.0")
        assert recorded and recorded[0]["source"] == "backfill"
        recorded.clear()
        con.count = 3  # já tem histórico → no-op
        await revisions.backfill_if_first(
            entity_type="skill", entity_id="s1", old_content="antigo")
        assert recorded == []

    @pytest.mark.asyncio
    async def test_safe_record_engole_excecao(self, monkeypatch):
        async def _boom(**kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(revisions, "record_revision", _boom)
        assert await revisions.safe_record(
            entity_type="skill", entity_id="s1", content="c") is None


# ═══ 2. Hooks de rota ════════════════════════════════════════════════════

_SKILL_MD = ("---\nid: urn:skill:x:y:1\nversion: 1.0.0\nkind: subagent\n"
             "owner: t\nstability: stable\n---\n# Skill: X\n## Purpose\np\n")


def _skills_client():
    app = FastAPI()
    app.include_router(skills_routes.router)
    return TestClient(app, raise_server_exceptions=False)


def _agents_client():
    app = FastAPI()
    app.include_router(agents_routes.router)
    return TestClient(app, raise_server_exceptions=False)


def _wire_rev(monkeypatch):
    """Recorders nos safe_* do módulo (os hooks importam o MÓDULO)."""
    calls = {"backfill": [], "record": []}

    async def _bf(**kw):
        calls["backfill"].append(kw)

    async def _rec(**kw):
        calls["record"].append(kw)
        return "rev_new"

    monkeypatch.setattr(revisions, "safe_backfill", _bf)
    monkeypatch.setattr(revisions, "safe_record", _rec)
    return calls


class TestHookSkills:
    def test_put_grava_backfill_e_snapshot(self, monkeypatch):
        calls = _wire_rev(monkeypatch)
        monkeypatch.setattr(skills_routes.skills_repo, "find_by_id", _async({
            "id": "s1", "version": "1.0.0", "raw_content": "conteúdo ANTIGO",
            "tags": "[]"}))
        monkeypatch.setattr(skills_routes.skills_repo, "update",
                            _async({"id": "s1", "version": "1.0.1"}))
        monkeypatch.setattr(skills_routes, "_warn_unknown_evidence_sources",
                            _async([]))
        r = _skills_client().put("/api/v1/skills/s1",
                                 json={"raw_content": _SKILL_MD})
        assert r.status_code == 200, r.text
        assert calls["backfill"][0]["old_content"] == "conteúdo ANTIGO"
        rec = calls["record"][0]
        assert rec["source"] == "update" and rec["entity_type"] == "skill"
        assert rec["version"] == "1.0.1"

    def test_rollback_restaura_como_save_novo(self, monkeypatch):
        calls = _wire_rev(monkeypatch)
        monkeypatch.setattr(skills_routes.skills_repo, "find_by_id", _async({
            "id": "s1", "version": "1.0.5", "raw_content": "atual",
            "tags": "[]"}))
        monkeypatch.setattr(skills_routes.skills_repo, "update",
                            _async({"id": "s1", "version": "1.0.6"}))
        monkeypatch.setattr(skills_routes, "_warn_unknown_evidence_sources",
                            _async([]))
        monkeypatch.setattr(revisions, "get_revision", _async({
            "id": "rev_old", "entity_type": "skill", "entity_id": "s1",
            "content": _SKILL_MD, "version": "1.0.2"}))
        r = _skills_client().post("/api/v1/skills/s1/revisions/rev_old/rollback")
        assert r.status_code == 200, r.text
        assert "restaurado" in r.json()["message"].lower()
        rec = calls["record"][0]
        assert rec["source"] == "rollback"
        assert rec["parent_revision_id"] == "rev_old"

    def test_rollback_404_revisao_de_outra_entidade(self, monkeypatch):
        _wire_rev(monkeypatch)
        monkeypatch.setattr(revisions, "get_revision", _async({
            "id": "rev_old", "entity_type": "skill", "entity_id": "OUTRA",
            "content": "x"}))
        r = _skills_client().post("/api/v1/skills/s1/revisions/rev_old/rollback")
        assert r.status_code == 404

    def test_list_404_skill_inexistente(self, monkeypatch):
        monkeypatch.setattr(skills_routes.skills_repo, "find_by_id", _async(None))
        assert _skills_client().get("/api/v1/skills/ghost/revisions").status_code == 404

    def test_create_grava_snapshot_inicial(self, monkeypatch):
        calls = _wire_rev(monkeypatch)
        monkeypatch.setattr(skills_routes.skills_repo, "create", _async(None))
        monkeypatch.setattr(skills_routes, "_warn_unknown_evidence_sources",
                            _async([]))
        r = _skills_client().post("/api/v1/skills",
                                  json={"raw_content": _SKILL_MD})
        assert r.status_code == 201, r.text
        assert calls["record"][0]["source"] == "create"


class TestHookAgents:
    def _wire_agent(self, monkeypatch, existing=None):
        monkeypatch.setattr(agents_routes.agents_repo, "find_by_id", _async(
            existing or {"id": "a1", "version": "1.0.0",
                         "system_prompt": "prompt ANTIGO"}))
        monkeypatch.setattr(agents_routes.agents_repo, "update",
                            _async({"id": "a1"}))
        monkeypatch.setattr(agents_routes.audit_repo, "create", _async(None))
        monkeypatch.setattr(
            "app.agents.preflight.run_preflight",
            _async(SimpleNamespace(blocked=False, model_dump=lambda: {})))

    def test_put_com_prompt_novo_grava_revisao(self, monkeypatch):
        calls = _wire_rev(monkeypatch)
        self._wire_agent(monkeypatch)
        r = _agents_client().put("/api/v1/agents/a1",
                                 json={"system_prompt": "prompt NOVO"})
        assert r.status_code == 200, r.text
        assert calls["backfill"][0]["old_content"] == "prompt ANTIGO"
        rec = calls["record"][0]
        assert rec["entity_type"] == "agent_system_prompt"
        assert rec["content"] == "prompt NOVO" and rec["source"] == "update"

    def test_put_sem_mudanca_de_prompt_nao_grava(self, monkeypatch):
        calls = _wire_rev(monkeypatch)
        self._wire_agent(monkeypatch)
        r = _agents_client().put("/api/v1/agents/a1",
                                 json={"system_prompt": "prompt ANTIGO",
                                       "description": "só a descrição"})
        assert r.status_code == 200, r.text
        assert calls["record"] == []

    def test_rollback_do_prompt(self, monkeypatch):
        calls = _wire_rev(monkeypatch)
        self._wire_agent(monkeypatch)
        monkeypatch.setattr(revisions, "get_revision", _async({
            "id": "rev_p", "entity_type": "agent_system_prompt",
            "entity_id": "a1", "content": "prompt de OURO",
            "version": "1.0.0"}))
        r = _agents_client().post(
            "/api/v1/agents/a1/prompt-revisions/rev_p/rollback")
        assert r.status_code == 200, r.text
        rec = calls["record"][0]
        assert rec["source"] == "rollback" and rec["content"] == "prompt de OURO"
        assert rec["parent_revision_id"] == "rev_p"


# ═══ 4. Templates ════════════════════════════════════════════════════════

def test_templates_tem_painel_de_revisoes():
    from pathlib import Path
    base = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")
    assert "function revisionsPanel(" in base
    for page in ("skill_form", "agent_form"):
        src = Path(f"app/templates/pages/{page}.html").read_text(encoding="utf-8")
        assert 'data-testid="revisions-panel"' in src
        assert 'data-testid="revision-rollback"' in src
