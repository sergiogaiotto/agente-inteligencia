"""Testes dos endpoints REST do catálogo.

Estratégia: mini FastAPI app só com o router do catálogo + dependency
override para `require_user` + monkeypatch dos métodos do repo. Cobre
plumbing HTTP, validação Pydantic e regras de autorização, sem precisar
de Postgres real. Persistência real é coberta no smoke test manual.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.core.database import (
    audit_repo,
    catalog_entries_repo,
    catalog_submissions_repo,
    users_repo,
)
from app.routes.catalog import router as catalog_router


# ─── Fixtures ─────────────────────────────────────────────────────


def make_app(user: dict) -> FastAPI:
    """Mini app com require_user mockado e o catalog router montado."""
    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[require_user] = lambda: user
    return app


def make_client(user: dict) -> TestClient:
    return TestClient(make_app(user))


def _bind_repo(monkeypatch, repo, store: dict):
    """Vincula os métodos CRUD de um Repository a um dict in-memory."""

    async def fake_create(data):
        store[data["id"]] = dict(data)
        return data

    async def fake_find_by_id(id_):
        return dict(store[id_]) if id_ in store else None

    async def fake_update(id_, data):
        if id_ not in store:
            return None
        store[id_].update(data)
        return dict(store[id_])

    async def fake_delete(id_):
        return store.pop(id_, None) is not None

    async def fake_find_all(limit=100, offset=0, **filters):
        rows = list(store.values())
        for k, v in filters.items():
            rows = [r for r in rows if r.get(k) == v]
        return rows[offset:offset + limit]

    async def fake_count(**filters):
        rows = list(store.values())
        for k, v in filters.items():
            rows = [r for r in rows if r.get(k) == v]
        return len(rows)

    monkeypatch.setattr(repo, "create", fake_create)
    monkeypatch.setattr(repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(repo, "update", fake_update)
    monkeypatch.setattr(repo, "delete", fake_delete)
    monkeypatch.setattr(repo, "find_all", fake_find_all)
    monkeypatch.setattr(repo, "count", fake_count)


@pytest.fixture
def fake_storage(monkeypatch):
    """Mock in-memory de todos os repos usados pelas rotas do catálogo.

    Disclosure usa get/upsert/delete (helpers em queries.py com PK=entry_id),
    não Repository genérico — mock direto no namespace de rotas.
    """
    entries: dict[str, dict] = {}
    submissions: dict[str, dict] = {}
    disclosures: dict[str, dict] = {}
    users: dict[str, dict] = {}
    audit_log: list[dict] = []

    _bind_repo(monkeypatch, catalog_entries_repo, entries)
    _bind_repo(monkeypatch, catalog_submissions_repo, submissions)
    _bind_repo(monkeypatch, users_repo, users)

    async def fake_get_disclosure(entry_id):
        return dict(disclosures[entry_id]) if entry_id in disclosures else None

    async def fake_upsert_disclosure(entry_id, payload):
        existing = disclosures.get(entry_id, {})
        existing.update({k: v for k, v in payload.items() if v is not None or k in existing})
        existing["entry_id"] = entry_id
        disclosures[entry_id] = existing
        return dict(existing)

    async def fake_delete_disclosure(entry_id):
        return disclosures.pop(entry_id, None) is not None

    monkeypatch.setattr("app.routes.catalog.get_disclosure", fake_get_disclosure)
    monkeypatch.setattr("app.routes.catalog.upsert_disclosure", fake_upsert_disclosure)
    monkeypatch.setattr("app.routes.catalog.delete_disclosure", fake_delete_disclosure)

    # External metadata (Onda 2)
    externals: dict[str, dict] = {}

    async def fake_get_external(entry_id):
        return dict(externals[entry_id]) if entry_id in externals else None

    async def fake_upsert_external(entry_id, payload):
        existing = externals.get(entry_id, {})
        # Mesma regra do real: vendor obrigatório na primeira escrita
        if not existing and not payload.get("vendor"):
            raise ValueError("vendor é obrigatório na criação de external_metadata")
        existing.update(payload)
        existing["entry_id"] = entry_id
        externals[entry_id] = existing
        return dict(existing)

    monkeypatch.setattr("app.routes.catalog.get_external_metadata", fake_get_external)
    monkeypatch.setattr("app.routes.catalog.upsert_external_metadata", fake_upsert_external)

    async def fake_audit_create(data):
        audit_log.append(dict(data))
        return data

    monkeypatch.setattr(audit_repo, "create", fake_audit_create)

    # Helpers especializados do catalog.queries que vão direto no pool —
    # mockam o INNER JOIN entry↔submission em memória usando os dicts acima.

    async def fake_list_submissions_for_review(*, status="pending", limit=50, offset=0):
        rows: list[dict] = []
        for s in submissions.values():
            if status and s.get("review_status") != status:
                continue
            eid = s.get("entry_id")
            if eid not in entries:  # INNER JOIN — exclui órfãs
                continue
            e = entries[eid]
            row = dict(s)
            row["entry"] = {
                "id": e.get("id"),
                "name": e.get("name"),
                "kind": e.get("kind"),
                "version": e.get("version"),
                "urn": e.get("urn"),
                "description": e.get("description"),
                "domain": e.get("domain"),
                "visibility": e.get("visibility"),
                "visibility_scope": e.get("visibility_scope"),
                "steward_team": e.get("steward_team"),
                "owner_user_id": e.get("owner_user_id"),
                "status": e.get("status"),
            }
            # LEFT JOIN com disclosure — None se entry não tem disclosure
            row["disclosure"] = dict(disclosures[eid]) if eid in disclosures else None
            # LEFT JOIN com users — None se submitter foi deletado
            sub_id = s.get("submitted_by")
            u = users.get(sub_id) if sub_id else None
            row["submitter"] = {
                "id": sub_id,
                "email": u.get("email") if u else None,
                "role": u.get("role") if u else None,
            } if u else None
            rows.append(row)
        rows.sort(
            key=lambda r: r.get("submitted_at") or "",
            reverse=True,
        )
        return rows[offset:offset + limit], len(rows)

    async def fake_cleanup_orphan_submissions():
        orphans = [
            sid for sid, s in submissions.items()
            if s.get("entry_id") not in entries
        ]
        for sid in orphans:
            submissions.pop(sid, None)
        return len(orphans)

    monkeypatch.setattr(
        "app.routes.catalog.list_submissions_for_review",
        fake_list_submissions_for_review,
    )
    monkeypatch.setattr(
        "app.routes.catalog.cleanup_orphan_submissions",
        fake_cleanup_orphan_submissions,
    )

    return {
        "entries": entries,
        "submissions": submissions,
        "disclosures": disclosures,
        "externals": externals,
        "users": users,
        "audit": audit_log,
    }


def _payload(**over):
    base = {
        "name": "Smoke Agent",
        "kind": "agent",
        "artifact_type": "agent",
        "artifact_id": "agent-123",
        "version": "1.0.0",
    }
    base.update(over)
    return base


# ─── POST /entries ────────────────────────────────────────────────


class TestCreate:
    def test_create_minimal_valid(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload())
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Smoke Agent"
        assert body["status"] == "draft"
        assert body["owner_user_id"] == "u1"
        assert body["urn"].startswith("urn:maestro:default:agent:smoke-agent:")
        # Tags e adapter_config voltam como tipos nativos
        assert body["tags"] == []
        assert body["adapter_config"] == {}

    def test_create_audited(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        c.post("/api/v1/catalog/entries", json=_payload())
        assert len(fake_storage["audit"]) == 1
        evt = fake_storage["audit"][0]
        assert evt["entity_type"] == "catalog_entry"
        assert evt["action"] == "created"
        assert evt["actor"] == "u1"

    def test_create_agent_requires_artifact_link(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload(artifact_type=None, artifact_id=None))
        assert r.status_code == 422
        assert "vínculo" in r.json()["detail"].lower()

    def test_create_external_platform_no_artifact_required(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post(
            "/api/v1/catalog/entries",
            json={"name": "ChatGPT", "kind": "external_platform"},
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "external_platform"

    def test_create_rejects_invalid_kind(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload(kind="bogus"))
        assert r.status_code == 422

    def test_create_rejects_non_semver(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries", json=_payload(version="bad"))
        assert r.status_code == 422

    def test_create_duplicate_urn_returns_409(self, fake_storage, monkeypatch):
        """Postgres unique violation → 409 com mensagem humana sugerindo bump
        de versão. Sem expor o jargão 'URN' no detail (UX consumer)."""
        c = make_client({"id": "u1", "role": "comum"})

        async def boom(data):
            raise Exception("duplicate key value violates unique constraint")

        monkeypatch.setattr(catalog_entries_repo, "create", boom)
        r = c.post("/api/v1/catalog/entries", json=_payload(name="Foo", version="1.0.0"))
        assert r.status_code == 409
        detail = r.json()["detail"]
        # Mensagem cita o que duplicou (nome + versão) e sugere ação (subir versão).
        assert "Foo" in detail
        assert "1.0.0" in detail
        assert "versão diferente" in detail.lower() or "versao diferente" in detail.lower()


# ─── GET /entries/{id} ────────────────────────────────────────────


class TestGetOne:
    def test_get_own_draft(self, fake_storage):
        # Cria via POST e depois GET
        c = make_client({"id": "u1", "role": "comum"})
        cr = c.post("/api/v1/catalog/entries", json=_payload())
        eid = cr.json()["id"]

        r = c.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200
        assert r.json()["id"] == eid

    def test_other_user_blocked_on_draft(self, fake_storage):
        # u1 cria
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        # u2 tenta acessar — 404 (não vaza existência)
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 404

    def test_root_sees_other_users_draft(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_other_user_sees_published_company(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post(
            "/api/v1/catalog/entries",
            json=_payload(visibility="company"),
        ).json()["id"]
        # Simula publicação direta no storage (em PR 3 haverá endpoint)
        fake_storage["entries"][eid]["status"] = "published"

        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_404_when_not_found(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries/nonexistent")
        assert r.status_code == 404


# ─── PUT /entries/{id} ────────────────────────────────────────────


class TestUpdate:
    def test_owner_updates_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]

        r = c.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "novo texto"},
        )
        assert r.status_code == 200
        assert r.json()["description"] == "novo texto"

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "hack"},
        )
        assert r.status_code == 403

    def test_root_can_update_others(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "root edit"},
        )
        assert r.status_code == 200

    def test_cant_update_non_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        fake_storage["entries"][eid]["status"] = "published"
        r = c.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"description": "after publish"},
        )
        assert r.status_code == 409

    def test_update_recalculates_urn_on_name_change(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        r = c.put(
            f"/api/v1/catalog/entries/{eid}",
            json={"name": "Outro Nome"},
        )
        assert r.status_code == 200
        assert "outro-nome" in r.json()["urn"]

    def test_update_404_when_missing(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.put(
            "/api/v1/catalog/entries/missing",
            json={"description": "x"},
        )
        assert r.status_code == 404


# ─── DELETE /entries/{id} ─────────────────────────────────────────


class TestDelete:
    def test_owner_deletes_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        r = c.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200
        assert eid not in fake_storage["entries"]

    def test_cant_delete_published(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        fake_storage["entries"][eid]["status"] = "published"
        r = c.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 409

    def test_can_delete_archived(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = c.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        fake_storage["entries"][eid]["status"] = "archived"
        r = c.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 403

    def test_root_deletes_others(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = c1.post("/api/v1/catalog/entries", json=_payload()).json()["id"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.delete(f"/api/v1/catalog/entries/{eid}")
        assert r.status_code == 200

    def test_delete_404_when_missing(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.delete("/api/v1/catalog/entries/missing")
        assert r.status_code == 404


# ─── GET /entries (list) ──────────────────────────────────────────


class TestList:
    """List delega para list_visible_entries (SQL); aqui só testamos
    o plumbing HTTP via mock dessa função."""

    def test_list_returns_pagination_envelope(self, monkeypatch):
        async def fake_list(user, **kwargs):
            return [{"id": "e1", "name": "X"}], 42

        monkeypatch.setattr(
            "app.routes.catalog.list_visible_entries",
            fake_list,
        )
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 42
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert len(body["entries"]) == 1

    def test_list_passes_filters(self, monkeypatch):
        captured = {}

        async def fake_list(user, **kwargs):
            captured.update(kwargs)
            return [], 0

        monkeypatch.setattr(
            "app.routes.catalog.list_visible_entries",
            fake_list,
        )
        c = make_client({"id": "u1", "role": "comum"})
        c.get("/api/v1/catalog/entries?kind=agent&status=published&domain=fiscal&limit=10&offset=20")
        assert captured["kind"] == "agent"
        assert captured["status"] == "published"
        assert captured["domain"] == "fiscal"
        assert captured["limit"] == 10
        assert captured["offset"] == 20

    def test_list_rejects_limit_too_high(self, monkeypatch):
        async def fake_list(user, **kwargs):
            return [], 0

        monkeypatch.setattr(
            "app.routes.catalog.list_visible_entries",
            fake_list,
        )
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/entries?limit=500")
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════
# Workflow: submit → decide → publish → deprecate
# ═════════════════════════════════════════════════════════════════


def _seed_owner(fake_storage, user_id: str, status: str = "active"):
    """Insere user no mock para que prechecks encontrem owner."""
    fake_storage["users"][user_id] = {"id": user_id, "status": status}


def _create_draft(client, fake_storage, owner_id: str) -> str:
    """Helper: cria entry draft e retorna id. Também garante user no storage."""
    _seed_owner(fake_storage, owner_id)
    r = client.post(
        "/api/v1/catalog/entries",
        json={**_payload(), "description": "descrição bem longa para passar prechecks"},
    )
    assert r.status_code == 201
    return r.json()["id"]


# ─── POST /entries/{id}/submit ────────────────────────────────────


class TestSubmit:
    def test_submit_transitions_to_submitted(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.post(f"/api/v1/catalog/entries/{eid}/submit", json={"notes": ""})
        assert r.status_code == 201
        body = r.json()
        assert body["entry_status"] == "submitted"
        assert "submission_id" in body
        assert "precheck_report" in body
        # Entry no storage reflete o novo status
        assert fake_storage["entries"][eid]["status"] == "submitted"

    def test_submit_creates_submission_row(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        subs = list(fake_storage["submissions"].values())
        assert len(subs) == 1
        assert subs[0]["entry_id"] == eid
        assert subs[0]["submitted_by"] == "u1"
        assert subs[0]["review_status"] == "pending"

    def test_submit_runs_prechecks_with_error_when_disclosure_missing(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        body = c.post(f"/api/v1/catalog/entries/{eid}/submit", json={}).json()
        # Disclosure ausente é ERROR a partir do PR 4 (CRUD entregue)
        assert body["precheck_report"]["errors_count"] >= 1
        assert body["precheck_report"]["passed"] is False
        # Submit ainda assim acontece — Root decide
        assert body["entry_status"] == "submitted"

    def test_submit_passes_prechecks_when_disclosure_declared(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        # Declara disclosure mínima antes de submeter
        c.put(
            f"/api/v1/catalog/entries/{eid}/capability",
            json={},  # CapabilityDisclosure tem defaults para tudo
        )
        body = c.post(f"/api/v1/catalog/entries/{eid}/submit", json={}).json()
        # Disclosure agora existe — precheck deste item passa
        report = body["precheck_report"]
        cap_check = next(c for c in report["checks"] if c["name"] == "capability_disclosure_present")
        assert cap_check["passed"]

    def test_submit_audits(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        actions = [a["action"] for a in fake_storage["audit"]]
        assert "submitted" in actions

    def test_submit_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        assert r.status_code == 403

    def test_submit_rejects_non_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "published"
        r = c.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        assert r.status_code == 409


# ─── POST /submissions/{id}/decide ────────────────────────────────


class TestDecide:
    def _submit_one(self, fake_storage) -> tuple[str, str]:
        """Cria entry, submete, retorna (entry_id, submission_id)."""
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        body = c.post(f"/api/v1/catalog/entries/{eid}/submit", json={}).json()
        return eid, body["submission_id"]

    def test_root_approves(self, fake_storage):
        eid, sid = self._submit_one(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "approved", "notes": "ok"},
        )
        assert r.status_code == 200
        assert r.json()["entry_status"] == "approved"
        assert fake_storage["entries"][eid]["status"] == "approved"
        assert fake_storage["submissions"][sid]["review_status"] == "approved"
        assert fake_storage["submissions"][sid]["reviewed_by"] == "root1"

    def test_root_rejects_returns_to_draft(self, fake_storage):
        eid, sid = self._submit_one(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "rejected", "notes": "no"},
        )
        assert r.status_code == 200
        assert r.json()["entry_status"] == "draft"

    def test_root_requests_changes_returns_to_draft(self, fake_storage):
        eid, sid = self._submit_one(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "changes_requested", "notes": "ajuste X"},
        )
        assert r.status_code == 200
        assert r.json()["entry_status"] == "draft"

    def test_non_root_forbidden(self, fake_storage):
        _, sid = self._submit_one(fake_storage)
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "approved"},
        )
        assert r.status_code == 403

    def test_cant_decide_already_decided(self, fake_storage):
        _, sid = self._submit_one(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        c_root.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "approved"},
        )
        # Segunda decisão deve falhar — review_status já não é pending
        r = c_root.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "rejected"},
        )
        assert r.status_code == 409

    def test_unknown_decision_rejected_by_pydantic(self, fake_storage):
        _, sid = self._submit_one(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.post(
            f"/api/v1/catalog/submissions/{sid}/decide",
            json={"decision": "maybe"},
        )
        assert r.status_code == 422


# ─── POST /entries/{id}/publish ───────────────────────────────────


class TestPublish:
    def test_owner_publishes_approved(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "approved"
        r = c.post(f"/api/v1/catalog/entries/{eid}/publish")
        assert r.status_code == 200
        assert fake_storage["entries"][eid]["status"] == "published"

    def test_cant_publish_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        # status é draft
        r = c.post(f"/api/v1/catalog/entries/{eid}/publish")
        assert r.status_code == 409

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "approved"
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.post(f"/api/v1/catalog/entries/{eid}/publish")
        assert r.status_code == 403


# ─── POST /entries/{id}/deprecate ─────────────────────────────────


class TestDeprecate:
    def test_owner_deprecates_published(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "published"
        r = c.post(f"/api/v1/catalog/entries/{eid}/deprecate")
        assert r.status_code == 200
        assert fake_storage["entries"][eid]["status"] == "deprecated"

    def test_cant_deprecate_draft(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.post(f"/api/v1/catalog/entries/{eid}/deprecate")
        assert r.status_code == 409


# ─── GET /submissions/queue ───────────────────────────────────────


class TestQueue:
    def test_root_sees_pending(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.post(f"/api/v1/catalog/entries/{eid}/submit", json={})

        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["submissions"]) == 1
        assert body["submissions"][0]["review_status"] == "pending"

    def test_non_root_forbidden(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 403

    def test_filter_by_status(self, fake_storage):
        # 2 entries: 1 pending, 1 approved
        c = make_client({"id": "u1", "role": "comum"})
        e1 = _create_draft(c, fake_storage, "u1")
        c.post(f"/api/v1/catalog/entries/{e1}/submit", json={})
        e2 = _create_draft(c, fake_storage, "u1")
        c.post(f"/api/v1/catalog/entries/{e2}/submit", json={})

        # Aprova a primeira
        sid_first = next(iter(fake_storage["submissions"].keys()))
        c_root = make_client({"id": "root1", "role": "root"})
        c_root.post(
            f"/api/v1/catalog/submissions/{sid_first}/decide",
            json={"decision": "approved"},
        )

        # Pending agora só tem 1
        r = c_root.get("/api/v1/catalog/submissions/queue?status=pending")
        assert r.json()["total"] == 1
        # Approved agora tem 1
        r = c_root.get("/api/v1/catalog/submissions/queue?status=approved")
        assert r.json()["total"] == 1


# ─── GET /entries/{id}/submissions ────────────────────────────────


class TestEntrySubmissions:
    def test_owner_sees_history(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        r = c.get(f"/api/v1/catalog/entries/{eid}/submissions")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c1.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}/submissions")
        assert r.status_code == 403

    def test_root_can_see_history(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c1.post(f"/api/v1/catalog/entries/{eid}/submit", json={})
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.get(f"/api/v1/catalog/entries/{eid}/submissions")
        assert r.status_code == 200
        assert r.json()["total"] == 1


# ═════════════════════════════════════════════════════════════════
# Capability Disclosure CRUD (PR 4)
# ═════════════════════════════════════════════════════════════════


class TestCapabilityPut:
    def test_owner_declares_minimal(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        assert r.status_code == 200
        body = r.json()
        # Todos os defaults False
        assert body["reads_user_kb"] is False
        assert body["processes_pii"] is False
        # entry_id no payload
        assert body["entry_id"] == eid

    def test_owner_declares_full_payload(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        payload = {
            "reads_user_kb": True,
            "calls_external_apis": True,
            "external_apis_list": ["https://api.openai.com", "https://api.anthropic.com"],
            "processes_pii": True,
            "stores_input": True,
            "storage_retention_days": 30,
            "data_residency": "BR",
            "additional_notes": "Pseudonimização aplicada antes do storage",
        }
        r = c.put(f"/api/v1/catalog/entries/{eid}/capability", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["calls_external_apis"] is True
        assert body["external_apis_list"] == payload["external_apis_list"]
        assert body["data_residency"] == "BR"

    def test_external_apis_list_required_when_flag_true(self, fake_storage):
        # Pydantic CapabilityDisclosure valida: calls_external_apis=True exige lista não vazia
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.put(
            f"/api/v1/catalog/entries/{eid}/capability",
            json={"calls_external_apis": True, "external_apis_list": []},
        )
        assert r.status_code == 422

    def test_negative_retention_rejected(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.put(
            f"/api/v1/catalog/entries/{eid}/capability",
            json={"stores_input": True, "storage_retention_days": -1},
        )
        assert r.status_code == 422

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        assert r.status_code == 403

    def test_root_can_declare_for_others(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        assert r.status_code == 200

    def test_cant_edit_after_submit(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "submitted"
        r = c.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        assert r.status_code == 409

    def test_cant_edit_after_publish(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "published"
        r = c.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        assert r.status_code == 409

    def test_audits_declaration(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/capability", json={"processes_pii": True})
        actions = [a["action"] for a in fake_storage["audit"]]
        assert "capability_declared" in actions

    def test_404_when_entry_missing(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.put("/api/v1/catalog/entries/missing/capability", json={})
        assert r.status_code == 404


class TestCapabilityGet:
    def test_owner_reads_own(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/capability", json={"processes_pii": True})
        r = c.get(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 200
        assert r.json()["processes_pii"] is True

    def test_404_when_not_declared(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.get(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 404

    def test_404_when_entry_invisible(self, fake_storage):
        # u1 cria + declara; u2 não consegue ver entry draft → não consegue ver capability
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c1.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 404

    def test_other_user_reads_published_entry_disclosure(self, fake_storage):
        # Transparência: consumer vê disclosure antes de invocar
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c1.put(f"/api/v1/catalog/entries/{eid}/capability", json={"processes_pii": True})
        # Move para published+company para outros verem
        fake_storage["entries"][eid]["status"] = "published"
        fake_storage["entries"][eid]["visibility"] = "company"
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 200
        assert r.json()["processes_pii"] is True


class TestCapabilityDelete:
    def test_owner_deletes(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        r = c.delete(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 200
        assert eid not in fake_storage["disclosures"]

    def test_404_when_not_declared(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        r = c.delete(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 404

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, fake_storage, "u1")
        c1.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.delete(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 403

    def test_cant_delete_after_submit(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/capability", json={})
        fake_storage["entries"][eid]["status"] = "submitted"
        r = c.delete(f"/api/v1/catalog/entries/{eid}/capability")
        assert r.status_code == 409


# ═════════════════════════════════════════════════════════════════
# External Platforms metadata (Onda 2 / PR 1)
# ═════════════════════════════════════════════════════════════════


def _create_external_draft(client, fake_storage, owner_id: str) -> str:
    _seed_owner(fake_storage, owner_id)
    r = client.post(
        "/api/v1/catalog/entries",
        json={
            "name": "ChatGPT Enterprise",
            "kind": "external_platform",
            "adapter_type": "openai_assistants",
            "description": "ChatGPT Enterprise para o time todo",
        },
    )
    assert r.status_code == 201, r.json()
    return r.json()["id"]


class TestExternalMetadataPut:
    def test_owner_declares_with_vendor(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        r = c.put(
            f"/api/v1/catalog/entries/{eid}/external-metadata",
            json={"vendor": "OpenAI", "contract_status": "active"},
        )
        assert r.status_code == 200
        assert r.json()["vendor"] == "OpenAI"
        assert r.json()["contract_status"] == "active"

    def test_first_put_requires_vendor(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        r = c.put(
            f"/api/v1/catalog/entries/{eid}/external-metadata",
            json={"contract_status": "active"},  # sem vendor
        )
        assert r.status_code == 422
        assert "vendor" in r.json()["detail"].lower()

    def test_second_put_can_omit_vendor(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/external-metadata",
              json={"vendor": "OpenAI"})
        # Update sem vendor — deve passar (mantém valor)
        r = c.put(f"/api/v1/catalog/entries/{eid}/external-metadata",
                  json={"monthly_cost_usd": 15000})
        assert r.status_code == 200
        assert r.json()["vendor"] == "OpenAI"
        assert r.json()["monthly_cost_usd"] == 15000

    def test_rejects_non_external_kind(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")  # kind=agent
        r = c.put(
            f"/api/v1/catalog/entries/{eid}/external-metadata",
            json={"vendor": "X"},
        )
        assert r.status_code == 422
        assert "external_platform" in r.json()["detail"]

    def test_nonowner_forbidden(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c1, fake_storage, "u1")
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.put(
            f"/api/v1/catalog/entries/{eid}/external-metadata",
            json={"vendor": "Hacker"},
        )
        assert r.status_code == 403

    def test_root_can_declare_for_others(self, fake_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c1, fake_storage, "u1")
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.put(
            f"/api/v1/catalog/entries/{eid}/external-metadata",
            json={"vendor": "OpenAI"},
        )
        assert r.status_code == 200

    def test_cant_edit_after_submit(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        fake_storage["entries"][eid]["status"] = "submitted"
        r = c.put(f"/api/v1/catalog/entries/{eid}/external-metadata",
                  json={"vendor": "OpenAI"})
        assert r.status_code == 409

    def test_audits_declaration(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/external-metadata",
              json={"vendor": "OpenAI"})
        actions = [a["action"] for a in fake_storage["audit"]]
        assert "external_metadata_declared" in actions

    def test_rejects_bad_iso_date(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        r = c.put(
            f"/api/v1/catalog/entries/{eid}/external-metadata",
            json={"vendor": "OpenAI", "contract_renewal_date": "31/12/2026"},
        )
        assert r.status_code == 422


class TestExternalMetadataGet:
    def test_owner_reads_own(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        c.put(f"/api/v1/catalog/entries/{eid}/external-metadata",
              json={"vendor": "OpenAI"})
        r = c.get(f"/api/v1/catalog/entries/{eid}/external-metadata")
        assert r.status_code == 200
        assert r.json()["vendor"] == "OpenAI"

    def test_404_when_not_declared(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c, fake_storage, "u1")
        r = c.get(f"/api/v1/catalog/entries/{eid}/external-metadata")
        assert r.status_code == 404

    def test_404_when_not_external_kind(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, fake_storage, "u1")  # agent
        r = c.get(f"/api/v1/catalog/entries/{eid}/external-metadata")
        assert r.status_code == 404

    def test_transparent_for_published(self, fake_storage):
        # Outros users veem metadata externa de entry publicada
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_external_draft(c1, fake_storage, "u1")
        c1.put(f"/api/v1/catalog/entries/{eid}/external-metadata",
               json={"vendor": "OpenAI"})
        fake_storage["entries"][eid]["status"] = "published"
        fake_storage["entries"][eid]["visibility"] = "company"
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.get(f"/api/v1/catalog/entries/{eid}/external-metadata")
        assert r.status_code == 200
        assert r.json()["vendor"] == "OpenAI"


# ═════════════════════════════════════════════════════════════════
# Inventário Regulatório (Onda 2 / PR 3)
# ═════════════════════════════════════════════════════════════════


class TestInventory:
    """Endpoints /inventory e /inventory/export.csv só testam plumbing HTTP +
    role gate. A query SQL com JOIN é coberta pelo smoke test."""

    def test_non_root_forbidden_json(self, monkeypatch):
        async def fake_list(**kwargs):
            return [], 0
        monkeypatch.setattr("app.routes.catalog.list_inventory", fake_list)
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/inventory")
        assert r.status_code == 403

    def test_non_root_forbidden_csv(self, monkeypatch):
        async def fake_list(**kwargs):
            return [], 0
        monkeypatch.setattr("app.routes.catalog.list_inventory", fake_list)
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/inventory/export.csv")
        assert r.status_code == 403

    def test_root_can_access(self, monkeypatch):
        async def fake_list(**kwargs):
            return [{"id": "e1", "name": "X"}], 1
        monkeypatch.setattr("app.routes.catalog.list_inventory", fake_list)
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/inventory")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["entries"]) == 1

    def test_filters_passed_to_query(self, monkeypatch):
        captured = {}
        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [], 0
        monkeypatch.setattr("app.routes.catalog.list_inventory", fake_list)
        c = make_client({"id": "root1", "role": "root"})
        c.get("/api/v1/catalog/inventory?processes_pii=true&calls_external_apis=false&kind=external_platform&residency=BR")
        assert captured["flags"]["processes_pii"] is True
        assert captured["flags"]["calls_external_apis"] is False
        # Não setados ficam None
        assert captured["flags"]["processes_health"] is None
        assert captured["kind"] == "external_platform"
        assert captured["residency"] == "BR"

    def test_empty_flag_means_no_filter(self, monkeypatch):
        captured = {}
        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [], 0
        monkeypatch.setattr("app.routes.catalog.list_inventory", fake_list)
        c = make_client({"id": "root1", "role": "root"})
        c.get("/api/v1/catalog/inventory?processes_pii=")
        assert captured["flags"]["processes_pii"] is None

    def test_csv_returns_text_csv(self, monkeypatch):
        async def fake_list(**kwargs):
            return [{
                "id": "e1", "urn": "urn:maestro:default:agent:x:1.0.0",
                "name": "Agente Fiscal", "kind": "agent", "status": "published",
                "version": "1.0.0", "domain": "fiscal",
                "owner_user_id": "u1", "steward_team": None, "visibility": "company",
                "processes_pii": True, "processes_financial": False, "processes_health": False,
                "calls_external_apis": False, "accesses_internet": False, "stores_input": False,
                "writes_user_kb": False, "reads_user_kb": True, "trains_on_input": False,
                "data_residency": "BR", "external_apis_list": ["https://api.openai.com"],
                "storage_retention_days": None,
                "vendor": None, "monthly_cost_usd": None,
                "contract_status": None, "contract_renewal_date": None,
                "created_at": "2026-01-01T00:00:00", "published_at": None,
            }], 1
        monkeypatch.setattr("app.routes.catalog.list_inventory", fake_list)
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/inventory/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        # Conteúdo tem header CSV + 1 row
        text = r.text
        assert "id,urn,name" in text
        assert "Agente Fiscal" in text
        # Lista serializada com "; "
        assert "https://api.openai.com" in text


# ═════════════════════════════════════════════════════════════════
# Stewardship Dashboard (Onda 2 / PR 4)
# ═════════════════════════════════════════════════════════════════


class TestStewardship:
    def test_root_returns_entries_and_by_team(self, monkeypatch):
        async def fake(**kwargs):
            return [{"id": "e1", "name": "X", "steward_team": "fiscal"}], {
                "fiscal": {"total": 1, "orphan": 0, "stale": 0, "low_reliability": 0,
                           "published": 1, "deprecated": 0},
            }
        monkeypatch.setattr("app.routes.catalog.list_stewardship", fake)
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/stewardship")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert "fiscal" in body["by_team"]
        assert body["viewer_is_root"] is True

    def test_filter_by_team(self, monkeypatch):
        captured = {}
        async def fake(**kwargs):
            captured.update(kwargs)
            return [], {}
        monkeypatch.setattr("app.routes.catalog.list_stewardship", fake)
        c = make_client({"id": "root1", "role": "root"})
        c.get("/api/v1/catalog/stewardship?steward_team=fiscal")
        assert captured["steward_team"] == "fiscal"

    # ── Onda 3: aberto a stewards de área ────────────────

    def test_root_no_team_restriction(self, monkeypatch):
        captured = {}
        async def fake(**kwargs):
            captured.update(kwargs)
            return [], {}
        monkeypatch.setattr("app.routes.catalog.list_stewardship", fake)
        c = make_client({"id": "root1", "role": "root"})
        c.get("/api/v1/catalog/stewardship")
        # Root passa restrict_to_teams=None (sem filtro)
        assert captured["restrict_to_teams"] is None

    def test_non_root_restricted_to_own_domains(self, monkeypatch):
        captured = {}
        async def fake(**kwargs):
            captured.update(kwargs)
            return [], {}
        monkeypatch.setattr("app.routes.catalog.list_stewardship", fake)
        c = make_client({
            "id": "u1", "role": "comum",
            "domains": '["fiscal", "rh"]',
        })
        r = c.get("/api/v1/catalog/stewardship")
        assert r.status_code == 200
        assert captured["restrict_to_teams"] == ["fiscal", "rh"]
        assert r.json()["viewer_is_root"] is False
        assert r.json()["viewer_domains"] == ["fiscal", "rh"]

    def test_non_root_no_domains_gets_empty_via_restrict(self, monkeypatch):
        captured = {}
        async def fake(**kwargs):
            captured.update(kwargs)
            # Mesmo se o query falasse com a DB, restrict_to_teams=[] curto-circuita
            return [], {}
        monkeypatch.setattr("app.routes.catalog.list_stewardship", fake)
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/stewardship")
        assert r.status_code == 200
        assert captured["restrict_to_teams"] == []
        assert r.json()["viewer_domains"] == []

    def test_non_root_returns_viewer_metadata(self, monkeypatch):
        async def fake(**kwargs): return [], {}
        monkeypatch.setattr("app.routes.catalog.list_stewardship", fake)
        c = make_client({
            "id": "u1", "role": "comum",
            "domains": '["fiscal"]',
        })
        r = c.get("/api/v1/catalog/stewardship")
        body = r.json()
        assert body["viewer_is_root"] is False
        assert body["viewer_domains"] == ["fiscal"]


class TestReassign:
    def _setup_entry(self, fake_storage, owner_id="u-original", steward="fiscal"):
        eid = "ent-reassign"
        fake_storage["entries"][eid] = {
            "id": eid,
            "owner_user_id": owner_id,
            "steward_team": steward,
            "kind": "agent",
            "status": "published",
            "urn": "urn:maestro:default:agent:x:1.0.0",
            "name": "X",
            "version": "1.0.0",
        }
        return eid

    def test_non_root_forbidden(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post(f"/api/v1/catalog/entries/{eid}/reassign",
                   json={"new_owner_user_id": "u2"})
        assert r.status_code == 403

    def test_empty_payload_rejected(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        c = make_client({"id": "root1", "role": "root"})
        r = c.post(f"/api/v1/catalog/entries/{eid}/reassign", json={})
        assert r.status_code == 422

    def test_404_when_entry_missing(self, fake_storage):
        c = make_client({"id": "root1", "role": "root"})
        r = c.post("/api/v1/catalog/entries/nonexistent/reassign",
                   json={"new_steward_team": "rh"})
        assert r.status_code == 404

    def test_reassign_owner_requires_existing_user(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        c = make_client({"id": "root1", "role": "root"})
        # new_owner não existe em fake_storage["users"]
        r = c.post(f"/api/v1/catalog/entries/{eid}/reassign",
                   json={"new_owner_user_id": "ghost"})
        assert r.status_code == 422

    def test_reassign_owner_success(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        fake_storage["users"]["u-new"] = {"id": "u-new", "status": "active"}
        c = make_client({"id": "root1", "role": "root"})
        r = c.post(f"/api/v1/catalog/entries/{eid}/reassign",
                   json={"new_owner_user_id": "u-new"})
        assert r.status_code == 200
        assert fake_storage["entries"][eid]["owner_user_id"] == "u-new"

    def test_reassign_steward_only(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        c = make_client({"id": "root1", "role": "root"})
        r = c.post(f"/api/v1/catalog/entries/{eid}/reassign",
                   json={"new_steward_team": "rh"})
        assert r.status_code == 200
        assert fake_storage["entries"][eid]["steward_team"] == "rh"

    def test_reassign_audit(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        c = make_client({"id": "root1", "role": "root"})
        c.post(f"/api/v1/catalog/entries/{eid}/reassign",
               json={"new_steward_team": "rh"})
        actions = [a["action"] for a in fake_storage["audit"]]
        assert "stewardship_reassigned" in actions

    def test_clear_steward_with_empty_string(self, fake_storage):
        eid = self._setup_entry(fake_storage)
        c = make_client({"id": "root1", "role": "root"})
        r = c.post(f"/api/v1/catalog/entries/{eid}/reassign",
                   json={"new_steward_team": ""})
        assert r.status_code == 200
        assert fake_storage["entries"][eid]["steward_team"] is None


# ═════════════════════════════════════════════════════════════════
# Bulk decide (Onda 2 / PR 5)
# ═════════════════════════════════════════════════════════════════


class TestBulkDecide:
    def _setup_two_pending(self, fake_storage):
        """Cria 2 entries draft → submete cada uma → retorna lista de submission_ids."""
        c = make_client({"id": "u1", "role": "comum"})
        ids = []
        for i in range(2):
            eid = _create_draft(c, fake_storage, "u1")
            body = c.post(f"/api/v1/catalog/entries/{eid}/submit", json={}).json()
            ids.append(body["submission_id"])
        return ids

    def test_non_root_forbidden(self, fake_storage):
        sids = self._setup_two_pending(fake_storage)
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/submissions/bulk-decide",
                   json={"submission_ids": sids, "decision": "approved"})
        assert r.status_code == 403

    def test_empty_ids_rejected(self, fake_storage):
        c = make_client({"id": "root1", "role": "root"})
        r = c.post("/api/v1/catalog/submissions/bulk-decide",
                   json={"submission_ids": [], "decision": "approved"})
        assert r.status_code == 422

    def test_duplicates_rejected(self, fake_storage):
        c = make_client({"id": "root1", "role": "root"})
        r = c.post("/api/v1/catalog/submissions/bulk-decide",
                   json={"submission_ids": ["a", "a"], "decision": "approved"})
        assert r.status_code == 422

    def test_unknown_decision_rejected(self, fake_storage):
        c = make_client({"id": "root1", "role": "root"})
        r = c.post("/api/v1/catalog/submissions/bulk-decide",
                   json={"submission_ids": ["a"], "decision": "maybe"})
        assert r.status_code == 422

    def test_bulk_approve_success(self, fake_storage):
        sids = self._setup_two_pending(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.post("/api/v1/catalog/submissions/bulk-decide",
                        json={"submission_ids": sids, "decision": "approved", "notes": "lote"})
        assert r.status_code == 200
        body = r.json()
        assert body["succeeded_count"] == 2
        assert body["failed_count"] == 0
        # Todas viraram approved
        for sid in sids:
            assert fake_storage["submissions"][sid]["review_status"] == "approved"

    def test_bulk_with_one_invalid_id(self, fake_storage):
        sids = self._setup_two_pending(fake_storage)
        sids_with_ghost = sids + ["nonexistent"]
        c_root = make_client({"id": "root1", "role": "root"})
        r = c_root.post("/api/v1/catalog/submissions/bulk-decide",
                        json={"submission_ids": sids_with_ghost, "decision": "approved"})
        assert r.status_code == 200
        body = r.json()
        assert body["succeeded_count"] == 2
        assert body["failed_count"] == 1
        assert body["failed"][0]["submission_id"] == "nonexistent"

    def test_bulk_skips_already_decided(self, fake_storage):
        sids = self._setup_two_pending(fake_storage)
        # Decide a primeira individualmente antes do bulk
        c_root = make_client({"id": "root1", "role": "root"})
        c_root.post(f"/api/v1/catalog/submissions/{sids[0]}/decide",
                    json={"decision": "approved"})
        # Agora bulk com as duas → primeira deve falhar
        r = c_root.post("/api/v1/catalog/submissions/bulk-decide",
                        json={"submission_ids": sids, "decision": "rejected"})
        body = r.json()
        assert body["succeeded_count"] == 1
        assert body["failed_count"] == 1

    def test_bulk_audits_each_success(self, fake_storage):
        sids = self._setup_two_pending(fake_storage)
        c_root = make_client({"id": "root1", "role": "root"})
        c_root.post("/api/v1/catalog/submissions/bulk-decide",
                    json={"submission_ids": sids, "decision": "approved"})
        # Cada sucesso gera 1 audit 'review_approved'
        approved_actions = [a for a in fake_storage["audit"] if a["action"] == "review_approved"]
        assert len(approved_actions) == 2
        # E todas marcadas como bulk no details
        import json as _json
        for a in approved_actions:
            details = a["details"] if isinstance(a["details"], dict) else _json.loads(a["details"])
            assert details.get("bulk") is True


# ═════════════════════════════════════════════════════════════════
# Cost & Consumption (Onda 3 / PR 2)
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def cost_storage(monkeypatch, fake_storage):
    """Mocks de record_invocation_cost / aggregate_costs / list_costs_raw
    sobre o fake_storage existente."""
    records: list[dict] = []

    async def fake_record(entry_id, **kwargs):
        rec = {"id": f"c-{len(records)}", "entry_id": entry_id, **kwargs}
        records.append(rec)
        return rec

    async def fake_aggregate(**kwargs):
        relevant = [r for r in records
                    if not kwargs.get("entry_id") or r["entry_id"] == kwargs["entry_id"]]
        if kwargs.get("consumer_user_id"):
            relevant = [r for r in relevant if r.get("consumer_user_id") == kwargs["consumer_user_id"]]
        gb = kwargs.get("group_by", "entry")
        if gb not in ("entry", "consumer", "department", "day"):
            raise ValueError(f"group_by inválido: {gb}")
        keys: dict = {}
        for r in relevant:
            k = r.get("entry_id") if gb == "entry" else r.get("consumer_user_id")
            keys[k] = keys.get(k, {"group_key": k, "invocations": 0,
                                    "total_cost_usd": 0, "total_tokens": 0,
                                    "avg_latency_ms": 0})
            keys[k]["invocations"] += 1
            keys[k]["total_cost_usd"] += r.get("cost_usd", 0)
            keys[k]["total_tokens"] += r.get("tokens_used", 0)
        totals = {
            "invocations": len(relevant),
            "total_cost_usd": sum(r.get("cost_usd", 0) for r in relevant),
            "total_tokens": sum(r.get("tokens_used", 0) for r in relevant),
            "avg_latency_ms": 0,
            "distinct_entries": len({r["entry_id"] for r in relevant}),
            "distinct_consumers": len({r.get("consumer_user_id") for r in relevant}),
        }
        return list(keys.values()), totals

    async def fake_list_raw(**kwargs):
        relevant = list(records)
        if kwargs.get("entry_id"):
            relevant = [r for r in relevant if r["entry_id"] == kwargs["entry_id"]]
        if kwargs.get("consumer_user_id"):
            relevant = [r for r in relevant if r.get("consumer_user_id") == kwargs["consumer_user_id"]]
        return relevant

    monkeypatch.setattr("app.routes.catalog.record_invocation_cost", fake_record)
    monkeypatch.setattr("app.routes.catalog.aggregate_costs", fake_aggregate)
    monkeypatch.setattr("app.routes.catalog.list_costs_raw", fake_list_raw)

    return {**fake_storage, "cost_records": records}


class TestRecordCost:
    def test_records_with_user_default(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, cost_storage, "u1")
        cost_storage["entries"][eid]["status"] = "published"
        cost_storage["entries"][eid]["visibility"] = "company"
        r = c.post(f"/api/v1/catalog/entries/{eid}/invocation-cost",
                   json={"cost_usd": 0.02, "tokens_used": 1500, "latency_ms": 320})
        assert r.status_code == 201
        body = r.json()
        assert body["entry_id"] == eid
        # Default: consumer_user_id = user.id
        assert body["consumer_user_id"] == "u1"

    def test_records_with_explicit_consumer(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, cost_storage, "u1")
        cost_storage["entries"][eid]["status"] = "published"
        cost_storage["entries"][eid]["visibility"] = "company"
        r = c.post(f"/api/v1/catalog/entries/{eid}/invocation-cost",
                   json={"consumer_user_id": "other-user", "cost_usd": 0.5})
        assert r.status_code == 201
        assert r.json()["consumer_user_id"] == "other-user"

    def test_404_when_entry_invisible(self, cost_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, cost_storage, "u1")  # private/draft
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.post(f"/api/v1/catalog/entries/{eid}/invocation-cost",
                    json={"cost_usd": 0.01})
        assert r.status_code == 404

    def test_negative_cost_rejected(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, cost_storage, "u1")
        r = c.post(f"/api/v1/catalog/entries/{eid}/invocation-cost",
                   json={"cost_usd": -1})
        assert r.status_code == 422


class TestGetCost:
    def test_auto_scope_root_returns_all(self, cost_storage):
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/cost")
        assert r.status_code == 200
        assert r.json()["scope"] == "all"

    def test_auto_scope_nonroot_returns_mine(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/cost")
        assert r.status_code == 200
        assert r.json()["scope"] == "mine"

    def test_nonroot_explicit_all_forbidden(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/cost?scope=all")
        assert r.status_code == 403

    def test_group_by_invalid_returns_422(self, cost_storage):
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/cost?group_by=bogus")
        assert r.status_code == 422

    def test_mine_filters_to_user(self, cost_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c1, cost_storage, "u1")
        cost_storage["entries"][eid]["status"] = "published"
        cost_storage["entries"][eid]["visibility"] = "company"
        c1.post(f"/api/v1/catalog/entries/{eid}/invocation-cost", json={"cost_usd": 1.0})
        c2 = make_client({"id": "u2", "role": "comum"})
        c2.post(f"/api/v1/catalog/entries/{eid}/invocation-cost", json={"cost_usd": 2.0})

        # u2 vê só o seu (1 row, cost=2)
        r = c2.get("/api/v1/catalog/cost?group_by=consumer")
        body = r.json()
        assert body["totals"]["invocations"] == 1
        assert body["totals"]["total_cost_usd"] == 2.0

        # Root vê tudo (2 rows somando 3)
        c_root = make_client({"id": "root1", "role": "root"})
        r_root = c_root.get("/api/v1/catalog/cost?group_by=consumer")
        assert r_root.json()["totals"]["invocations"] == 2
        assert r_root.json()["totals"]["total_cost_usd"] == 3.0


class TestExportCostCsv:
    def test_returns_text_csv(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, cost_storage, "u1")
        cost_storage["entries"][eid]["status"] = "published"
        cost_storage["entries"][eid]["visibility"] = "company"
        c.post(f"/api/v1/catalog/entries/{eid}/invocation-cost",
               json={"cost_usd": 0.5, "tokens_used": 100})

        r = c.get("/api/v1/catalog/cost/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        assert "id,entry_id,consumer_user_id" in r.text
        assert "u1" in r.text

    def test_nonroot_all_forbidden(self, cost_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/cost/export.csv?scope=all")
        assert r.status_code == 403


# ═════════════════════════════════════════════════════════════════
# Recipes (Onda 3 / PR 3)
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def recipe_storage(monkeypatch, fake_storage):
    """Mocks de get_recipe / upsert_recipe / delete_recipe sobre fake_storage."""
    recipes: dict[str, dict] = {}

    async def fake_get(entry_id):
        if entry_id not in recipes:
            return None
        # Enriquece steps com target_name/kind do entries fake
        rec = dict(recipes[entry_id])
        enriched = []
        for s in rec.get("steps", []):
            tid = s.get("target_entry_id")
            t = fake_storage["entries"].get(tid)
            enriched.append({
                **s,
                "target_name": t.get("name") if t else None,
                "target_kind": t.get("kind") if t else None,
                "target_status": t.get("status") if t else None,
                "target_exists": t is not None,
            })
        rec["steps"] = enriched
        return rec

    async def fake_upsert(entry_id, steps):
        # Replica regras do real: anti-self e target deve existir
        for s in steps:
            if s.get("target_entry_id") == entry_id:
                raise ValueError("recipe não pode invocar a si mesmo")
        target_ids = [s.get("target_entry_id") for s in steps]
        missing = [tid for tid in target_ids if tid not in fake_storage["entries"]]
        if missing:
            raise ValueError(f"target_entry_id(s) inexistente(s): {missing}")
        recipes[entry_id] = {"entry_id": entry_id, "steps": steps}
        return {"entry_id": entry_id, "steps": steps}

    async def fake_delete(entry_id):
        return recipes.pop(entry_id, None) is not None

    monkeypatch.setattr("app.routes.catalog.get_recipe", fake_get)
    monkeypatch.setattr("app.routes.catalog.upsert_recipe", fake_upsert)
    monkeypatch.setattr("app.routes.catalog.delete_recipe", fake_delete)

    return {**fake_storage, "recipes": recipes}


def _create_recipe_draft(client, storage, owner_id: str) -> str:
    """Cria entry kind=recipe (sem artifact)."""
    _seed_owner(storage, owner_id)
    r = client.post(
        "/api/v1/catalog/entries",
        json={
            "name": "Pipeline Fiscal",
            "kind": "recipe",
            "description": "Recipe que encadeia agentes fiscais",
        },
    )
    assert r.status_code == 201, r.json()
    return r.json()["id"]


def _seed_target(storage, name="Step Target", kind="agent"):
    """Insere uma entry no fake storage diretamente (sem passar pela API)."""
    import uuid
    tid = f"target-{uuid.uuid4().hex[:8]}"
    storage["entries"][tid] = {
        "id": tid, "name": name, "kind": kind, "status": "published",
        "version": "1.0.0", "urn": f"urn:maestro:default:{kind}:slug:1.0.0",
        "owner_user_id": "u-owner",
    }
    return tid


class TestRecipeKindCreation:
    def test_recipe_kind_no_artifact_required(self, recipe_storage):
        """Onda 3: recipe não exige artifact_type/artifact_id."""
        c = make_client({"id": "u1", "role": "comum"})
        _seed_owner(recipe_storage, "u1")
        r = c.post(
            "/api/v1/catalog/entries",
            json={"name": "R1", "kind": "recipe"},
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "recipe"


class TestRecipePut:
    def test_owner_defines_steps(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage, "Extrair", "agent")
        t2 = _seed_target(recipe_storage, "Classificar", "agent")
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [
                {"order": 1, "target_entry_id": t1, "notes": "extrai"},
                {"order": 2, "target_entry_id": t2, "notes": "classifica"},
            ],
        })
        assert r.status_code == 200
        body = r.json()
        assert len(body["steps"]) == 2
        assert body["steps"][0]["target_entry_id"] == t1

    def test_rejects_self_reference(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": rid}],
        })
        assert r.status_code == 422
        assert "si mesmo" in r.json()["detail"]

    def test_rejects_nonexistent_target(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": "ghost"}],
        })
        assert r.status_code == 422

    def test_rejects_duplicate_target(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage)
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [
                {"order": 1, "target_entry_id": t1},
                {"order": 2, "target_entry_id": t1},  # duplicado
            ],
        })
        assert r.status_code == 422

    def test_rejects_empty_steps(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={"steps": []})
        assert r.status_code == 422

    def test_normalizes_order_gaps(self, recipe_storage):
        """Validator renumera para 1..N mesmo se vier com gaps."""
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage, "A")
        t2 = _seed_target(recipe_storage, "B")
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [
                {"order": 5, "target_entry_id": t1},
                {"order": 10, "target_entry_id": t2},
            ],
        })
        assert r.status_code == 200
        # Renumerado para 1, 2
        orders = [s["order"] for s in r.json()["steps"]]
        assert orders == [1, 2]

    def test_nonowner_forbidden(self, recipe_storage):
        c1 = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c1, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage)
        c2 = make_client({"id": "u2", "role": "comum"})
        r = c2.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        assert r.status_code == 403

    def test_cant_edit_after_submit(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        recipe_storage["entries"][rid]["status"] = "submitted"
        t1 = _seed_target(recipe_storage)
        r = c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        assert r.status_code == 409

    def test_non_recipe_kind_rejected(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, recipe_storage, "u1")  # kind=agent
        t1 = _seed_target(recipe_storage)
        r = c.put(f"/api/v1/catalog/entries/{eid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        assert r.status_code == 422
        assert "recipe" in r.json()["detail"]

    def test_audits_definition(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage)
        c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        actions = [a["action"] for a in recipe_storage["audit"]]
        assert "recipe_defined" in actions


class TestRecipeGet:
    def test_owner_reads(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage, "Extrair", "agent")
        c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        r = c.get(f"/api/v1/catalog/entries/{rid}/recipe")
        assert r.status_code == 200
        body = r.json()
        assert len(body["steps"]) == 1
        # Enriquecido
        assert body["steps"][0]["target_name"] == "Extrair"

    def test_404_when_not_defined(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        r = c.get(f"/api/v1/catalog/entries/{rid}/recipe")
        assert r.status_code == 404

    def test_404_for_non_recipe_kind(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        eid = _create_draft(c, recipe_storage, "u1")
        r = c.get(f"/api/v1/catalog/entries/{eid}/recipe")
        assert r.status_code == 404


class TestRecipeDelete:
    def test_owner_deletes(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage)
        c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        r = c.delete(f"/api/v1/catalog/entries/{rid}/recipe")
        assert r.status_code == 200
        assert rid not in recipe_storage["recipes"]

    def test_404_when_not_defined(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        r = c.delete(f"/api/v1/catalog/entries/{rid}/recipe")
        assert r.status_code == 404

    def test_cant_delete_after_submit(self, recipe_storage):
        c = make_client({"id": "u1", "role": "comum"})
        rid = _create_recipe_draft(c, recipe_storage, "u1")
        t1 = _seed_target(recipe_storage)
        c.put(f"/api/v1/catalog/entries/{rid}/recipe", json={
            "steps": [{"order": 1, "target_entry_id": t1}],
        })
        recipe_storage["entries"][rid]["status"] = "submitted"
        r = c.delete(f"/api/v1/catalog/entries/{rid}/recipe")
        assert r.status_code == 409


# ═════════════════════════════════════════════════════════════════
# Fila de revisão — filtragem de órfãs + cleanup admin
# ═════════════════════════════════════════════════════════════════
# Submissions cuja entry foi deletada são órfãs; Root não pode decidir
# sobre algo que não existe. A fila deve filtrá-las e há endpoint admin
# para limpar lixo histórico (FK CASCADE só pega deletes futuros).


def _seed_pending_submission(storage, *, sub_id, entry_id, entry_exists=True):
    """Cria submission pending. Se entry_exists=True, cria também a entry
    com aquele id (caso normal). Se False, simula órfã (entry deletada)."""
    storage["submissions"][sub_id] = {
        "id": sub_id,
        "entry_id": entry_id,
        "submitted_by": "u1",
        "review_status": "pending",
        "submitted_at": f"2026-05-20T15:{len(storage['submissions']):02d}:00Z",
        "precheck_passed": True,
        "review_notes": "",
    }
    if entry_exists and entry_id not in storage["entries"]:
        storage["entries"][entry_id] = {
            "id": entry_id,
            "name": "Real Entry",
            "kind": "agent",
            "status": "submitted",
            "version": "1.0.0",
            "owner_user_id": "u1",
            "visibility": "company",
        }


class TestSubmissionsQueue:
    def test_root_pode_acessar_fila(self, fake_storage):
        _seed_pending_submission(fake_storage, sub_id="s1", entry_id="e1")
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["submissions"][0]["id"] == "s1"

    def test_comum_403(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 403

    def test_orfas_sao_filtradas(self, fake_storage):
        """Submission com entry_id apontando para entry inexistente NÃO aparece.
        Cobre o cenário do print original: 2 'pending' fantasmas após delete."""
        # Uma válida (entry existe) e duas órfãs (entry não existe)
        _seed_pending_submission(fake_storage, sub_id="real", entry_id="e-real")
        _seed_pending_submission(fake_storage, sub_id="ghost1", entry_id="e-ghost", entry_exists=False)
        _seed_pending_submission(fake_storage, sub_id="ghost2", entry_id="e-ghost", entry_exists=False)

        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 200
        body = r.json()
        # Só a válida aparece — total também reflete o JOIN, não o count raw
        ids = [s["id"] for s in body["submissions"]]
        assert ids == ["real"]
        assert body["total"] == 1

    def test_filtro_por_status(self, fake_storage):
        _seed_pending_submission(fake_storage, sub_id="s1", entry_id="e1")
        # Aprovada — não deve aparecer em ?status=pending
        fake_storage["submissions"]["s2"] = {
            "id": "s2", "entry_id": "e1",
            "submitted_by": "u1", "review_status": "approved",
            "submitted_at": "2026-05-20T10:00:00Z",
            "precheck_passed": True, "review_notes": "",
        }
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/submissions/queue?status=pending")
        ids = [s["id"] for s in r.json()["submissions"]]
        assert ids == ["s1"]

    def test_inclui_dados_da_entry_aninhados(self, fake_storage):
        """Cada submission devolvida tem objeto `entry` aninhado com nome, kind
        e version — para a UI Root mostrar 'Análise de Dados (AGENT v1.0.0)'
        em vez de '(entry deletada — XXX)' para entries que existem."""
        _seed_pending_submission(fake_storage, sub_id="s1", entry_id="e-real")
        # _seed_pending_submission cria entry com name="Real Entry", kind="agent"
        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 200
        sub = r.json()["submissions"][0]
        assert sub["entry"]["id"] == "e-real"
        assert sub["entry"]["name"] == "Real Entry"
        assert sub["entry"]["kind"] == "agent"
        assert sub["entry"]["version"] == "1.0.0"
        assert sub["entry"]["status"] == "submitted"

    def test_inclui_disclosure_e_submitter_para_contexto_root(self, fake_storage):
        """Fila vem com disclosure (LEFT JOIN — None se ausente) e submitter
        com email/role (não só UUID). Root decide com contexto rico."""
        _seed_pending_submission(fake_storage, sub_id="s1", entry_id="e-real")
        # Adiciona disclosure para a entry
        fake_storage["disclosures"]["e-real"] = {
            "entry_id": "e-real",
            "processes_pii": True,
            "calls_external_apis": False,
            "data_residency": "BR",
            "reads_user_kb": True,
        }
        # Submitter resolvido com nome real (não só UUID)
        fake_storage["users"]["u1"] = {"id": "u1", "email": "alice@empresa.com", "role": "comum"}

        c = make_client({"id": "root1", "role": "root"})
        r = c.get("/api/v1/catalog/submissions/queue")
        assert r.status_code == 200
        sub = r.json()["submissions"][0]
        # Disclosure presente
        assert sub["disclosure"] is not None
        assert sub["disclosure"]["processes_pii"] is True
        assert sub["disclosure"]["data_residency"] == "BR"
        # Submitter resolvido
        assert sub["submitter"]["email"] == "alice@empresa.com"
        assert sub["submitter"]["role"] == "comum"

    def test_disclosure_none_quando_entry_sem_disclosure(self, fake_storage):
        """LEFT JOIN: entry sem disclosure declarada → sub.disclosure == None
        (UI mostra estado 'sem flags ativas declaradas')."""
        _seed_pending_submission(fake_storage, sub_id="s1", entry_id="e-bare")
        c = make_client({"id": "root1", "role": "root"})
        sub = c.get("/api/v1/catalog/submissions/queue").json()["submissions"][0]
        assert sub["disclosure"] is None

    def test_entry_meta_completa(self, fake_storage):
        """Entry aninhada inclui domain/visibility/steward (não só id+name).
        Root precisa pra decidir se a entry tá pronta operacionalmente."""
        _seed_pending_submission(fake_storage, sub_id="s1", entry_id="e-meta")
        fake_storage["entries"]["e-meta"].update({
            "domain": "fiscal",
            "visibility": "department",
            "visibility_scope": "contabilidade",
            "steward_team": "time-fiscal@empresa",
        })
        c = make_client({"id": "root1", "role": "root"})
        sub = c.get("/api/v1/catalog/submissions/queue").json()["submissions"][0]
        assert sub["entry"]["domain"] == "fiscal"
        assert sub["entry"]["visibility"] == "department"
        assert sub["entry"]["visibility_scope"] == "contabilidade"
        assert sub["entry"]["steward_team"] == "time-fiscal@empresa"


class TestCleanupOrphanSubmissions:
    def test_root_limpa_orfas(self, fake_storage):
        _seed_pending_submission(fake_storage, sub_id="real", entry_id="e-real")
        _seed_pending_submission(fake_storage, sub_id="g1", entry_id="ghost", entry_exists=False)
        _seed_pending_submission(fake_storage, sub_id="g2", entry_id="ghost", entry_exists=False)

        c = make_client({"id": "root1", "role": "root"})
        r = c.post("/api/v1/catalog/admin/cleanup-orphan-submissions")
        assert r.status_code == 200
        assert r.json()["deleted_count"] == 2
        # Real preservada
        assert "real" in fake_storage["submissions"]
        assert "g1" not in fake_storage["submissions"]
        assert "g2" not in fake_storage["submissions"]
        # Audit registrado (com count, sem entry_id específico — cross-entry)
        actions = [a["action"] for a in fake_storage["audit"]]
        assert "cleanup_orphan_submissions" in actions
        cleanup_evt = next(a for a in fake_storage["audit"] if a["action"] == "cleanup_orphan_submissions")
        assert "2" in cleanup_evt["details"]  # deleted_count=2 dentro do JSON

    def test_idempotente_zero_orfas(self, fake_storage):
        _seed_pending_submission(fake_storage, sub_id="real", entry_id="e-real")
        c = make_client({"id": "root1", "role": "root"})
        r1 = c.post("/api/v1/catalog/admin/cleanup-orphan-submissions")
        r2 = c.post("/api/v1/catalog/admin/cleanup-orphan-submissions")
        assert r1.json()["deleted_count"] == 0
        assert r2.json()["deleted_count"] == 0
        # Sem audit quando não houve delete (não polui o log)
        actions = [a["action"] for a in fake_storage["audit"]]
        assert "cleanup_orphan_submissions" not in actions

    def test_comum_403(self, fake_storage):
        c = make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/admin/cleanup-orphan-submissions")
        assert r.status_code == 403
