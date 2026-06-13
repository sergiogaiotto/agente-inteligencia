"""PR8b1 — federação provider (read-only).

Cobre: gate de exposição `is_federation_exposable`, `pipeline_fingerprint`
(determinístico/ordem-independente), `_disclosure_summary`, o resolver SELADO
`resolve_federated_exec` (sem fallback de mesh vivo; root∈membros), `build_manifest`
e a rota `/.well-known/maestro-federation.json` (404 desligado, 200 ligado).

Tudo puro OU com `settings`/queries monkeypatchados — sem Postgres. Async via
`asyncio.run`. Rota via TestClient sobre um app mínimo (padrão do projeto).
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.catalog.federation as fed
from app.routes import federation as fed_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


class TestIsFederationExposable:
    def _entry(self, **over):
        e = {"status": "published", "visibility": "company", "kind": "pipeline"}
        e.update(over)
        return e

    def test_published_company_pipeline_is_exposable(self):
        assert fed.is_federation_exposable(self._entry())

    def test_draft_not_exposable(self):
        assert not fed.is_federation_exposable(self._entry(status="draft"))

    def test_deprecated_not_exposable(self):
        assert not fed.is_federation_exposable(self._entry(status="deprecated"))

    def test_private_not_exposable(self):
        assert not fed.is_federation_exposable(self._entry(visibility="private"))

    def test_department_not_exposable(self):
        # department NÃO é exponível (can_user_see admitiria — por isso gate dedicado)
        assert not fed.is_federation_exposable(self._entry(visibility="department"))

    def test_non_allowlisted_kind_not_exposable(self):
        assert not fed.is_federation_exposable(self._entry(kind="agent"))
        assert not fed.is_federation_exposable(self._entry(kind="recipe"))

    def test_missing_fields_not_exposable(self):
        assert not fed.is_federation_exposable({})


class TestPipelineFingerprint:
    def _pdef(self):
        return {
            "root_agent_id": "a1",
            "nodes": [{"id": "a1", "kind": "aobd"}, {"id": "a2", "kind": "subagent"}],
            "edges": [{"id": "e1", "source": "a1", "target": "a2", "type": "sequential"}],
        }

    def test_deterministic(self):
        d = self._pdef()
        assert fed.pipeline_fingerprint(d) == fed.pipeline_fingerprint(d)

    def test_order_independent(self):
        d1 = self._pdef()
        d2 = self._pdef()
        d2["nodes"] = list(reversed(d2["nodes"]))
        assert fed.pipeline_fingerprint(d1) == fed.pipeline_fingerprint(d2)

    def test_changes_with_edge(self):
        d1 = self._pdef()
        d2 = self._pdef()
        d2["edges"][0]["target"] = "a3"
        assert fed.pipeline_fingerprint(d1) != fed.pipeline_fingerprint(d2)

    def test_none_without_root(self):
        assert fed.pipeline_fingerprint(None) is None
        assert fed.pipeline_fingerprint({"root_agent_id": None, "nodes": [], "edges": []}) is None

    def test_has_alg_prefix(self):
        assert fed.pipeline_fingerprint(self._pdef()).startswith("sha256:")


class TestDisclosureSummary:
    def test_none_passthrough(self):
        assert fed._disclosure_summary(None) is None

    def test_picks_subset(self):
        full = {
            "processes_pii": True, "calls_external_apis": False, "data_residency": "BR",
            "reads_user_kb": True, "additional_notes": "interno",
        }
        s = fed._disclosure_summary(full)
        assert s["processes_pii"] is True
        assert s["data_residency"] == "BR"
        assert "reads_user_kb" not in s        # fora do resumo
        assert "additional_notes" not in s     # fora do resumo


class TestResolveFederatedExec:
    def test_valid_snapshot_returns_root_and_members(self, monkeypatch):
        pdef = {"root_agent_id": "a1", "nodes": [{"id": "a1"}, {"id": "a2"}], "edges": []}
        monkeypatch.setattr(fed, "get_pipeline_def", _async(pdef))
        root, members = asyncio.run(fed.resolve_federated_exec({"id": "e1"}))
        assert root == "a1"
        assert members == {"a1", "a2"}

    def test_no_snapshot_rejects(self, monkeypatch):
        monkeypatch.setattr(fed, "get_pipeline_def", _async(None))
        assert asyncio.run(fed.resolve_federated_exec({"id": "e1"})) == (None, set())

    def test_no_root_rejects(self, monkeypatch):
        monkeypatch.setattr(
            fed, "get_pipeline_def",
            _async({"root_agent_id": None, "nodes": [{"id": "a1"}], "edges": []}),
        )
        assert asyncio.run(fed.resolve_federated_exec({"id": "e1"})) == (None, set())

    def test_root_not_in_members_rejects(self, monkeypatch):
        # raiz fora dos membros (escape vector que a BFS não checaria) → rejeita
        pdef = {"root_agent_id": "ghost", "nodes": [{"id": "a1"}, {"id": "a2"}], "edges": []}
        monkeypatch.setattr(fed, "get_pipeline_def", _async(pdef))
        assert asyncio.run(fed.resolve_federated_exec({"id": "e1"})) == (None, set())

    def test_empty_members_rejects(self, monkeypatch):
        monkeypatch.setattr(
            fed, "get_pipeline_def",
            _async({"root_agent_id": "a1", "nodes": [], "edges": []}),
        )
        assert asyncio.run(fed.resolve_federated_exec({"id": "e1"})) == (None, set())


class TestBuildManifest:
    def _setup(self, monkeypatch, entries, disc=None, pdef=None):
        monkeypatch.setattr(fed, "local_workspace", _async("acme"))
        monkeypatch.setattr(fed, "list_exposable_entries", _async(entries))
        monkeypatch.setattr(fed, "get_disclosure", _async(disc))
        monkeypatch.setattr(fed, "get_pipeline_def", _async(pdef))

    def test_shape_and_workspace(self, monkeypatch):
        self._setup(monkeypatch, [])
        m = asyncio.run(fed.build_manifest())
        assert m["schema_version"] == "1.0"
        assert m["workspace"] == "acme"
        assert m["capabilities"] == []

    def test_pipeline_cap_has_fingerprint_and_invoke_path(self, monkeypatch):
        entry = {
            "id": "e1", "urn": "urn:maestro:acme:pipeline:x:1.0.0", "name": "X",
            "kind": "pipeline", "version": "1.0.0", "domain": "fiscal",
            "description": "d", "status": "published", "visibility": "company",
        }
        pdef = {"root_agent_id": "a1", "nodes": [{"id": "a1"}], "edges": []}
        self._setup(monkeypatch, [entry], disc={"processes_pii": True}, pdef=pdef)
        m = asyncio.run(fed.build_manifest())
        assert len(m["capabilities"]) == 1
        cap = m["capabilities"][0]
        assert cap["urn"] == entry["urn"]
        assert cap["invoke_path"] == fed.FEDERATION_INVOKE_PATH
        assert cap["fingerprint"].startswith("sha256:")
        assert cap["disclosure"]["processes_pii"] is True

    def test_filters_non_exposable(self, monkeypatch):
        # Defesa em profundidade: mesmo que a list devolva algo não-exponível,
        # build_manifest re-aplica o gate. Cobre os 3 casos que can_user_see
        # admitiria/regrediria: draft, department, kind=agent.
        base = {"urn": "u", "name": "Y", "version": "1.0.0"}
        bads = [
            {**base, "id": "e2", "kind": "pipeline", "status": "draft", "visibility": "company"},
            {**base, "id": "e3", "kind": "pipeline", "status": "published", "visibility": "department"},
            {**base, "id": "e4", "kind": "agent", "status": "published", "visibility": "company"},
            {**base, "id": "e5", "kind": "pipeline", "status": "deprecated", "visibility": "company"},
        ]
        self._setup(monkeypatch, bads)
        m = asyncio.run(fed.build_manifest())
        assert m["capabilities"] == []

    def test_manifest_omits_internal_id(self, monkeypatch):
        entry = {
            "id": "secret-internal-id", "urn": "urn:maestro:acme:pipeline:x:1.0.0",
            "name": "X", "kind": "pipeline", "version": "1.0.0", "domain": "fiscal",
            "description": "d", "status": "published", "visibility": "company",
        }
        self._setup(monkeypatch, [entry], pdef={"root_agent_id": "a1", "nodes": [{"id": "a1"}], "edges": []})
        m = asyncio.run(fed.build_manifest())
        cap = m["capabilities"][0]
        assert "id" not in cap and "owner_user_id" not in cap
        assert "secret-internal-id" not in str(cap)  # id interno nunca vaza


class TestListExposableSql:
    """Guarda de DRIFT: a SQL de list_exposable_entries deve casar exatamente
    com o gate puro is_federation_exposable (única parte cega aos outros testes)."""

    class _Conn:
        def __init__(self, sink):
            self.sink = sink

        async def fetch(self, sql, *params):
            self.sink["sql"] = sql
            self.sink["params"] = params
            return []

    class _Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return TestListExposableSql._Acquire(self.conn)

    def test_sql_predicates_match_gate(self, monkeypatch):
        sink = {}
        monkeypatch.setattr(fed, "_get_pool", lambda: self._Pool(self._Conn(sink)))
        rows = asyncio.run(fed.list_exposable_entries())
        assert rows == []
        sql = sink["sql"]
        assert "status='published'" in sql       # == is_federation_exposable
        assert "visibility='company'" in sql
        assert "kind = ANY($1::text[])" in sql
        assert sink["params"] == (list(fed._FEDERATION_KINDS),)


class TestManifestRoute:
    def _client(self):
        app = FastAPI()
        app.include_router(fed_routes.router)
        return TestClient(app, raise_server_exceptions=False)

    def test_404_when_disabled(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(False))
        r = self._client().get("/.well-known/maestro-federation.json")
        assert r.status_code == 404
        # detalhe padrão "Not Found" — indistinguível de rota inexistente
        assert r.json().get("detail") == "Not Found"

    def test_200_when_enabled(self, monkeypatch):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        manifest = {"schema_version": "1.0", "workspace": "acme", "capabilities": []}
        monkeypatch.setattr(fed_routes, "build_manifest", _async(manifest))
        r = self._client().get("/.well-known/maestro-federation.json")
        assert r.status_code == 200
        assert r.json() == manifest
