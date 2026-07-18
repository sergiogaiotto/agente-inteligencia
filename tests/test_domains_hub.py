"""Hub de Domínios (55.0.0): CRUD completo + Raio-X (inventário) + órfãos.

Lógica dos handlers testada com repos fake (in-memory), sem DB. O insert real
com as colunas novas vive em tests/integration/test_domains_hub_real_postgres.py
(mock não pega UndefinedColumn).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

import app.routes.users as U
from app.models.schemas import DomainCreate, DomainUpdate

_SETTINGS = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html"


class FakeRepo:
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]

    async def find_all(self, limit=100, offset=0, **filters):
        out = [r for r in self.rows if all(r.get(k) == v for k, v in filters.items())]
        return out[offset:offset + limit]

    async def count(self, **filters):
        return len([r for r in self.rows if all(r.get(k) == v for k, v in filters.items())])

    async def find_by_id(self, _id):
        return next((r for r in self.rows if r.get("id") == _id), None)

    async def create(self, row):
        self.rows.append(dict(row))
        return row.get("id")

    async def update(self, _id, patch):
        for r in self.rows:
            if r.get("id") == _id:
                r.update(patch)
                return True
        return False

    async def delete(self, _id):
        n = len(self.rows)
        self.rows = [r for r in self.rows if r.get("id") != _id]
        return len(self.rows) < n


@pytest.fixture
def fakes(monkeypatch):
    """Injeta repos fake + bypassa o RBAC nos handlers de users.py."""
    doms = FakeRepo([{"id": "d1", "name": "atendimento", "description": "", "status": "active"}])
    agents = FakeRepo([
        {"id": "a1", "name": "Maestro Bússola", "domain": "atendimento"},
        {"id": "a2", "name": "Especialista", "domain": "crédito"},  # órfão (não registrado)
    ])
    users = FakeRepo([
        {"id": "u1", "username": "ana", "display_name": "Ana", "domains": '["atendimento"]'},
        {"id": "u2", "username": "bob", "display_name": "Bob", "domains": '["outro"]'},
    ])
    empty = FakeRepo([])
    monkeypatch.setattr(U, "domains_repo", doms)
    monkeypatch.setattr(U, "users_repo", users)
    monkeypatch.setattr(U, "_DOMAIN_INV_REPOS", [
        ("agentes", agents), ("skills", empty), ("pipelines", empty),
        ("jornadas", empty), ("catalogo", empty), ("car", empty),
    ])
    monkeypatch.setattr(U, "_is_privileged", lambda c: True)

    async def _caller(_req):
        return {"role": "root"}
    monkeypatch.setattr(U, "_get_caller", _caller)
    return {"doms": doms, "agents": agents, "users": users}


class TestSchemas:
    def test_create_so_nome_obrigatorio(self):
        d = DomainCreate(name="x")
        assert d.name == "x" and d.status == "active"

    def test_create_aceita_campos_ricos(self):
        d = DomainCreate(name="x", description="d", owner_user_id="u1", color="#fff", status="archived")
        assert d.owner_user_id == "u1" and d.color == "#fff"

    def test_update_tudo_opcional(self):
        d = DomainUpdate()
        assert d.name is None and d.description is None


class TestCreate:
    @pytest.mark.asyncio
    async def test_cria_so_com_nome(self, fakes):
        r = await U.create_domain(DomainCreate(name="novo"), None)
        assert "id" in r
        assert any(x["name"] == "novo" and x["status"] == "active" for x in fakes["doms"].rows)

    @pytest.mark.asyncio
    async def test_nome_vazio_422(self, fakes):
        with pytest.raises(HTTPException) as e:
            await U.create_domain(DomainCreate(name="   "), None)
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_duplicado_409_case_insensitive(self, fakes):
        with pytest.raises(HTTPException) as e:
            await U.create_domain(DomainCreate(name="ATENDIMENTO"), None)
        assert e.value.status_code == 409


class TestUpdate:
    @pytest.mark.asyncio
    async def test_atualiza_campos(self, fakes):
        await U.update_domain("d1", DomainUpdate(description="agora tem", color="#123"), None)
        row = fakes["doms"].rows[0]
        assert row["description"] == "agora tem" and row["color"] == "#123"

    @pytest.mark.asyncio
    async def test_404_inexistente(self, fakes):
        with pytest.raises(HTTPException) as e:
            await U.update_domain("nope", DomainUpdate(description="x"), None)
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    async def test_nome_vazio_422(self, fakes):
        with pytest.raises(HTTPException) as e:
            await U.update_domain("d1", DomainUpdate(name="  "), None)
        assert e.value.status_code == 422


class TestInventory:
    @pytest.mark.asyncio
    async def test_raio_x_conta_e_amostra(self, fakes):
        r = await U.domain_inventory("d1")
        assert r["inventory"]["agentes"]["count"] == 1
        assert r["inventory"]["agentes"]["sample"][0]["name"] == "Maestro Bússola"
        assert r["inventory"]["membros"]["count"] == 1  # Ana
        assert r["total"] == 2

    @pytest.mark.asyncio
    async def test_404_inexistente(self, fakes):
        with pytest.raises(HTTPException) as e:
            await U.domain_inventory("nope")
        assert e.value.status_code == 404


class TestOrphans:
    @pytest.mark.asyncio
    async def test_detecta_nao_registrado(self, fakes):
        r = await U.domain_orphans()
        names = [o["name"] for o in r["orphans"]]
        assert "crédito" in names            # citado por agente, não registrado
        assert "atendimento" not in names    # registrado → não é órfão


class TestSampleLabel:
    def test_fallbacks(self):
        assert U._sample_label({"name": "N"}) == "N"
        assert U._sample_label({"skill_urn": "urn:x"}) == "urn:x"
        assert U._sample_label({}) == "—"


class TestMigration:
    def test_colunas_aditivas_registradas(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        blob = "\n".join(_IDEMPOTENT_MIGRATIONS)
        for col in ("owner_user_id", "color", "icon", "status"):
            assert f"ALTER TABLE domains ADD COLUMN IF NOT EXISTS {col}" in blob


class TestTemplate:
    @pytest.fixture(scope="class")
    def html(self):
        return _SETTINGS.read_text(encoding="utf-8")

    def test_aba_e_painel(self, html):
        assert 'data-testid="settings-tab-domains"' in html
        assert 'data-testid="settings-domains-tab"' in html
        assert "tab==='domains'" in html
        assert "loadDomainsHub()" in html

    def test_crud_afordancias(self, html):
        for t in ("domain-new", "domain-editor", "domain-name", "domain-save", "domain-dossier", "domain-orphans"):
            assert f'data-testid="{t}"' in html, t

    def test_metodos(self, html):
        for m in ("async loadDomainsHub()", "async selectDomain(", "async saveDomain()",
                  "async deleteDomain(", "async registerOrphan(", "get domainHealth()"):
            assert m in html, m

    def test_raio_x_e_orfaos_renderizados(self, html):
        assert "domainInvKeys" in html
        assert "registerOrphan(o.name)" in html
        assert "só o nome é obrigatório" in html.lower()
