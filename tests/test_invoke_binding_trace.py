"""Testes do trace canônico de invocações diretas (/invoke-binding-direct).

Contexto do bug (2026-06-02): invocações diretas de tool/binding (slash
invoke, sem LLM) NÃO produziam trace. Resultado na UI do Maestro Workspace:

- Painel **Execution Log** mostrava "0 entrada(s)" + a mensagem de fallback
  "Sem entradas registradas para esta execução."
- Painel **Rastreabilidade** ficava totalmente vazio (lastTrace=null enquanto
  a sessão estava aberta → nenhum branch x-if do template renderizava).

O usuário pediu: esses dois painéis devem SEMPRE aparecer. A correção:

1. `_build_invoke_trace` — monta o trace canônico (mesmo shape do /chat
   declarativo) com `execution_log` não-vazio. (testes puros)
2. `_persist_invoke_turn(..., trace_data=...)` — grava o trace em
   `trace_data` da interaction para que o reload da sessão reconstrua os
   painéis. (repos mockados, sem DB)

Testes puros + mocks de repositório — não tocam Postgres nem rede.
"""

from __future__ import annotations

import json

import pytest

from app.routes import workspace


# ═════════════════════════════════════════════════════════════════
# _build_invoke_trace — função pura que monta o trace canônico
# ═════════════════════════════════════════════════════════════════
class TestBuildInvokeTrace:
    def _agent(self):
        return {
            "id": "agent-1",
            "name": "Pesquisador",
            "kind": "atrd",
            "version": "2.1.0",
            "domain": "pesquisa",
        }

    def test_execution_log_e_total_steps_refletem_entradas(self):
        exec_log = [
            {"cat": "tools", "icon": "🛠️", "title": "Tavily (search)", "detail": "params: query", "level": "info"},
            {"cat": "result", "icon": "✓", "title": "Resultado", "detail": "2901ms · ok", "level": "success"},
        ]
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None,
            final_state="LogAndClose", duration_ms=2901,
            execution_log=exec_log,
        )
        # invariante anti-regressão: execution_log NUNCA volta vazio aqui
        assert tr["trace"]["execution_log"] == exec_log
        assert tr["trace"]["total_steps"] == 2
        assert len(tr["trace"]["execution_log"]) > 0

    def test_campos_top_level_para_rastreabilidade(self):
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill={"name": "Busca Web", "version": "1.0.0"},
            final_state="LogAndClose", duration_ms=1234,
            execution_log=[{"cat": "tools", "icon": "🛠️", "title": "x", "detail": "", "level": "info"}],
        )
        assert tr["final_state"] == "LogAndClose"
        assert tr["duration_ms"] == 1234
        assert tr["mode"] == "agent"
        assert tr["agent_id"] == "agent-1"
        assert tr["evidence_score"] == 0.0
        # frontend itera transitions/pipeline_steps — precisam existir e ser listas
        assert tr["transitions"] == []
        assert tr["pipeline_steps"] == []

    def test_skill_detail_quando_skill_none(self):
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None,
            final_state="LogAndClose", duration_ms=0,
            execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
        )
        sd = tr["trace"]["skill_detail"]
        assert sd["name"] == ""
        assert sd["execution_mode"] == "direct"

    def test_skill_detail_quando_skill_fornecida(self):
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill={"name": "Busca Web", "version": "3.2.1"},
            final_state="LogAndClose", duration_ms=0,
            execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
        )
        sd = tr["trace"]["skill_detail"]
        assert sd["name"] == "Busca Web"
        assert sd["version"] == "3.2.1"

    def test_mcp_tools_refletidos_no_trace(self):
        mcp = [{"name": "Tavily", "status": "completed", "server": "tavily", "latency_ms": 2901}]
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None,
            final_state="LogAndClose", duration_ms=2901,
            execution_log=[{"cat": "tools", "icon": "🛠️", "title": "Tavily", "detail": "", "level": "info"}],
            mcp_tools=mcp,
        )
        assert tr["trace"]["mcp_tools"] == mcp

    def test_api_tools_e_count_refletidos(self):
        api = [{"binding_id": "b1", "status_code": 200, "latency_ms": 12, "attempts": 1}]
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None,
            final_state="LogAndClose", duration_ms=12,
            execution_log=[{"cat": "api", "icon": "🌐", "title": "GET /x", "detail": "", "level": "success"}],
            api_tools=api, api_tools_count=1,
        )
        assert tr["trace"]["api_tools"] == api
        assert tr["trace"]["api_tools_count"] == 1
        # api_bindings_executed espelha api_tools (compat com /chat)
        assert tr["trace"]["api_bindings_executed"] == api

    def test_evidencias_rag(self):
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None,
            final_state="LogAndClose", duration_ms=80,
            execution_log=[{"cat": "evidence", "icon": "🔍", "title": "Busca RAG", "detail": "", "level": "info"}],
            evidence_count=3, evidence_sources=["Base A", "Base B"],
        )
        assert tr["trace"]["evidence_count"] == 3
        assert tr["trace"]["evidence_sources"] == ["Base A", "Base B"]

    def test_interaction_id_default_none_e_passa_quando_dado(self):
        tr1 = workspace._build_invoke_trace(
            agent=self._agent(), skill=None, final_state="LogAndClose",
            duration_ms=0, execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
        )
        assert tr1["interaction_id"] is None
        tr2 = workspace._build_invoke_trace(
            agent=self._agent(), skill=None, final_state="LogAndClose",
            duration_ms=0, execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
            interaction_id="iid-99",
        )
        assert tr2["interaction_id"] == "iid-99"

    def test_diagnostics_passados(self):
        diag = [{"level": "success", "text": "ok"}]
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None, final_state="LogAndClose",
            duration_ms=0, execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
            diagnostics=diag,
        )
        assert tr["trace"]["diagnostics"] == diag

    def test_serializavel_em_json(self):
        # trace_data é persistido via json.dumps — não pode ter tipos exóticos
        tr = workspace._build_invoke_trace(
            agent=self._agent(), skill=None, final_state="LogAndClose",
            duration_ms=10, execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
        )
        s = json.dumps(tr, ensure_ascii=False)
        assert isinstance(s, str) and len(s) > 0


# ═════════════════════════════════════════════════════════════════
# _persist_invoke_turn — persistência de trace_data (repos mockados)
# ═════════════════════════════════════════════════════════════════
class _FakeInteractionsRepo:
    def __init__(self, existing=None):
        self._existing = existing
        self.created: list[dict] = []
        self.updates: list[tuple[str, dict]] = []

    async def find_by_id(self, sid):
        return self._existing

    async def create(self, data):
        self.created.append(data)

    async def update(self, sid, data):
        self.updates.append((sid, data))


class _FakeTurnsRepo:
    def __init__(self):
        self.created: list[dict] = []

    async def find_all(self, interaction_id=None, limit=500):
        return []

    async def create(self, data):
        self.created.append(data)


def _patch_repos(monkeypatch, interactions, turns):
    monkeypatch.setattr("app.routes.workspace.interactions_repo", interactions)
    monkeypatch.setattr("app.routes.workspace.turns_repo", turns)


class TestPersistInvokeTurnTraceData:
    @pytest.mark.asyncio
    async def test_trace_data_persistido_quando_fornecido(self, monkeypatch):
        inter = _FakeInteractionsRepo(existing=None)  # sessão nova
        turns = _FakeTurnsRepo()
        _patch_repos(monkeypatch, inter, turns)

        trace_obj = workspace._build_invoke_trace(
            agent={"id": "a1", "name": "Bot"}, skill=None,
            final_state="LogAndClose", duration_ms=2901,
            execution_log=[
                {"cat": "tools", "icon": "🛠️", "title": "Tavily (search)", "detail": "", "level": "info"},
                {"cat": "result", "icon": "✓", "title": "Resultado", "detail": "2901ms · ok", "level": "success"},
            ],
        )

        sid = await workspace._persist_invoke_turn(
            session_id="",
            message="🛠️ Tavily (search) · query=x",
            output_text="```json\n[]\n```",
            agent_id="a1",
            title_fallback="Invocação · Tavily",
            trace_data=trace_obj,
        )

        assert sid  # devolve o id da sessão criada
        # gravou 1 interaction + 2 turns
        assert len(inter.created) == 1
        assert len(turns.created) == 2
        # gravou trace_data exatamente 1x
        trace_updates = [u for u in inter.updates if "trace_data" in u[1]]
        assert len(trace_updates) == 1
        upd_sid, upd_payload = trace_updates[0]
        assert upd_sid == sid
        # round-trip do JSON persistido
        persisted = json.loads(upd_payload["trace_data"])
        assert persisted["interaction_id"] == sid  # carimbado com o id real
        assert len(persisted["trace"]["execution_log"]) == 2
        assert persisted["final_state"] == "LogAndClose"

    @pytest.mark.asyncio
    async def test_sem_trace_data_nao_grava_trace_legacy(self, monkeypatch):
        inter = _FakeInteractionsRepo(existing=None)  # sessão nova
        turns = _FakeTurnsRepo()
        _patch_repos(monkeypatch, inter, turns)

        sid = await workspace._persist_invoke_turn(
            session_id="",
            message="oi",
            output_text="resposta",
            agent_id="a1",
            title_fallback="x",
            # trace_data omitido → comportamento legado
        )

        assert sid
        # nenhum update com trace_data (sessão nova nem chama update)
        assert not any("trace_data" in u[1] for u in inter.updates)

    @pytest.mark.asyncio
    async def test_mensagem_vazia_nao_persiste_nada(self, monkeypatch):
        inter = _FakeInteractionsRepo(existing=None)
        turns = _FakeTurnsRepo()
        _patch_repos(monkeypatch, inter, turns)

        sid = await workspace._persist_invoke_turn(
            session_id="",
            message="",  # sem texto humano → não dá pra reconstruir a bolha
            output_text="resposta",
            agent_id="a1",
            title_fallback="x",
            trace_data={"foo": "bar"},
        )

        assert sid is None
        assert inter.created == []
        assert turns.created == []
        assert inter.updates == []

    @pytest.mark.asyncio
    async def test_sessao_existente_persiste_trace_data(self, monkeypatch):
        inter = _FakeInteractionsRepo(existing={"id": "sess-1", "agent_id": "a1"})
        turns = _FakeTurnsRepo()
        _patch_repos(monkeypatch, inter, turns)

        trace_obj = workspace._build_invoke_trace(
            agent={"id": "a1", "name": "Bot"}, skill=None,
            final_state="LogAndClose", duration_ms=10,
            execution_log=[{"cat": "x", "icon": "", "title": "y", "detail": "", "level": "info"}],
        )

        sid = await workspace._persist_invoke_turn(
            session_id="sess-1",
            message="reusar sessão",
            output_text="ok",
            agent_id="a1",
            title_fallback="x",
            trace_data=trace_obj,
        )

        assert sid == "sess-1"
        # sessão existente: update de state/ended_at + update de trace_data
        trace_updates = [u for u in inter.updates if "trace_data" in u[1]]
        assert len(trace_updates) == 1
        persisted = json.loads(trace_updates[0][1]["trace_data"])
        assert persisted["interaction_id"] == "sess-1"
