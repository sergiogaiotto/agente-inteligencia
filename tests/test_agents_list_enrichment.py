"""Lista de Agentes 66.2.0 — enriquecimento (autoria/risco/selo), busca,
filtro por usuário, paginação e painel Governança & Autoria.

Decisões: enriquecimento é OPT-IN (?enriched=1) — consumidores de API não
pagam nada; tudo batelado (2 queries de autoria + 1 de risco + 1 de selo) e
best-effort (falha → lista sem decoração, nunca 500). "SELADO" usa o MESMO
critério do runtime (_resolve_invoke_schema): pipeline 'publicado' E
contract_hash. O PUT de agente passou a auditar action='updated' — sem isso
"última alteração por" não existia na trilha.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_PAGE = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agents.html"


class FakeRepo:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.updated = []

    async def find_all(self, limit=100, offset=0, **f):
        out = [r for r in self.rows if all(r.get(k) == v for k, v in f.items())]
        return out[:limit]

    async def find_by_id(self, i):
        return next((r for r in self.rows if r.get("id") == i), None)

    async def count(self, **f):
        return len([r for r in self.rows if all(r.get(k) == v for k, v in f.items())])

    async def update(self, i, patch):
        self.updated.append((i, patch))
        return {"id": i, **patch}

    async def create(self, row):
        self.rows.append(dict(row))


class _FakeAcquire:
    def __init__(self, con):
        self.con = con

    async def __aenter__(self):
        return self.con

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, con):
        self.con = con

    def acquire(self):
        return _FakeAcquire(self.con)


class TestSealInfo:
    @pytest.mark.asyncio
    async def test_criterio_de_selo_igual_ao_runtime(self, monkeypatch):
        # publicado+hash = SELADO; publicado sem hash NÃO; rascunho NÃO.
        import app.routes.agents as A
        import app.core.database as DB

        class Con:
            async def fetch(self, q, ids):
                return [
                    {"agent_id": "a1", "pid": "p1", "name": "Pipe Selado", "status": "publicado", "contract_hash": "abc"},
                    {"agent_id": "a2", "pid": "p2", "name": "Pipe Pub s/ selo", "status": "publicado", "contract_hash": None},
                    {"agent_id": "a3", "pid": "p3", "name": "Pipe Rascunho", "status": "rascunho", "contract_hash": "x"},
                ]
        monkeypatch.setattr(DB, "_get_pool", lambda: _FakePool(Con()))
        r = await A._agents_seal_info(["a1", "a2", "a3"])
        assert r["a1"]["sealed"] is True and r["a1"]["pipeline_name"] == "Pipe Selado"
        assert r["a2"]["sealed"] is False
        assert r["a3"]["sealed"] is False and r["a3"]["pipeline_status"] == "rascunho"

    @pytest.mark.asyncio
    async def test_sem_pool_nao_quebra(self):
        # unit env sem Postgres: _get_pool levanta → {} (decoração best-effort)
        import app.routes.agents as A
        assert await A._agents_seal_info(["a1"]) == {}
        assert await A._agents_authorship(["a1"]) == {}


class TestChangeActionsWhitelist:
    def test_whitelist_exclui_eventos_de_runtime(self):
        # achado HIGH da revisão: blacklist (<> 'invoked') deixava passar
        # 'tool_strategy_degraded' (gravado a CADA invoke de modelo sem function
        # calling) → "última alteração por" virava o último INVOCADOR.
        import app.routes.agents as A
        assert set(A._CHANGE_ACTIONS) == {
            "created", "updated", "status_changed", "prompt_rollback", "prompt_promoted"}
        assert "invoked" not in A._CHANGE_ACTIONS
        assert "tool_strategy_degraded" not in A._CHANGE_ACTIONS
        # a query usa a whitelist (ANY), não blacklist — vive no helper
        # genérico (66.3.0: app/core/authorship.py, compartilhado com skills)
        import inspect
        import app.core.authorship as AU
        src = inspect.getsource(AU.audit_entity_authorship)
        assert "action = ANY($2::text[])" in src
        assert "<> 'invoked'" not in src
        wrapper = inspect.getsource(A._agents_authorship)
        assert "audit_entity_authorship(\"agent\", ids, _CHANGE_ACTIONS)" in wrapper

    def test_indice_do_audit_log_existe(self):
        # achado de revisão: audit_log era append-heavy SEM nenhum índice —
        # autoria + filtros de governança faziam seq scan.
        from app.core.database import _IDEMPOTENT_MIGRATIONS
        assert any("idx_audit_log_entity" in m for m in _IDEMPOTENT_MIGRATIONS)


class TestListEnriched:
    @pytest.mark.asyncio
    async def test_enriched_junta_autoria_risco_selo(self, monkeypatch):
        import app.routes.agents as A
        import app.core.database as DB
        monkeypatch.setattr(A, "agents_repo", FakeRepo([
            {"id": "a1", "name": "X", "kind": "subagent", "status": "active",
             "config": '{"blob": "grande"}'},
        ]))

        async def _auth(ids):
            return {"a1": {"created_by": "u1", "created_by_name": "Ana",
                           "updated_by": "u2", "updated_by_name": "Beto",
                           "last_change_action": "updated", "last_change_at": "2026-07-20"}}

        async def _seal(ids):
            return {"a1": {"pipeline_id": "p1", "pipeline_name": "Pipe", "pipeline_status": "publicado", "sealed": True}}
        monkeypatch.setattr(A, "_agents_authorship", _auth)
        monkeypatch.setattr(A, "_agents_seal_info", _seal)
        monkeypatch.setattr(DB, "governance_risk_repo", FakeRepo([
            {"id": "r1", "entity_type": "agent", "entity_id": "a1", "tier": "limited"},
        ]))
        r = await A.list_agents(enriched=True)
        a = r["agents"][0]
        assert a["created_by_name"] == "Ana" and a["updated_by_name"] == "Beto"
        assert a["risk_tier"] == "limited"
        assert a["sealed"] is True and a["pipeline_name"] == "Pipe"
        assert "config" not in a  # blob fora do payload da UI (revisão)

    @pytest.mark.asyncio
    async def test_sem_enriched_payload_intacto(self, monkeypatch):
        # consumidores de API existentes não ganham chaves novas nem custo.
        import app.routes.agents as A
        monkeypatch.setattr(A, "agents_repo", FakeRepo([
            {"id": "a1", "name": "X", "kind": "subagent", "status": "active"},
        ]))

        async def _boom(ids):
            raise AssertionError("enrichment não pode rodar sem enriched=1")
        monkeypatch.setattr(A, "_agents_authorship", _boom)
        monkeypatch.setattr(A, "_agents_seal_info", _boom)
        r = await A.list_agents()
        assert "risk_tier" not in r["agents"][0]
        assert "created_by_name" not in r["agents"][0]


class TestUpdateAudita:
    @pytest.mark.asyncio
    async def test_put_grava_action_updated(self, monkeypatch):
        import app.routes.agents as A
        import app.agents.preflight as PF
        from app.models.schemas import AgentUpdate
        repo = FakeRepo([{"id": "a1", "name": "X", "kind": "subagent",
                          "status": "active", "version": "1.0.0"}])
        audit = FakeRepo([])
        monkeypatch.setattr(A, "agents_repo", repo)
        monkeypatch.setattr(A, "audit_repo", audit)

        class _Report:
            blocked = False

            def model_dump(self):
                return {}

        async def _pf(payload):
            return _Report()
        monkeypatch.setattr(PF, "run_preflight", _pf)
        await A.update_agent("a1", AgentUpdate(description="nova descrição"))
        acts = [r for r in audit.rows if r.get("action") == "updated"]
        assert len(acts) == 1
        assert acts[0]["entity_type"] == "agent" and acts[0]["entity_id"] == "a1"
        assert "description" in acts[0]["details"]


class TestTemplate:
    def test_busca_filtro_usuario_paginacao(self):
        html = _PAGE.read_text(encoding="utf-8")
        for t in ("agents-search", "agents-filter-user", "agents-pagination", "agents-pagesize"):
            assert f'data-testid="{t}"' in html, t
        # tamanhos pedidos: 10, 20, 50 e Todos
        assert "{ v: 10, l: '10' }" in html and "{ v: 20, l: '20' }" in html
        assert "{ v: 50, l: '50' }" in html and "{ v: 0, l: 'Todos' }" in html
        assert "applyFilters()" in html and "get pagedAgents()" in html
        assert "get distinctUsers()" in html
        assert "set('enriched', '1')" in html

    def test_fixes_da_revisao_adversarial(self):
        html = _PAGE.read_text(encoding="utf-8")
        # clamp de página (excluir/desativar o último item não some com a lista)
        assert "Math.min(this.agPage, this.agPageCount)" in html
        # desativar remove TAMBÉM de allAgents (senão a busca "ressuscita")
        i_toggle = html.index("async toggleStatus(")
        bloco = html[i_toggle:i_toggle + 900]
        assert "this.allAgents = this.allAgents.filter" in bloco
        # needle da busca com trim (espaço colado não zera o match)
        assert "this.searchText.trim().toLowerCase()" in html
        # filtro-fantasma de usuário resetado no load
        assert "this.filterUser = ''" in html
        # truncamento honesto: total do servidor visível quando > carregados
        assert "serverTotal" in html and "no servidor" in html

    def test_linha_mostra_usuario_selo_risco(self):
        html = _PAGE.read_text(encoding="utf-8")
        assert "agent.updated_by_name || agent.created_by_name" in html
        assert ">SELADO<" in html
        assert "riskTierLabel(agent.risk_tier)" in html

    def test_painel_governanca_autoria(self):
        html = _PAGE.read_text(encoding="utf-8")
        assert 'data-testid="agent-preview-governance"' in html
        for campo in ("Criado por", "Última alteração", "Pipeline / selo", "Risco (EU AI Act)"):
            assert campo in html, campo
        # complementos da Configuração
        for campo in ("Conhecimento geral", "Anexos", "Temperatura"):
            assert campo in html, campo
