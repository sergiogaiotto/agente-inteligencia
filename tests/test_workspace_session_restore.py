"""Restore de sessão antiga no Workspace (2026-06-01).

User reportou via screenshot: ao clicar em sessão antiga na sidebar, o
painel "Rastreabilidade" mostra "undefinedms / 0 transições / 0 evidências"
e Execution Log fica vazio mesmo a sessão tendo conteúdo. Causas:

1. Backend (`GET /workspace/sessions/{id}`) retornava `trace_data` cru,
   às vezes com campos faltando (sessões antigas pré-hardening). Frontend
   acessava `lastTrace.duration_ms?.toFixed(0)+'ms'` que, com campo
   undefined, virava literal string `"undefinedms"`.
2. Sem distinção entre "sessão sem trace algum" vs "trace minimalista
   sem detalhe (FSM/execution_log)". UI tratava ambos igual e mostrava
   placeholders quebrados.
3. Erros silenciosos: JSON.parse de trace_data inválido era engolido
   sem log, dificultando troubleshooting.

Fix (este PR):
- Backend estabiliza campos críticos (final_state, duration_ms, mode,
  transitions, evidence_score) com defaults seguros antes de devolver
- Backend marca `_has_real_trace` baseado em transitions/pipeline_steps/
  execution_log para o frontend escolher template
- Backend loga `workspace.session.trace_data_parse_failed` e
  `workspace.chat.trace_persist_failed` com exc_info=True no errors.log
- Frontend tem 3 templates distintos: empty state (sem sessão),
  "sessão antiga sem rastreabilidade", "trace parcial", "trace completo"
- Frontend usa fallback `(duration_ms||0)` no toFixed para evitar
  "undefinedms"
- Frontend toggle Agente/Pipeline deriva primeiro de session.agent_id
  (sempre persistido) e só depois consulta trace.mode
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def workspace_client():
    from app.routes.workspace import router
    from app.core.auth import require_user
    app = FastAPI()
    app.include_router(router)
    # get_session agora exige auth (IDOR 33.13.0). Override com um root de teste;
    # o gate de posse fail-opens sem DB (owner None → passa) e root bypassa.
    app.dependency_overrides[require_user] = lambda: {"id": "test-owner", "role": "root"}
    return TestClient(app)


def _patch_session(monkeypatch, *, session_row, turns=None):
    """Mocka o repo para retornar uma sessão específica + turns vazios."""
    async def fake_find_by_id(sid):
        return session_row if (session_row and session_row.get("id") == sid) else None

    async def fake_turns_find_all(limit=200, **filters):
        return turns or []

    monkeypatch.setattr("app.core.database.interactions_repo.find_by_id", fake_find_by_id)
    monkeypatch.setattr("app.core.database.turns_repo.find_all", fake_turns_find_all)


# ─── GET /sessions/{id} — comportamento de hardening ─────────────────


class TestGetSessionTraceHardening:
    SESSION_ID = "sess-001"
    AGENT_ID = "agent-xyz"

    def _base_session(self, *, trace_data: str | None, state: str = "LogAndClose"):
        return {
            "id": self.SESSION_ID,
            "agent_id": self.AGENT_ID,
            "title": "test session",
            "state": state,
            "trace_data": trace_data,
        }

    def test_trace_data_null_returns_synthetic_trace_with_defaults(self, monkeypatch, workspace_client):
        """Sessão antiga sem trace_data persistido — backend retorna trace
        SINTÉTICO com defaults preenchidos (3ª revisão 2026-06-01).
        User quer painéis Rastreabilidade + Execution Log SEMPRE visíveis,
        então frontend confia que trace existe sempre que sessão existe."""
        _patch_session(monkeypatch, session_row=self._base_session(trace_data=None))
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        assert r.status_code == 200
        trace = r.json()["trace"]
        # Não é mais None — vem como objeto com defaults preenchidos
        assert trace is not None
        assert trace["duration_ms"] == 0
        assert trace["transitions"] == []
        assert trace["pipeline_steps"] == []
        assert trace["mode"] == "agent"
        assert trace["interaction_id"] == self.SESSION_ID
        assert trace["agent_id"] == self.AGENT_ID
        # Marcador opcional indica que trace é "placeholder" (não real)
        assert trace["_has_real_trace"] is False

    def test_trace_data_empty_json_returns_stabilized_trace(self, monkeypatch, workspace_client):
        """trace_data='{}' — após hardening, vem com defaults preenchidos
        e _has_real_trace=False (UI mostra "parcial")."""
        _patch_session(monkeypatch, session_row=self._base_session(trace_data="{}"))
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        body = r.json()
        trace = body["trace"]
        assert trace is not None
        # Defaults estabilizados pelo backend
        assert trace["duration_ms"] == 0
        assert trace["transitions"] == []
        assert trace["evidence_score"] == 0
        assert trace["pipeline_steps"] == []
        assert trace["mode"] == "agent"  # default seguro
        assert trace["interaction_id"] == self.SESSION_ID
        assert trace["agent_id"] == self.AGENT_ID
        # Marcador "trace raso" para o frontend
        assert trace["_has_real_trace"] is False

    def test_invalid_json_logs_warning_with_exc_info(self, monkeypatch, workspace_client, caplog):
        """trace_data='{not json' — log warning com exc_info=True
        (errors.log). Backend NÃO retorna trace=null (3ª revisão 2026-06-01:
        trace sempre não-null pra UI sempre renderizar painéis); retorna
        trace sintético com defaults vazios."""
        _patch_session(
            monkeypatch,
            session_row=self._base_session(trace_data="{ not valid json"),
        )
        with caplog.at_level(logging.WARNING, logger="app.routes.workspace"):
            r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        # Trace agora vem sintético, não null
        trace = r.json()["trace"]
        assert trace is not None
        assert trace["transitions"] == []
        assert trace["_has_real_trace"] is False
        # Achou o log estruturado com exc_info
        rec = next(
            (r for r in caplog.records
             if getattr(r, "event", None) == "workspace.session.trace_data"),
            None,
        )
        assert rec is not None, "esperava log workspace.session.trace_data"
        assert rec.exc_info is not None  # exc_info=True preservado
        assert getattr(rec, "session_id", None) == self.SESSION_ID

    def test_trace_with_transitions_marks_has_real_trace_true(self, monkeypatch, workspace_client):
        """Sessão com FSM transitions registradas → _has_real_trace=True
        (frontend renderiza o painel completo)."""
        trace = {
            "interaction_id": self.SESSION_ID,
            "agent_id": self.AGENT_ID,
            "final_state": "LogAndClose",
            "duration_ms": 4321,
            "evidence_score": 0.85,
            "transitions": [
                {"to": "PolicyCheck"},
                {"to": "DraftAnswer"},
                {"to": "LogAndClose"},
            ],
            "trace": {"execution_log": [], "evidence_count": 0},
        }
        _patch_session(monkeypatch, session_row=self._base_session(trace_data=json.dumps(trace)))
        body = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}").json()
        assert body["trace"]["_has_real_trace"] is True

    def test_trace_with_pipeline_steps_marks_has_real_trace_true(self, monkeypatch, workspace_client):
        """Pipeline session com steps registrados → _has_real_trace=True
        e mode='pipeline' (mesmo que não veio explícito no payload)."""
        trace = {
            "interaction_id": self.SESSION_ID,
            "pipeline_steps": [
                {"agent_id": "a1", "agent_name": "AOBD", "status": "completed"},
                {"agent_id": "a2", "agent_name": "AR", "status": "completed"},
            ],
        }
        _patch_session(monkeypatch, session_row=self._base_session(trace_data=json.dumps(trace)))
        trace_out = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}").json()["trace"]
        assert trace_out["_has_real_trace"] is True
        assert trace_out["mode"] == "pipeline"  # derivado pela presença de steps

    def test_trace_with_execution_log_only_marks_has_real_trace_true(self, monkeypatch, workspace_client):
        """Trace com `trace.execution_log` mesmo sem transitions/pipeline_steps
        → considerado real (operador vê os passos no Execution Log)."""
        trace = {
            "interaction_id": self.SESSION_ID,
            "trace": {
                "execution_log": [
                    {"title": "Agent Bootstrapped", "cat": "init"},
                ],
            },
        }
        _patch_session(monkeypatch, session_row=self._base_session(trace_data=json.dumps(trace)))
        assert workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}").json()["trace"]["_has_real_trace"] is True

    def test_trace_minimal_dict_marks_has_real_trace_false(self, monkeypatch, workspace_client):
        """Trace com só agent_id+final_state, sem transitions nem steps nem
        execution_log → _has_real_trace=False (UI mostra "parcial")."""
        trace = {
            "interaction_id": self.SESSION_ID,
            "agent_id": self.AGENT_ID,
            "final_state": "LogAndClose",
            "duration_ms": 1234,
        }
        _patch_session(monkeypatch, session_row=self._base_session(trace_data=json.dumps(trace)))
        out = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}").json()["trace"]
        assert out["_has_real_trace"] is False
        # Duração original foi preservada (não sobrescrita pelo default)
        assert out["duration_ms"] == 1234

    def test_trace_with_none_duration_gets_default_zero(self, monkeypatch, workspace_client):
        """Bug clássico do screenshot: `duration_ms=null` → frontend
        renderizava "undefinedms". Backend agora normaliza pra 0."""
        trace = {
            "final_state": "LogAndClose",
            "duration_ms": None,  # explicit None
            "transitions": None,
            "evidence_score": None,
        }
        _patch_session(monkeypatch, session_row=self._base_session(trace_data=json.dumps(trace)))
        out = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}").json()["trace"]
        assert out["duration_ms"] == 0
        assert out["transitions"] == []
        assert out["evidence_score"] == 0

    def test_non_dict_trace_data_replaced_by_synthetic(self, monkeypatch, workspace_client):
        """trace_data='[1,2,3]' (JSON válido mas não dict) → backend
        descarta e devolve trace sintético (em vez de null) pra UI sempre
        renderizar painéis (3ª revisão 2026-06-01)."""
        _patch_session(monkeypatch, session_row=self._base_session(trace_data='[1,2,3]'))
        trace = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}").json()["trace"]
        assert trace is not None
        assert trace["_has_real_trace"] is False
        assert trace["interaction_id"] == self.SESSION_ID


# ─── UI smoke (workspace.html) ───────────────────────────────────────


def _workspace_html() -> str:
    p = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html"
    return p.read_text(encoding="utf-8")


class TestWorkspaceUiHardening:
    """Smoke do source HTML — garante que os fixes ficaram cabeados no
    template e que regressões não removeriam o tratamento dos campos null."""

    def test_duration_ms_has_fallback_to_zero(self):
        """Antes: `lastTrace.duration_ms?.toFixed(0)+'ms'` virava 'undefinedms'
        quando o campo faltava. Fix: usa `||0` antes do toFixed."""
        src = _workspace_html()
        assert "(lastTrace.duration_ms||0).toFixed(0)" in src
        # Garante que a versão buggy não voltou (regressão guard)
        assert "lastTrace.duration_ms?.toFixed(0)+'ms'" not in src

    def test_two_states_for_trace_panel(self):
        """Painel agora tem APENAS 2 estados (3ª revisão 2026-06-01):
        1. Sem sessão aberta → empty state inicial
        2. lastTrace presente (backend SEMPRE retorna trace para sessão
           aberta, sintético ou real) → painel completo

        User pediu explicitamente: painéis SEMPRE visíveis com TODAS as
        informações pensadas. Removeu-se o estado "Sessão sem
        rastreabilidade detalhada" porque o backend agora garante trace
        sintético com defaults pra qualquer sessão aberta."""
        src = _workspace_html()
        # 1. Empty (sem sessão aberta)
        assert "!lastTrace && !currentSessionId" in src
        # 2. lastTrace presente — SEMPRE renderiza painel completo
        assert '<template x-if="lastTrace">' in src

    def test_no_more_hidden_panel_for_old_sessions(self):
        """Regressão guard: as duas mensagens de "sessão sem trace" que
        escondiam painéis foram removidas em 2026-06-01."""
        src = _workspace_html()
        assert "Sessão sem rastreabilidade detalhada" not in src
        assert "Rastreabilidade parcial" not in src
        # `!lastTrace && currentSessionId` não deve existir como condicional
        # (era usado pra esconder o painel direito)
        assert "!lastTrace && currentSessionId" not in src

    def test_no_more_partial_trace_banner(self):
        """Regressão guard: `_has_real_trace` continua sendo emitido no
        payload do backend (info diagnóstica), mas NÃO pode ser usado em
        x-if que esconda painel principal."""
        src = _workspace_html()
        for line in src.split("\n"):
            stripped = line.strip()
            if "_has_real_trace" not in stripped:
                continue
            # Aceita comentário HTML / JS, mas não x-if condicional
            assert "x-if=" not in stripped, (
                f"_has_real_trace não pode condicionar x-if (esconde painel): {stripped}"
            )

    def test_execution_log_always_visible_with_session(self):
        """User pediu (2026-06-01, 3ª iteração): painel Execution Log
        SEMPRE visível quando há sessão aberta, mesmo que vazio. Antes:
        `x-show="liveLog.length > 0"` escondia painel quando trace antigo
        não tinha execution_log. Agora: `x-show="currentSessionId || liveLog.length > 0"`."""
        src = _workspace_html()
        assert 'x-show="currentSessionId || liveLog.length > 0"' in src
        # Regressão guard: condição antiga foi removida
        assert 'x-show="liveLog.length > 0" x-transition' not in src

    def test_execution_log_empty_state_message(self):
        """Quando há sessão mas log vazio, mostrar mensagem suave
        explicando ("Sem entradas registradas...") em vez de painel oco."""
        src = _workspace_html()
        assert "Sem entradas registradas para esta execução" in src
        assert 'x-show="liveLog.length === 0"' in src

    def test_final_state_has_fallback_label(self):
        """Quando final_state é null/undefined, mostrar 'Concluído' em vez
        de literal undefined no header."""
        src = _workspace_html()
        # O fallback `||'Concluído'` foi acoplado ao mostrar final_state
        assert "(lastTrace.final_state||'Concluído')" in src

    def test_pipeline_step_bubble_skips_empty_content(self):
        """#3/#6: um router intermediário decision-only recebe output_display=""
        do engine → o loop de render PULA o balão vazio (em vez de mostrar bolha
        vazia ou a linha DECISAO crua)."""
        src = _workspace_html()
        assert "if(!_content) continue;" in src

    def test_load_session_prefers_session_agent_id(self):
        """selectedAgentId prioriza session.agent_id (sempre persistido)
        sobre pipeline_steps[0].agent_id — evita ficar undefined em
        sessões antigas sem pipeline_steps."""
        src = _workspace_html()
        # A nova lógica usa session.agent_id como primeira fonte
        assert "const sessionAgentId = d.session?.agent_id || ''" in src
        # E mantém os fallbacks
        assert "sessionAgentId || pipelineSteps[0]?.agent_id || d.trace?.agent_id" in src

    def test_load_session_catch_has_console_error(self):
        """Reforço (2026-06-01): catch silencioso ganhou console.error
        com contexto (sessionId + error) pra troubleshooting no DevTools."""
        src = _workspace_html()
        assert "console.error('[workspace] loadSession falhou'" in src


class TestEntryModeAutoDetectsPipeline:
    """2026-06-06: bug frequente — clicar em "Testar"/play num Roteador(AR) ou
    Orquestrador(AOBD) na tela de Agentes abria o Workspace em modo AGENTE
    (o link é `/workspace?agent=<id>` SEM &mode=), rodando só o prompt da raiz
    e PULANDO o fan-out — os subagentes nunca eram acionados. O modo decide o
    backend (workspace.py: só `mode=='pipeline'` chama execute_pipeline).

    Fix: o caminho de entrada `?agent=` passa a AUTO-DETECTAR — se o agente é
    RAIZ de um mesh (pipelineRoots, chainLen>1), abre em Pipeline; folha abre
    em Agente. Reusa o MESMO sinal (pipelineRoots) que loadSession já usava
    via isPipelineRoot — play e reabrir-sessão não divergem mais de modo. Um
    `?mode=` explícito na URL continua tendo prioridade (override manual)."""

    def test_entry_autodetects_pipeline_for_root(self):
        src = _workspace_html()
        # isRoot derivado da topologia já carregada (pipelineRoots)
        assert "const isRoot=(this.pipelineRoots||[]).some(pr=>pr.id===aid);" in src
        # ?mode= explícito vence; senão raiz→pipeline, folha→agent
        assert "this.execMode=p.get('mode')||(isRoot?'pipeline':'agent');" in src

    def test_old_unconditional_agent_default_removed(self):
        """Regressão guard: a linha antiga que SEMPRE caía em 'agent' sumiu."""
        src = _workspace_html()
        assert "this.execMode=p.get('mode')||'agent'" not in src

    def test_pipeline_roots_loaded_before_url_param_handling(self):
        """A auto-detecção depende de pipelineRoots já estar populado quando o
        `?agent=` é processado. Garante a ordem em load(): loadPipelineRoots()
        é AGUARDADO antes de ler os params da URL."""
        src = _workspace_html()
        i_roots = src.find("await this.loadPipelineRoots()")
        i_params = src.find("const p=new URLSearchParams(window.location.search)")
        assert i_roots != -1 and i_params != -1
        assert i_roots < i_params, "loadPipelineRoots deve ser aguardado antes de ler ?agent="

    def test_entry_consistent_with_loadsession_detection(self):
        """Simetria: loadSession marca pipeline quando o agente da sessão é
        raiz (isPipelineRoot). A entrada `?agent=` usa o MESMO sinal
        (pipelineRoots), evitando que play e reabrir-sessão divirjam."""
        src = _workspace_html()
        # loadSession continua com a detecção por raiz (a fonte que reusamos)
        assert "const isPipelineRoot = (this.pipelineRoots || []).some(p => p.id === sessionAgentId);" in src
        # entrada ?agent= também consulta pipelineRoots
        assert "(this.pipelineRoots||[]).some(pr=>pr.id===aid)" in src


# ─── Cond-C (35.19.0): linha DECISAO não volta no histórico ──────────────────


class TestGetSessionDecisionLineStrip:
    """O banco guarda o output CRU (auditoria); o balão do histórico é RESPOSTA
    APRESENTADA — mesmo strip do /chat vivo, senão a linha DECISAO reaparece ao
    recarregar a sessão (achado do review do plano Cond-C)."""

    SESSION_ID = "sess-dec"

    def _session(self):
        return {
            "id": self.SESSION_ID, "agent_id": "ag-contrato", "title": "t",
            "state": "LogAndClose", "trace_data": None,
        }

    def _turns(self, output):
        return [{
            "user_text_redacted": "Meu cartão foi clonado.",
            "output_text_redacted": output, "created_at": "",
        }]

    def test_balao_sem_linha_quando_agente_tem_contrato(self, workspace_client, monkeypatch):
        _patch_session(
            monkeypatch, session_row=self._session(),
            turns=self._turns("Entendi o problema do cartão.\n\nDECISAO: escalar=sim"),
        )

        async def _schema(_id):
            return {"escalar": ["sim", "não"]}

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        assert r.status_code == 200
        assistant = [m for m in r.json()["messages"] if m["role"] == "assistant"]
        assert assistant[0]["content"] == "Entendi o problema do cartão."

    def test_prosa_decisao_fica_quando_agente_sem_contrato(self, workspace_client, monkeypatch):
        # gate duplo: sem schema o strip é no-op — prosa legítima não é amputada
        raw = "Análise concluída.\nDecisão: aprovado o crédito"
        _patch_session(monkeypatch, session_row=self._session(), turns=self._turns(raw))

        async def _schema(_id):
            return None

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        assistant = [m for m in r.json()["messages"] if m["role"] == "assistant"]
        assert assistant[0]["content"] == raw

    def test_pipeline_strippa_por_autor_do_balao(self, workspace_client, monkeypatch):
        # MAJOR do review pré-push: numa sessão de PIPELINE as turns são por
        # step — o schema tem que ser o do AUTOR do balão (agente meio-de-cadeia
        # com contrato), não o do agente de entrada.
        trace = {"pipeline_steps": [{"agent_id": "ag-A"}, {"agent_id": "ag-B"}]}
        session = {
            "id": self.SESSION_ID, "agent_id": "ag-A", "title": "t",
            "state": "LogAndClose", "trace_data": json.dumps(trace),
        }
        # repo devolve mais-recente-primeiro; o handler reverte p/ cronológica
        turns = [
            {"user_text_redacted": None,
             "output_text_redacted": "Caso grave.\nDECISAO: escalar=sim", "created_at": ""},
            {"user_text_redacted": "Meu cartão foi clonado.",
             "output_text_redacted": "Roteado para triagem.", "created_at": ""},
        ]
        _patch_session(monkeypatch, session_row=session, turns=turns)

        async def _schema(aid):
            return {"escalar": ["sim", "não"]} if aid == "ag-B" else None

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        assistant = [m for m in r.json()["messages"] if m["role"] == "assistant"]
        assert assistant[0]["content"] == "Roteado para triagem."
        assert assistant[1]["content"] == "Caso grave."

    def test_eco_de_upstream_em_balao_sem_contrato_e_strippado(self, workspace_client, monkeypatch):
        # 36.1.0 (borda do review): o agente final SEM contrato ecoou a linha do
        # upstream — o strip do histórico tenta os schemas dos DEMAIS steps.
        trace = {"pipeline_steps": [{"agent_id": "ag-B"}, {"agent_id": "ag-C"}]}
        session = {
            "id": self.SESSION_ID, "agent_id": "ag-B", "title": "t",
            "state": "LogAndClose", "trace_data": json.dumps(trace),
        }
        turns = [
            {"user_text_redacted": None,  # balão do agente C (sem contrato, ECOA a linha de B)
             "output_text_redacted": "Resolvido conforme triagem.\nDECISAO: escalar=sim", "created_at": ""},
            {"user_text_redacted": "u",
             "output_text_redacted": "Caso grave.\nDECISAO: escalar=sim", "created_at": ""},
        ]
        _patch_session(monkeypatch, session_row=session, turns=turns)

        async def _schema(aid):
            return {"escalar": ["sim", "não"]} if aid == "ag-B" else None

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        assistant = [m for m in r.json()["messages"] if m["role"] == "assistant"]
        assert assistant[0]["content"] == "Caso grave."                      # autor B (contrato)
        assert assistant[1]["content"] == "Resolvido conforme triagem."      # eco em C, strippado via schema de B

    def test_router_terminal_decision_only_vira_recusa(self, workspace_client, monkeypatch):
        # #4: pipeline que termina no router (owner) cujo balão é só a linha
        # DECISAO → recusa amigável no histórico, consistente com o vivo (F5).
        from app.agents.engine import _TERMINAL_DECISION_FALLBACK
        trace = {"pipeline_steps": [{"agent_id": "ag-router"}],
                 "output_agent": {"id": "ag-router"}}
        session = {"id": self.SESSION_ID, "agent_id": "ag-router", "title": "t",
                   "state": "LogAndClose", "trace_data": json.dumps(trace)}
        turns = [{"user_text_redacted": "qual a capital da França?",
                  "output_text_redacted": "DECISAO: categoria=fora_de_escopo; risco=normal",
                  "created_at": ""}]
        _patch_session(monkeypatch, session_row=session, turns=turns)

        async def _schema(aid):
            return {"categoria": ["fora_de_escopo", "comprador"], "risco": ["normal", "alto"]}

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        assistant = [m for m in r.json()["messages"] if m["role"] == "assistant"]
        assert assistant[0]["content"] == _TERMINAL_DECISION_FALLBACK
        assert "DECISAO" not in assistant[0]["content"]

    def test_router_intermediario_decision_only_suprimido(self, workspace_client, monkeypatch):
        # #3/#6: router INTERMEDIÁRIO decision-only → balão suprimido no histórico
        # (não vaza a linha crua nem anuncia "não consegui"); o especialista
        # (owner) responde. Consistente com o vivo (workspace.html pula vazio).
        trace = {"pipeline_steps": [{"agent_id": "ag-router"}, {"agent_id": "ag-esp"}],
                 "output_agent": {"id": "ag-esp"}}
        session = {"id": self.SESSION_ID, "agent_id": "ag-router", "title": "t",
                   "state": "LogAndClose", "trace_data": json.dumps(trace)}
        # repo devolve mais-recente-primeiro; o handler reverte p/ cronológica
        turns = [
            {"user_text_redacted": None,
             "output_text_redacted": "Vou resolver sua devolução agora.", "created_at": ""},  # esp (owner)
            {"user_text_redacted": "quero devolver",
             "output_text_redacted": "DECISAO: categoria=comprador; risco=normal", "created_at": ""},  # router
        ]
        _patch_session(monkeypatch, session_row=session, turns=turns)

        async def _schema(aid):
            return ({"categoria": ["comprador", "fora_de_escopo"], "risco": ["normal", "alto"]}
                    if aid == "ag-router" else None)

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        msgs = r.json()["messages"]
        assistant = [m for m in msgs if m["role"] == "assistant"]
        # só UM balão assistant (o do especialista); o router foi suprimido
        assert len(assistant) == 1
        assert assistant[0]["content"] == "Vou resolver sua devolução agora."
        assert all("DECISAO" not in m["content"] for m in msgs)

    def test_router_terminal_fallback_sobrevive_step_pulado_antes(self, workspace_client, monkeypatch):
        # 2ª revisão adversarial (Trigger A): um step skipped/passthrough ANTES do
        # owner desalinha o índice posicional turn↔pipeline_steps. A resposta
        # terminal decision-only NÃO pode SUMIR — deve virar a recusa amigável
        # (decidido pelo "último output cronológico", não pelo índice de step).
        from app.agents.engine import _TERMINAL_DECISION_FALLBACK
        trace = {
            "pipeline_steps": [{"agent_id": "ag-B"}, {"agent_id": "ag-skip"}, {"agent_id": "ag-C"}],
            "output_agent": {"id": "ag-C"},
        }
        session = {"id": self.SESSION_ID, "agent_id": "ag-B", "title": "t",
                   "state": "LogAndClose", "trace_data": json.dumps(trace)}
        # recent-first: C (terminal, decision-only), B (entry, prosa que roteia).
        # ag-skip NÃO tem turn persistido (é o cenário do desalinhamento).
        turns = [
            {"user_text_redacted": None,
             "output_text_redacted": "DECISAO: categoria=fora_de_escopo; risco=normal", "created_at": ""},
            {"user_text_redacted": "pergunta fora de escopo",
             "output_text_redacted": "Encaminhando.", "created_at": ""},
        ]
        _patch_session(monkeypatch, session_row=session, turns=turns)

        async def _schema(aid):
            return ({"categoria": ["fora_de_escopo", "comprador"], "risco": ["normal", "alto"]}
                    if aid == "ag-C" else None)

        monkeypatch.setattr("app.agents.engine._decisions_schema_for_agent", _schema)
        r = workspace_client.get(f"/api/v1/workspace/sessions/{self.SESSION_ID}")
        msgs = r.json()["messages"]
        assistant = [m for m in msgs if m["role"] == "assistant"]
        # a resposta terminal NÃO sumiu: virou a recusa amigável (não "" nem crua)
        assert assistant[-1]["content"] == _TERMINAL_DECISION_FALLBACK
        assert all("DECISAO" not in m["content"] for m in msgs)
