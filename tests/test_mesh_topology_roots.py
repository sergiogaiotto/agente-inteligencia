"""PR3 — centralização da detecção de raiz + enriquecimento do /topology.

`_detect_roots` vira a FONTE ÚNICA (mesh.html e workspace.html consomem via
/topology no lugar de recomputar). O /topology também passa a expor `roots` e
`pipeline_id` por nó (membership), de forma ADITIVA e fail-safe.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
from app.routes import mesh
from app.routes.mesh import _detect_roots


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _make_client():
    app = FastAPI()
    app.include_router(mesh.router)
    return TestClient(app, raise_server_exceptions=False)


# ───────────────── _detect_roots (função pura) ─────────────────
class TestDetectRoots:
    def test_source_never_target(self):
        edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]
        assert _detect_roots(edges) == ["a"]

    def test_multiple_roots_preserve_order(self):
        edges = [
            {"source": "r1", "target": "x"},
            {"source": "r2", "target": "y"},
            {"source": "x", "target": "z"},
        ]
        assert _detect_roots(edges) == ["r1", "r2"]

    def test_fanout_single_root(self):
        edges = [{"source": "r", "target": "a"}, {"source": "r", "target": "b"}]
        assert _detect_roots(edges) == ["r"]

    def test_pure_cycle_falls_back_to_all_sources(self):
        # a→b→a : ninguém é source-never-target → fallback p/ todos os sources.
        edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]
        assert _detect_roots(edges) == ["a", "b"]

    def test_empty(self):
        assert _detect_roots([]) == []


# ───────────────── /topology enriquecido ─────────────────
def _agents():
    return [
        {"id": "a", "name": "A", "kind": "router", "status": "active", "llm_provider": "azure", "model": "gpt-4o", "domain": "x", "version": "1.0.0"},
        {"id": "b", "name": "B", "kind": "subagent", "status": "active", "llm_provider": "azure", "model": "gpt-4o", "domain": "x", "version": "1.0.0"},
    ]


def _conns():
    return [{"id": "e1", "source_agent_id": "a", "target_agent_id": "b", "connection_type": "sequential", "config": "{}"}]


class TestTopologyEnrichment:
    def test_roots_and_pipeline_id(self, monkeypatch):
        monkeypatch.setattr(db.agents_repo, "find_all", _async(_agents()))
        monkeypatch.setattr(db.mesh_repo, "find_all", _async(_conns()))
        monkeypatch.setattr(db.pipeline_membership, "all", _async([{"agent_id": "a", "pipeline_id": "p1"}]))
        r = _make_client().get("/api/v1/mesh/topology")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["roots"] == ["a"]
        nodes = {n["id"]: n for n in body["nodes"]}
        assert nodes["a"]["pipeline_id"] == "p1"
        assert nodes["b"]["pipeline_id"] is None
        # campos legados preservados
        assert "edges" in body and "fanout_roots" in body

    def test_membership_failure_is_failsafe(self, monkeypatch):
        # Se a membership levantar (ex.: pool down), a topologia NÃO quebra:
        # segue com pipeline_id=None e ainda traz roots.
        async def boom(*a, **k):
            raise RuntimeError("pool down")
        monkeypatch.setattr(db.agents_repo, "find_all", _async(_agents()))
        monkeypatch.setattr(db.mesh_repo, "find_all", _async(_conns()))
        monkeypatch.setattr(db.pipeline_membership, "all", boom)
        r = _make_client().get("/api/v1/mesh/topology")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["roots"] == ["a"]
        assert all(n["pipeline_id"] is None for n in body["nodes"])
