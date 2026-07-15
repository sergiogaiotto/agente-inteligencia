"""DELETE /api/v1/releases/{id} — fecha o último gap do CRUD do Registry §18.

Release de teste era irremovível (backlog E2E 2026-06-23; o DELETE de
eval-runs, mesmo gap, saiu em 27.1.0). Contrato: staging/candidate → hard
delete + audit (com contagem de eval_runs órfãos); canary/production → 409
com hint de re-promover para staging antes.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.dashboard as dash


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


def _release(env="staging"):
    return {"id": "r1", "name": "rel-teste", "environment": env, "status": "candidate"}


def test_delete_release_ok_with_audit(monkeypatch):
    audits = []

    async def _audit(payload):
        audits.append(payload)

    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async(_release()))
    monkeypatch.setattr(dash.releases_repo, "delete", _async(True))
    monkeypatch.setattr(dash.eval_runs_repo, "count", _async(2))
    monkeypatch.setattr(dash.audit_repo, "create", _audit)

    r = _client().delete("/api/v1/releases/r1")
    assert r.status_code == 200, r.text
    assert "removida" in r.json()["message"].lower()
    assert audits and audits[0]["action"] == "deleted"
    assert audits[0]["entity_type"] == "release"
    # órfãos registrados no audit (sem FK, o delete não cascateia)
    assert json.loads(audits[0]["details"])["eval_runs_orfaos"] == 2


def test_delete_release_404_when_missing(monkeypatch):
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async(None))
    r = _client().delete("/api/v1/releases/ghost")
    assert r.status_code == 404, r.text


@pytest.mark.parametrize("env", ["canary", "production"])
def test_delete_release_409_when_promoted(monkeypatch, env):
    deleted = []

    async def _delete(*a, **k):
        deleted.append(a)
        return True

    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async(_release(env)))
    monkeypatch.setattr(dash.releases_repo, "delete", _delete)

    r = _client().delete("/api/v1/releases/r1")
    assert r.status_code == 409, r.text
    # hint acionável: a "despromoção" é o próprio promote de volta pra staging
    assert "staging" in r.json()["detail"]
    assert not deleted, "guard deve barrar ANTES do delete"


def test_delete_release_404_on_row_gone_race(monkeypatch):
    """find_by_id acha, mas a linha sumiu entre a checagem e o DELETE."""
    monkeypatch.setattr(dash.releases_repo, "find_by_id", _async(_release()))
    monkeypatch.setattr(dash.releases_repo, "delete", _async(False))
    r = _client().delete("/api/v1/releases/r1")
    assert r.status_code == 404, r.text
