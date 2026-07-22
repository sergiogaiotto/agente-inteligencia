"""F1 (67.0.0) — Painel da federação: GET /api/v1/federation/dashboard + audit de sync_failed.

O painel agrega SÓ dados locais: peers (sem segredos), entries federadas,
consumo em catalog_costs (peer-attested) e syncs derivadas do audit_log.
Regras seladas aqui:
- ROOT-only (inventário de peers é sensível); NÃO gated por federation_enabled
  (observabilidade funciona com a federação desligada).
- A resposta NUNCA carrega shared_secret/secret_prev/fingerprint.
- Órfãs: entry federada sem peer ('peer_ausente') ou com peer revogado
  ('peer_revogado').
- Totais de consumo somam TODAS as entries federadas (inclusive órfãs) —
  somar só peers conhecidos esconderia gasto real.
- Falha de sync agora é AUDITADA (action='sync_failed', best-effort): antes era
  só log, e o painel não tinha como mostrar "última falha de sync".
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.core.ssrf import SSRFError
from app.routes import federation as fed_routes

REPO_ROOT = Path(__file__).resolve().parents[1]

ROOT = {"id": "r", "role": "root"}
MEMBER = {"id": "u", "role": "member"}


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client(user):
    app = FastAPI()
    app.include_router(fed_routes.router)
    app.include_router(fed_routes.peers_router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


class _Conn:
    """fetch() despachado pelo SQL — o dashboard consulta 2 tabelas na mesma conexão."""

    def __init__(self, entries, costs):
        self.entries, self.costs = entries, costs

    async def fetch(self, sql, *p):
        if "FROM catalog_entries" in sql:
            return self.entries
        if "FROM catalog_costs" in sql:
            return self.costs
        raise AssertionError("SQL inesperado: " + sql)


class _Acq:
    def __init__(self, c): self.c = c
    async def __aenter__(self): return self.c
    async def __aexit__(self, *a): return False


class _Pool:
    def __init__(self, c): self.c = c
    def acquire(self): return _Acq(self.c)


def _audit_by_type(events_by_type):
    async def _fn(*a, **k):
        return events_by_type.get(k.get("entity_type"), [])
    return _fn


# Rows CRUAS de peer (com segredos cifrados) — provam que o endpoint não vaza.
P1 = {
    "id": "p1", "workspace": "acme", "base_url": "https://acme.example",
    "status": "active",
    "created_at": datetime(2026, 7, 1, 12, 0), "rotated_at": datetime(2026, 7, 10, 12, 0),
    "shared_secret": "enc::SUPERSECRETA", "secret_prev": "enc::ANTERIOR",
    "fingerprint": "fp-x",
}
P2 = {
    "id": "p2", "workspace": "beta", "base_url": None, "status": "revoked",
    "created_at": datetime(2026, 6, 1, 8, 0), "rotated_at": None,
    "shared_secret": "enc::OUTRA", "secret_prev": None, "fingerprint": None,
}

ENTRIES = [
    {"id": "e1", "name": "Cap X", "kind": "pipeline", "domain": "telecom",
     "version": "1.0.0", "remote_urn": "urn:maestro:acme:pipeline:x:1.0.0",
     "remote_peer_id": "p1"},
    {"id": "e2", "name": "Cap Y", "kind": "pipeline", "domain": None,
     "version": "1.0.0", "remote_urn": "urn:maestro:acme:pipeline:y:1.0.0",
     "remote_peer_id": "p1"},
    {"id": "e3", "name": "Cap Z", "kind": "pipeline", "domain": "telecom",
     "version": "1.0.0", "remote_urn": "urn:maestro:beta:pipeline:z:1.0.0",
     "remote_peer_id": "p2"},
    {"id": "e4", "name": "Cap Fantasma", "kind": "pipeline", "domain": None,
     "version": "1.0.0", "remote_urn": "urn:maestro:ghost:pipeline:g:1.0.0",
     "remote_peer_id": "ghost"},
]

COSTS = [
    {"entry_id": "e1", "invocations": 3, "total_cost_usd": 0.30,
     "avg_latency_ms": 100.0, "last_invoked_at": datetime(2026, 7, 21, 9, 0)},
    {"entry_id": "e2", "invocations": 1, "total_cost_usd": 0.10,
     "avg_latency_ms": 200.0, "last_invoked_at": datetime(2026, 7, 20, 9, 0)},
    # e4 é órfã e MESMO ASSIM entra nos totais — o gasto existiu.
    {"entry_id": "e4", "invocations": 2, "total_cost_usd": 0.50,
     "avg_latency_ms": 50.0, "last_invoked_at": datetime(2026, 7, 19, 9, 0)},
]

# Eventos em ordem DESC (contrato do audit_repo.find_all) — o 1º visto vence.
PEER_EVENTS = [
    {"entity_id": "p1", "action": "synced", "created_at": datetime(2026, 7, 20, 10, 0)},
    {"entity_id": "p1", "action": "sync_failed", "created_at": datetime(2026, 7, 19, 10, 0)},
    {"entity_id": "p1", "action": "sync_failed", "created_at": datetime(2026, 7, 18, 10, 0)},
    {"entity_id": "p1", "action": "synced", "created_at": datetime(2026, 7, 17, 10, 0)},
    {"entity_id": "p1", "action": "created", "created_at": datetime(2026, 7, 1, 12, 0)},
]

INVOKE_EVENTS = [
    {"entity_id": "e1", "created_at": datetime(2026, 7, 21, 9, 0),
     "details": json.dumps({"peer_workspace": "acme", "status": "completed"})},
    {"entity_id": "sumiu", "created_at": datetime(2026, 7, 18, 9, 0),
     "details": "não-é-json"},
]


def _patch_dashboard(monkeypatch, *, peers=(), entries=(), costs=(),
                     events=None, enabled=False, key=True):
    monkeypatch.setattr(fed_routes.peers, "list_peers", _async(list(peers)))
    monkeypatch.setattr(fed_routes, "_get_pool",
                        lambda: _Pool(_Conn(list(entries), list(costs))))
    monkeypatch.setattr(fed_routes.audit_repo, "find_all",
                        _audit_by_type(events or {}))
    monkeypatch.setattr(fed_routes, "federation_enabled", _async(enabled))
    monkeypatch.setattr(fed_routes, "secret_key_present", lambda: key)


class TestDashboardRBAC:
    def test_member_403(self, monkeypatch):
        _patch_dashboard(monkeypatch)
        r = _client(MEMBER).get("/api/v1/federation/dashboard")
        assert r.status_code == 403

    def test_root_200_mesmo_com_federacao_desligada(self, monkeypatch):
        # Observabilidade NÃO é gated por federation_enabled (diferente do manifesto).
        _patch_dashboard(monkeypatch, enabled=False)
        r = _client(ROOT).get("/api/v1/federation/dashboard")
        assert r.status_code == 200
        assert r.json()["federation_enabled"] is False


class TestDashboardEmptyState:
    def test_vazio_sem_zeros_fabricados(self, monkeypatch):
        _patch_dashboard(monkeypatch, key=False)
        b = _client(ROOT).get("/api/v1/federation/dashboard").json()
        assert b["peers"] == []
        assert b["orphans"] == []
        assert b["by_domain"] == []
        assert b["recent_invocations"] == []
        assert b["totals"] == {
            "peers_active": 0, "peers_revoked": 0, "remote_capabilities": 0,
            "orphan_capabilities": 0, "invocations": 0, "total_cost_usd": 0.0,
        }
        assert b["costs_peer_attested"] is True
        assert b["secret_key_present"] is False
        assert b["generated_at"]  # sempre presente


class TestDashboardAggregation:
    def _body(self, monkeypatch):
        _patch_dashboard(
            monkeypatch, peers=[P1, P2], entries=ENTRIES, costs=COSTS,
            events={"federation_peer": PEER_EVENTS,
                    "federation_remote_invoke": INVOKE_EVENTS},
            enabled=True,
        )
        r = _client(ROOT).get("/api/v1/federation/dashboard")
        assert r.status_code == 200
        return r.json()

    def test_peer_com_syncs_derivadas_do_audit(self, monkeypatch):
        b = self._body(monkeypatch)
        p1 = next(p for p in b["peers"] if p["id"] == "p1")
        # O evento mais recente de cada action vence (lista vem DESC).
        assert p1["last_sync_at"] == "2026-07-20T10:00:00"
        assert p1["last_sync_failed_at"] == "2026-07-19T10:00:00"
        assert p1["sync_failures_recent"] == 2
        p2 = next(p for p in b["peers"] if p["id"] == "p2")
        assert p2["last_sync_at"] is None
        assert p2["sync_failures_recent"] == 0

    def test_consumo_por_peer_e_media_ponderada(self, monkeypatch):
        b = self._body(monkeypatch)
        p1 = next(p for p in b["peers"] if p["id"] == "p1")
        assert p1["capabilities"] == 2
        c = p1["consumption"]
        assert c["invocations"] == 4
        assert abs(c["total_cost_usd"] - 0.40) < 1e-9
        # média ponderada: (100*3 + 200*1) / 4 = 125.0
        assert c["avg_latency_ms"] == 125.0
        assert c["last_invoked_at"] == "2026-07-21T09:00:00"
        # peer revogado sem custo: consumo honesto zerado, latência None
        p2 = next(p for p in b["peers"] if p["id"] == "p2")
        assert p2["consumption"]["invocations"] == 0
        assert p2["consumption"]["avg_latency_ms"] is None

    def test_orfas_por_razao(self, monkeypatch):
        b = self._body(monkeypatch)
        reasons = {o["id"]: o["reason"] for o in b["orphans"]}
        assert reasons == {"e3": "peer_revogado", "e4": "peer_ausente"}

    def test_totais_incluem_consumo_de_orfas(self, monkeypatch):
        b = self._body(monkeypatch)
        t = b["totals"]
        assert t["peers_active"] == 1 and t["peers_revoked"] == 1
        assert t["remote_capabilities"] == 4 and t["orphan_capabilities"] == 2
        # 3 (e1) + 1 (e2) + 2 (e4 órfã) — órfã CONTA
        assert t["invocations"] == 6
        assert abs(t["total_cost_usd"] - 0.90) < 1e-9

    def test_by_domain_agrupa_null_como_none(self, monkeypatch):
        b = self._body(monkeypatch)
        pares = {(d["domain"], d["count"]) for d in b["by_domain"]}
        assert pares == {("telecom", 2), (None, 2)}

    def test_atividade_recente_honesta(self, monkeypatch):
        b = self._body(monkeypatch)
        rec = b["recent_invocations"]
        assert rec[0]["entry_name"] == "Cap X"
        assert rec[0]["peer_workspace"] == "acme"
        assert rec[0]["status"] == "completed"
        # entry sumida + details corrompido → campos None, nunca inventados
        assert rec[1]["entry_name"] is None
        assert rec[1]["peer_workspace"] is None
        assert rec[1]["status"] is None

    def test_nenhum_segredo_vaza(self, monkeypatch):
        b = self._body(monkeypatch)
        raw = json.dumps(b)
        for proibido in ("enc::", "SUPERSECRETA", "ANTERIOR",
                         "shared_secret", "secret_prev", "fingerprint"):
            assert proibido not in raw, proibido


class TestSyncFailedAudit:
    """sync_peer_route: falha passa a deixar rastro no audit_log (best-effort)."""

    def _patch(self, monkeypatch, exc, audit_calls):
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active",
                                    "workspace": "acme", "base_url": "https://acme.example"}))

        async def _boom(*a, **k):
            raise exc
        monkeypatch.setattr(fed_routes.egress, "sync_remote_entries", _boom)

        async def _capture(row):
            audit_calls.append(row)
        monkeypatch.setattr(fed_routes.audit_repo, "create", _capture)

    def test_httpx_error_audita_sync_failed_e_502(self, monkeypatch):
        calls = []
        self._patch(monkeypatch, httpx.ConnectError("boom"), calls)
        r = _client(ROOT).post("/api/v1/federation/peers/p1/sync", json={})
        assert r.status_code == 502
        assert len(calls) == 1
        row = calls[0]
        assert row["entity_type"] == "federation_peer"
        assert row["action"] == "sync_failed"
        assert row["entity_id"] == "p1"
        det = json.loads(row["details"])
        assert det["workspace"] == "acme"
        assert det["cause_kind"] == "network"

    def test_peer_error_vira_categoria_sem_texto_do_peer(self, monkeypatch):
        # ValueError carrega o detail do CORPO DE ERRO DO PEER (texto hostil de
        # até ~300 chars). O audit é legível por qualquer autenticado via
        # /api/v1/history — persiste-se a CATEGORIA, nunca o texto.
        calls = []
        self._patch(monkeypatch, ValueError("peer disse: <script>xss</script> em 10.0.0.7"), calls)
        r = _client(ROOT).post("/api/v1/federation/peers/p1/sync", json={})
        assert r.status_code == 502
        det = json.loads(calls[0]["details"])
        assert det["cause_kind"] == "peer_error"
        raw = calls[0]["details"]
        assert "peer disse" not in raw and "10.0.0.7" not in raw

    def test_ssrf_audita_categoria_sem_host_interno_e_400(self, monkeypatch):
        # A mensagem do SSRFError contém hostname/IP PRIVADO da base_url — só a
        # categoria vai ao audit; a versão verbosa fica no 400 (rota root-only).
        calls = []
        self._patch(monkeypatch, SSRFError("host resolve para IP não-público: 10.1.2.3"), calls)
        r = _client(ROOT).post("/api/v1/federation/peers/p1/sync", json={})
        assert r.status_code == 400
        det = json.loads(calls[0]["details"])
        assert det["cause_kind"] == "ssrf"
        assert "10.1.2.3" not in calls[0]["details"]

    def test_falha_do_audit_nao_mascara_o_502(self, monkeypatch):
        # Best-effort de verdade: audit quebrado não muda o status da falha real.
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active", "workspace": "acme"}))

        async def _boom(*a, **k):
            raise httpx.ConnectError("boom")
        monkeypatch.setattr(fed_routes.egress, "sync_remote_entries", _boom)

        async def _audit_quebrado(row):
            raise RuntimeError("banco de auditoria fora")
        monkeypatch.setattr(fed_routes.audit_repo, "create", _audit_quebrado)
        r = _client(ROOT).post("/api/v1/federation/peers/p1/sync", json={})
        assert r.status_code == 502

    def test_sucesso_continua_auditando_synced(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fed_routes, "federation_enabled", _async(True))
        monkeypatch.setattr(fed_routes.federation_peers_repo, "find_by_id",
                            _async({"id": "p1", "status": "active", "workspace": "acme"}))
        monkeypatch.setattr(fed_routes.egress, "sync_remote_entries",
                            _async({"registered": 2, "skipped": 0}))

        async def _capture(row):
            calls.append(row)
        monkeypatch.setattr(fed_routes.audit_repo, "create", _capture)
        r = _client(ROOT).post("/api/v1/federation/peers/p1/sync", json={})
        assert r.status_code == 200
        assert [c["action"] for c in calls] == ["synced"]


class TestTemplateMarkers:
    """Markers da aba Painel no template cru (padrão test_federation_ui_feedback)."""

    def _content(self) -> str:
        return (REPO_ROOT / "app" / "templates" / "pages" /
                "federation.html").read_text(encoding="utf-8")

    def test_abas_e_testids_presentes(self):
        c = self._content()
        for marker in (
            'data-testid="federation-tab-painel"',
            'data-testid="federation-tab-gestao"',
            'data-testid="fed-dash-totals"',
            'data-testid="fed-dash-peer"',
            'data-testid="fed-dash-orphans"',
            'data-testid="fed-dash-activity"',
            'data-testid="fed-dash-error"',
            'data-testid="fed-dash-updated"',
        ):
            assert marker in c, marker

    def test_timestamps_usam_tz_helpers(self):
        # Nunca fatiar ISO na mão — tz* de base.html é obrigatório (naive = UTC).
        c = self._content()
        assert "tzDateTime(" in c
        assert "tzTime(" in c
        assert "tzParse(" in c

    def test_rotulo_peer_attested_presente(self):
        # Honestidade de métrica: custo remoto não é medido localmente.
        assert "atestado pelo peer" in self._content()

    def test_script_sem_backtick_e_sem_jinja(self):
        c = self._content()
        script = c[c.index("<script>"):]
        assert "`" not in script      # footgun de template literal em Jinja
        assert "{{" not in script     # Jinja come chaves duplas no <script> → 500

    def test_dashboard_carrega_no_load(self):
        c = self._content()
        assert "loadDashboard()" in c
        assert "'/api/v1/federation/dashboard'" in c

    def test_403_detectado_pelo_status_nao_pelo_detail(self):
        # api.get NÃO parseia o corpo do erro (só verbos de escrita usam
        # _errDetail) — a mensagem é "GET url → 403 [trace]". Detectar 'root'
        # no texto era código morto (achado da revisão adversarial da F1).
        c = self._content()
        assert "indexOf('→ 403')" in c
