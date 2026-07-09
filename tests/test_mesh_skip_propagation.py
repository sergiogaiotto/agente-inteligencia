"""Propagação de skip pela cadeia + reativação por inbound misto (29.1.13).

Achados das baterias E2E "Hélios" e "Arca" (2026-07-09) — o gate condicional
avaliava SÓ a aresta do parent BFS (primeiro-marca-vence do `parent_of`):

N1 ("Hélios"): nó com entradas MISTAS — conditional NÃO-casada (de uma triagem)
+ sequential vinda de um nó que EXECUTOU — ficava `skipped_conditional` e a
cadeia sequencial não o reativava. Qual aresta mandava dependia da ordem de
descoberta da BFS, não da modelagem.

N2 ("Arca", 2× reproduzido): nó cujo ÚNICO inbound é sequential de um nó
PULADO caía no fallback linear (last_result) e RODAVA com o output do último
executado — um nó NÃO conectado a ele. Pior: sendo o último da ordem
topológica, o output dele virava a RESPOSTA FINAL do pipeline, sobrepondo a
resposta correta do especialista roteado (pergunta de vacinas respondida com
instruções de transporte).

Semântica nova (multi-inbound): o nó roda se QUALQUER aresta inbound disparar —
sequential/parallel de source executada dispara sempre; conditional se casar;
default se nenhum irmão casou; aresta de source PULADA nunca dispara. Sem
disparo nenhum: `skipped_conditional`/`skipped_default` quando havia aresta
viva não-casada; novo `skipped_upstream` (skip_reason=upstream_skipped) quando
TODAS as sources inbound foram puladas — propagando recursivamente.
"""
from __future__ import annotations

import json

import pytest


# ─── helpers de mock (mesmo padrão de test_mesh_fanout_routing) ─────


def _agent(aid: str, name: str) -> dict:
    return {
        "id": aid, "name": name, "status": "active", "kind": "subagent",
        "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "prompt real",
    }


def _conn(src: str, tgt: str, *, ctype: str = "sequential", expr: str | None = None) -> dict:
    cfg = json.dumps({"expr": expr}) if expr is not None else "{}"
    return {
        "source_agent_id": src, "target_agent_id": tgt,
        "connection_type": ctype, "config": cfg,
    }


def _patch_topology(monkeypatch, conns_by_source: dict):
    async def fake_find_all(source_agent_id=None, limit=20, **_):
        return list(conns_by_source.get(source_agent_id, []))
    monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)


def _patch_agents(monkeypatch, agents: dict):
    async def fake_find_by_id(aid):
        return agents.get(aid)
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)


def _patch_executions(monkeypatch):
    invoked = []

    async def fake_exec(*, agent_id, user_input, channel="api", attachments=None,
                        pipeline_context=None, session_id=None, **_):
        invoked.append({
            "agent_id": agent_id,
            "user_input": user_input,
            "pipeline_context": pipeline_context,
        })
        return {
            "output": f"output-of-{agent_id}",
            "final_state": "Recommend",
            "interaction_id": None,
            "duration_ms": 1,
            "evidence_score": 0,
            "transitions": [],
            "trace": {},
        }
    monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)
    return invoked


def _statuses(res: dict) -> dict:
    return {s["agent_id"]: s["status"] for s in res["pipeline_steps"]}


def _step(res: dict, aid: str) -> dict:
    return next(s for s in res["pipeline_steps"] if s["agent_id"] == aid)


# ─── N2: cadeia de nó PULADO → skip propaga (skipped_upstream) ──────


class TestSkipPropagatesDownChain:
    @pytest.mark.asyncio
    async def test_chain_target_of_skipped_node_is_skipped_upstream(self, monkeypatch):
        """T→A (conditional NÃO casa) e A→B (sequential). A é pulado; B tem A
        como ÚNICO inbound → B NÃO roda (antes rodava via fallback linear com
        o output de T, um nó não conectado a ele — o bug N2)."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Triagem"),
            "A": _agent("A", "Emergencia"),
            "B": _agent("B", "Transporte"),
        })
        _patch_topology(monkeypatch, {
            "T": [_conn("T", "A", ctype="conditional", expr="'emergencia' in input_lower")],
            "A": [_conn("A", "B")],  # sequential
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="quais vacinas o filhote precisa?")

        ran = [x["agent_id"] for x in invoked]
        assert ran == ["T"], "B (cadeia de nó pulado) não pode executar"
        st = _statuses(res)
        assert st["A"] == "skipped_conditional"
        assert st["B"] == "skipped_upstream"
        assert _step(res, "B")["skip_reason"] == "upstream_skipped"
        # o output final é o do último EXECUTADO, não o do nó da cadeia morta
        assert res["output"] == "output-of-T"

    @pytest.mark.asyncio
    async def test_skip_propagates_recursively(self, monkeypatch):
        """T→A (cond não casa), A→B (seq), B→C (seq): o skip desce a cadeia
        inteira — B e C viram skipped_upstream."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Triagem"), "A": _agent("A", "A"),
            "B": _agent("B", "B"), "C": _agent("C", "C"),
        })
        _patch_topology(monkeypatch, {
            "T": [_conn("T", "A", ctype="conditional", expr="'nunca-casa' in input_lower")],
            "A": [_conn("A", "B")],
            "B": [_conn("B", "C")],
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="qualquer pergunta")

        assert [x["agent_id"] for x in invoked] == ["T"]
        st = _statuses(res)
        assert st["A"] == "skipped_conditional"
        assert st["B"] == "skipped_upstream"
        assert st["C"] == "skipped_upstream"

    @pytest.mark.asyncio
    async def test_arca_shape_final_output_is_routed_specialist(self, monkeypatch):
        """A forma exata do achado N2 "Arca": fan-out T→V (casa) e T→E (não
        casa), com E→X (sequential). Antes, X rodava e — por ser o último da
        ordem topológica — o output DELE sobrepunha a resposta correta de V.
        Agora: V responde, E skipped_conditional, X skipped_upstream, e o
        output final do pipeline é o de V."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Plantao"),
            "V": _agent("V", "Vacinas"),
            "E": _agent("E", "Emergencia"),
            "X": _agent("X", "Transporte"),
        })
        _patch_topology(monkeypatch, {
            "T": [
                _conn("T", "V", ctype="conditional", expr="'vacina' in input_lower"),
                _conn("T", "E", ctype="conditional", expr="'emergencia' in input_lower"),
            ],
            "E": [_conn("E", "X")],  # sequential (Emergência→Transporte)
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="qual vacina meu gato precisa?")

        ran = [x["agent_id"] for x in invoked]
        assert "V" in ran and "X" not in ran
        st = _statuses(res)
        assert st["E"] == "skipped_conditional"
        assert st["X"] == "skipped_upstream"
        assert res["output"] == "output-of-V", (
            "a resposta final deve ser a do especialista roteado, "
            "não a do nó de cadeia morta (bug N2)"
        )


# ─── N1: inbound MISTO → cadeia válida reativa o nó ─────────────────


class TestMixedInboundReactivation:
    @pytest.mark.asyncio
    async def test_valid_chain_reactivates_conditionally_preempted_node(self, monkeypatch):
        """T→S (cond casa), T→F (cond NÃO casa), S→F (sequential). O parent
        BFS de F é T (primeiro descobridor) e a expr não casa — antes F ficava
        skipped_conditional. Agora a cadeia S→F (S executou) dispara e F roda,
        com o CONTEXTO de S (a aresta que de fato disparou)."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Triagem"),
            "S": _agent("S", "Tecnico"),
            "F": _agent("F", "Faturamento"),
        })
        _patch_topology(monkeypatch, {
            "T": [
                _conn("T", "S", ctype="conditional", expr="'tecnico' in input_lower"),
                _conn("T", "F", ctype="conditional", expr="'fatura' in input_lower"),
            ],
            "S": [_conn("S", "F")],  # sequential (Técnico→Faturamento)
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="problema tecnico no inversor")

        by_id = {x["agent_id"]: x for x in invoked}
        assert "F" in by_id, "cadeia sequencial válida deve reativar o nó preterido"
        assert by_id["F"]["pipeline_context"] == "output-of-S"
        assert "output-of-S" in by_id["F"]["user_input"]
        assert _statuses(res)["F"] == "completed"
        # display honesto: o mesh_chain aponta a aresta que DISPAROU (S→F seq)
        mesh = {m["id"]: m for m in res["trace"]["mesh_chain"]}
        assert mesh["F"]["connection"] == "sequential"

    @pytest.mark.asyncio
    async def test_order_independent_when_chain_edge_is_the_bfs_parent(self, monkeypatch):
        """Espelho do anterior: o parent BFS de F é a própria aresta sequencial
        (A→F) e a conditional não-casada (T→F) chega por outra source. F roda
        do mesmo jeito — a decisão não depende da ordem de descoberta da BFS."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "E": _agent("E", "Entry"), "A": _agent("A", "Tecnico"),
            "T": _agent("T", "Triagem"), "F": _agent("F", "Faturamento"),
        })
        _patch_topology(monkeypatch, {
            "E": [_conn("E", "A"), _conn("E", "T")],
            "A": [_conn("A", "F")],  # BFS descobre F por A → parent = aresta seq
            "T": [_conn("T", "F", ctype="conditional", expr="'nunca-casa' in input_lower")],
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="E", user_input="qualquer pergunta")

        by_id = {x["agent_id"]: x for x in invoked}
        assert "F" in by_id
        assert by_id["F"]["pipeline_context"] == "output-of-A"
        assert _statuses(res)["F"] == "completed"

    @pytest.mark.asyncio
    async def test_dead_chain_does_not_reactivate(self, monkeypatch):
        """T→A (cond não casa) e T→F (cond não casa) + A→F (sequential): a
        cadeia vem de um nó PULADO → não dispara. F permanece
        skipped_conditional (não vira skipped_upstream: havia aresta viva de
        T, executada, que simplesmente não casou)."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Triagem"), "A": _agent("A", "A"), "F": _agent("F", "F"),
        })
        _patch_topology(monkeypatch, {
            "T": [
                _conn("T", "A", ctype="conditional", expr="'nunca1' in input_lower"),
                _conn("T", "F", ctype="conditional", expr="'nunca2' in input_lower"),
            ],
            "A": [_conn("A", "F")],
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="pergunta fora de escopo")

        assert [x["agent_id"] for x in invoked] == ["T"]
        st = _statuses(res)
        assert st["A"] == "skipped_conditional"
        assert st["F"] == "skipped_conditional"
        assert _step(res, "F")["skip_reason"] == "conditional_false"


# ─── regressão: cadeia de nó EXECUTADO segue rodando ────────────────


class TestChainOfExecutedNodeStillRuns:
    @pytest.mark.asyncio
    async def test_chain_target_of_executed_node_runs_with_its_context(self, monkeypatch):
        """T→A (cond casa) e A→B (sequential): A roda e a cadeia dispara B com
        o contexto de A — comportamento histórico preservado."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Triagem"), "A": _agent("A", "A"), "B": _agent("B", "B"),
        })
        _patch_topology(monkeypatch, {
            "T": [_conn("T", "A", ctype="conditional", expr="'sim' in input_lower")],
            "A": [_conn("A", "B")],
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="sim, quero prosseguir")

        by_id = {x["agent_id"]: x for x in invoked}
        assert list(by_id) == ["T", "A", "B"]
        assert by_id["B"]["pipeline_context"] == "output-of-A"
        assert _statuses(res)["B"] == "completed"
        assert res["output"] == "output-of-B"

    @pytest.mark.asyncio
    async def test_fanout_1_of_n_unchanged(self, monkeypatch):
        """Fan-out clássico sem cadeia: irmão não-casado segue
        skipped_conditional (nada de reativação sem aresta que dispare)."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "T": _agent("T", "Triagem"), "B": _agent("B", "B"), "C": _agent("C", "C"),
        })
        _patch_topology(monkeypatch, {"T": [
            _conn("T", "B", ctype="conditional", expr="'invest' in input_lower"),
            _conn("T", "C", ctype="conditional", expr="'suporte' in input_lower"),
        ]})
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="quero investir")

        ran = [x["agent_id"] for x in invoked]
        assert "B" in ran and "C" not in ran
        assert _statuses(res)["C"] == "skipped_conditional"


# ─── aviso de modelagem: _mixed_inbound (topologia → editor de fluxo) ─


class TestMixedInboundTopologyHint:
    def _edge(self, eid, src, tgt, etype, expr=None):
        return {"id": eid, "source": src, "target": tgt, "type": etype,
                "config": json.dumps({"expr": expr}) if expr else "{}"}

    def test_flags_target_with_conditional_and_sequential_inbound(self):
        from app.routes.mesh import _mixed_inbound
        edges = [
            self._edge("e1", "T", "F", "conditional", "'fatura' in input_lower"),
            self._edge("e2", "S", "F", "sequential"),
        ]
        assert _mixed_inbound(edges) == ["F"]

    def test_parallel_also_counts_as_chain(self):
        from app.routes.mesh import _mixed_inbound
        edges = [
            self._edge("e1", "T", "F", "conditional", "'x' in input_lower"),
            self._edge("e2", "S", "F", "parallel"),
        ]
        assert _mixed_inbound(edges) == ["F"]

    def test_pure_conditional_or_pure_chain_not_flagged(self):
        from app.routes.mesh import _mixed_inbound
        only_cond = [
            self._edge("e1", "T", "F", "conditional", "'x' in input_lower"),
            self._edge("e2", "T", "G", "conditional", "'y' in input_lower"),
        ]
        only_chain = [
            self._edge("e3", "A", "B", "sequential"),
            self._edge("e4", "B", "C", "sequential"),
        ]
        assert _mixed_inbound(only_cond) == []
        assert _mixed_inbound(only_chain) == []

    def test_default_edge_does_not_count_as_chain(self):
        from app.routes.mesh import _mixed_inbound
        edges = [
            self._edge("e1", "T", "F", "conditional", "'x' in input_lower"),
            self._edge("e2", "T", "F", "default"),
        ]
        assert _mixed_inbound(edges) == []
