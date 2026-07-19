"""PR3 — centralização da detecção de raiz + enriquecimento do /topology.

`_detect_roots` vira a FONTE ÚNICA (mesh.html e workspace.html consomem via
/topology no lugar de recomputar). O /topology também passa a expor `roots` e
`pipeline_id` por nó (membership), de forma ADITIVA e fail-safe.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
from app.routes import mesh
from app.routes.mesh import _detect_roots, _router_nonisolated_inbound


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


# ───────── _router_nonisolated_inbound (guarda #4, função pura) ─────────
class TestRouterNonisolatedInbound:
    ROUTER = [{"id": "r", "kind": "router"}, {"id": "o", "kind": "aobd"}]

    def _edge(self, cfg="{}", ttype="sequential", src="o", tgt="r"):
        return [{"id": "e1", "source": src, "target": tgt, "type": ttype, "config": cfg}]

    def test_router_sequential_inbound_sem_scope_flagado(self):
        assert _router_nonisolated_inbound(self.ROUTER, self._edge()) == ["r"]

    def test_router_sequential_inbound_inherit_flagado(self):
        cfg = '{"context_scope": {"mode": "inherit"}}'
        assert _router_nonisolated_inbound(self.ROUTER, self._edge(cfg)) == ["r"]

    def test_router_sequential_inbound_scoped_flagado(self):
        cfg = '{"context_scope": {"mode": "scoped", "template": "output[:200]"}}'
        assert _router_nonisolated_inbound(self.ROUTER, self._edge(cfg)) == ["r"]

    def test_router_isolated_inbound_nao_flagado(self):
        cfg = '{"context_scope": {"mode": "isolated"}}'
        assert _router_nonisolated_inbound(self.ROUTER, self._edge(cfg)) == []

    def test_router_parallel_inbound_flagado(self):
        assert _router_nonisolated_inbound(self.ROUTER, self._edge(ttype="parallel")) == ["r"]

    def test_router_conditional_inbound_nao_flagado(self):
        # conditional/default NÃO são cadeia incondicional → fora do escopo do aviso
        assert _router_nonisolated_inbound(self.ROUTER, self._edge(ttype="conditional")) == []

    def test_router_como_entrada_sem_inbound_nao_flagado(self):
        # roteador que é a ENTRADA (nenhuma aresta o tem como target) → OK
        edges = [{"id": "e1", "source": "r", "target": "b", "type": "conditional", "config": "{}"}]
        assert _router_nonisolated_inbound(self.ROUTER, edges) == []

    def test_nao_router_sequential_inbound_nao_flagado(self):
        # um subagente (não-router) em cadeia é o padrão normal → não avisa
        nodes = [{"id": "s", "kind": "subagent"}, {"id": "o", "kind": "aobd"}]
        edges = [{"id": "e1", "source": "o", "target": "s", "type": "sequential", "config": "{}"}]
        assert _router_nonisolated_inbound(nodes, edges) == []

    def test_config_dict_e_isolated(self):
        # config já como dict (não string) + isolated → não flaga
        edges = [{"id": "e1", "source": "o", "target": "r", "type": "sequential",
                  "config": {"context_scope": {"mode": "isolated"}}}]
        assert _router_nonisolated_inbound(self.ROUTER, edges) == []

    def test_config_malformado_trata_como_nao_isolado(self):
        edges = self._edge(cfg="{not json")
        assert _router_nonisolated_inbound(self.ROUTER, edges) == ["r"]

    def test_dedup_multiplas_arestas_para_mesmo_router(self):
        edges = [
            {"id": "e1", "source": "o", "target": "r", "type": "sequential", "config": "{}"},
            {"id": "e2", "source": "x", "target": "r", "type": "parallel", "config": "{}"},
        ]
        assert _router_nonisolated_inbound(self.ROUTER, edges) == ["r"]

    def test_empty(self):
        assert _router_nonisolated_inbound([], []) == []


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

    def _router_chain(self, scope_cfg="{}"):
        agents = [
            {"id": "o", "name": "O", "kind": "aobd", "status": "active", "llm_provider": "azure", "model": "gpt-4o", "domain": "x", "version": "1.0.0"},
            {"id": "r", "name": "R", "kind": "router", "status": "active", "llm_provider": "azure", "model": "gpt-4o", "domain": "x", "version": "1.0.0"},
        ]
        conns = [{"id": "e1", "source_agent_id": "o", "target_agent_id": "r", "connection_type": "sequential", "config": scope_cfg}]
        return agents, conns

    def test_router_nonisolated_inbound_flagged(self, monkeypatch):
        agents, conns = self._router_chain("{}")
        monkeypatch.setattr(db.agents_repo, "find_all", _async(agents))
        monkeypatch.setattr(db.mesh_repo, "find_all", _async(conns))
        monkeypatch.setattr(db.pipeline_membership, "all", _async([]))
        body = _make_client().get("/api/v1/mesh/topology").json()
        assert body["router_nonisolated_inbound"] == ["r"]

    def test_router_nonisolated_inbound_absent_when_isolated(self, monkeypatch):
        agents, conns = self._router_chain('{"context_scope": {"mode": "isolated"}}')
        monkeypatch.setattr(db.agents_repo, "find_all", _async(agents))
        monkeypatch.setattr(db.mesh_repo, "find_all", _async(conns))
        monkeypatch.setattr(db.pipeline_membership, "all", _async([]))
        body = _make_client().get("/api/v1/mesh/topology").json()
        assert body["router_nonisolated_inbound"] == []

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


# ───────── guarda #4 cabeada no Fluxograma (source smoke) ─────────
def test_flow_editor_wires_router_nonisolated_warning():
    from pathlib import Path
    html = (
        Path(__file__).resolve().parent.parent
        / "app" / "templates" / "pages" / "mesh_flow.html"
    ).read_text(encoding="utf-8")
    assert "isRouterNonisolatedInbound" in html, "getter ausente no editor de fluxo"
    assert "router_nonisolated_inbound" in html, "sinal do /topology não consumido"
    assert "Roteador em cadeia não-isolada" in html, "aviso do roteador ausente"
