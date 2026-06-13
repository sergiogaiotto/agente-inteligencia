"""PR4 — publicar pipeline no Catálogo (kind='pipeline').

Cobre: make_urn aceita 'pipeline'; require_artifact_link exige artefato p/
pipeline; o endpoint /catalog/entries/from-pipeline cria entry draft kind=pipeline
referenciando o pipeline; 404 (pipeline inexistente) e 409 (URN duplicado).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
from app.routes import catalog as catalog_routes
from app.core.auth import require_user
from app.catalog.urn import make_urn, VALID_KINDS
from app.catalog.models import CatalogEntryCreate


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _make_client(user):
    app = FastAPI()
    app.include_router(catalog_routes.router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# ── urn + model ──
def test_make_urn_accepts_pipeline():
    assert "pipeline" in VALID_KINDS
    assert make_urn("pipeline", "Folha Fiscal", "1.0.0") == "urn:maestro:default:pipeline:folha-fiscal:1.0.0"


def test_require_artifact_link_pipeline_needs_artifact():
    m = CatalogEntryCreate(name="X", kind="pipeline")
    with pytest.raises(ValueError):
        m.require_artifact_link()
    # com artefato → ok (não levanta)
    ok = CatalogEntryCreate(name="X", kind="pipeline", artifact_type="pipeline", artifact_id="p1")
    ok.require_artifact_link()


# ── endpoint /entries/from-pipeline ──
class TestPublishFromPipeline:
    def _pipeline(self):
        return {"id": "p1", "name": "Folha Fiscal", "description": "desc", "domain": "fiscal"}

    def test_creates_pipeline_kind_draft_entry(self, monkeypatch):
        created = {}
        async def fake_create(row):
            created.update(row)
            return row
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(self._pipeline()))
        monkeypatch.setattr(db.catalog_entries_repo, "create", fake_create)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        c = _make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/from-pipeline", json={"pipeline_id": "p1", "version": "1.0.0"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "pipeline"
        assert body["artifact_type"] == "pipeline"
        assert body["artifact_id"] == "p1"
        assert body["status"] == "draft"
        assert body["owner_user_id"] == "u1"
        assert body["urn"] == "urn:maestro:default:pipeline:folha-fiscal:1.0.0"
        # persistiu com a referência ao pipeline no adapter_config
        assert created["kind"] == "pipeline"
        assert "p1" in created["adapter_config"]

    def test_default_name_from_pipeline(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(self._pipeline()))
        monkeypatch.setattr(db.catalog_entries_repo, "create", _async(None))
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        c = _make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/from-pipeline", json={"pipeline_id": "p1"})
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "Folha Fiscal"  # herdou o nome do pipeline

    def test_404_when_pipeline_missing(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(None))
        c = _make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/from-pipeline", json={"pipeline_id": "ghost"})
        assert r.status_code == 404, r.text

    def test_409_on_duplicate_urn(self, monkeypatch):
        async def boom(row):
            raise Exception('duplicate key value violates unique constraint "catalog_entries_urn_key"')
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(self._pipeline()))
        monkeypatch.setattr(db.catalog_entries_repo, "create", boom)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        c = _make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/from-pipeline", json={"pipeline_id": "p1", "version": "1.0.0"})
        assert r.status_code == 409, r.text

    def test_422_on_bad_version(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(self._pipeline()))
        c = _make_client({"id": "u1", "role": "comum"})
        r = c.post("/api/v1/catalog/entries/from-pipeline", json={"pipeline_id": "p1", "version": "v1"})
        assert r.status_code == 422, r.text
