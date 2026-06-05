"""Roteamento 1-de-N (fan-out / branch) no AI Mesh — Fix completo (2026-06-05).

CONTEXTO / bug que motivou:
O usuário montou AOBD → SA direto, o Composer criou a conexão como
`sequential`, e ele perguntou se a resposta não deveria ser CONDICIONAL
(a pergunta era específica: "qual a melhor forma de rentabilizar?") e se
ele não deveria ter usado uma Triagem (SR) antes do Especialista (SA).

Ao investigar, achei uma limitação do MOTOR (não só do Composer): o gate
condicional de `execute_pipeline` avaliava cada nó contra `chain[i-1]` — o
IRMÃO ANTERIOR na lista BFS — e não contra o PARENT REAL. Logo, num fan-out
(1 source → N filhos), só o PRIMEIRO filho era avaliado corretamente; do 2º
em diante o gate procurava uma conexão `irmão→filho` que NÃO existe → caía no
"não é conditional" → o filho rodava SEMPRE, ignorando a própria regra. Ou
seja: roteamento 1-de-N era impossível.

Fix:
- `_resolve_ordered_chain_with_parents` devolve `parent_of` (aresta real).
- `execute_pipeline` avalia gate/scope/input contra o PARENT REAL e o output
  DELE (outputs_by_id) — habilitando branch real.
- `_build_conditional_context` ganhou `input`/`input_lower` (a pergunta do
  usuário), pra ramificar pela PERGUNTA e não só pelo output do upstream.

Estes testes provam:
1. `parent_of` correto (fan-out, linear, diamante).
2. fan-out roteia 1-de-N de verdade — inclusive PULANDO o 2º+ filho cuja
   regra não casa (o caso que o motor antigo NUNCA pulava: a prova do fix).
3. cada filho deriva input/contexto do PARENT comum, não do irmão anterior.
4. cadeia linear segue avaliando contra o parent imediato (byte-idêntico).
5. o display `mesh_chain` mostra o tipo de conexão REAL (não crava sequential).
"""
from __future__ import annotations

import json

import pytest


# ─── helpers de mock ────────────────────────────────────────────────


def _agent(aid: str, name: str) -> dict:
    # skill_id setado → _is_passthrough False → o agente realmente "executa".
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
    """Captura cada chamada a execute_interaction. Output = 'output-of-<id>'
    (assim dá pra distinguir o output do PARENT do output do IRMÃO).
    interaction_id=None → pula toda a consolidação de sessão (sem DB)."""
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


# ─── _resolve_ordered_chain_with_parents ────────────────────────────


class TestResolveOrderedChainWithParents:
    @pytest.mark.asyncio
    async def test_fanout_all_children_point_to_common_source(self, monkeypatch):
        from app.agents import engine as eng
        _patch_topology(monkeypatch, {"A": [_conn("A", "B"), _conn("A", "C")]})

        chain, parent_of = await eng._resolve_ordered_chain_with_parents("A")

        assert chain == ["A", "B", "C"]
        assert parent_of == {"B": "A", "C": "A"}

    @pytest.mark.asyncio
    async def test_linear_parent_is_immediate_predecessor(self, monkeypatch):
        from app.agents import engine as eng
        _patch_topology(monkeypatch, {"A": [_conn("A", "B")], "B": [_conn("B", "C")]})

        chain, parent_of = await eng._resolve_ordered_chain_with_parents("A")

        assert chain == ["A", "B", "C"]
        # invariante linear: parent_of[chain[i]] == chain[i-1]
        assert parent_of == {"B": "A", "C": "B"}

    @pytest.mark.asyncio
    async def test_diamond_first_discoverer_wins(self, monkeypatch):
        from app.agents import engine as eng
        _patch_topology(monkeypatch, {
            "A": [_conn("A", "B"), _conn("A", "C")],
            "B": [_conn("B", "D")],
            "C": [_conn("C", "D")],
        })

        chain, parent_of = await eng._resolve_ordered_chain_with_parents("A")

        assert chain == ["A", "B", "C", "D"]
        # BFS: D é descoberto por B antes de C → parent_of[D] == "B"
        assert parent_of["D"] == "B"

    @pytest.mark.asyncio
    async def test_compat_wrapper_returns_chain_only(self, monkeypatch):
        from app.agents import engine as eng
        _patch_topology(monkeypatch, {"A": [_conn("A", "B"), _conn("A", "C")]})

        chain = await eng._resolve_ordered_chain("A")

        assert chain == ["A", "B", "C"]


# ─── fan-out 1-de-N via execute_pipeline ────────────────────────────


class TestFanOutRouting:
    @pytest.mark.asyncio
    async def test_skips_non_matching_second_child(self, monkeypatch):
        """A PROVA DO FIX. Fan-out A→B (casa) e A→C (NÃO casa). O motor antigo
        NUNCA pulava C (procurava conexão B→C inexistente → rodava sempre).
        Agora C avalia contra o PARENT A e é corretamente pulado."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "A": _agent("A", "Triagem"),
            "B": _agent("B", "Investimentos"),
            "C": _agent("C", "Suporte"),
        })
        _patch_topology(monkeypatch, {"A": [
            _conn("A", "B", ctype="conditional", expr="'invest' in input_lower"),
            _conn("A", "C", ctype="conditional", expr="'suporte' in input_lower"),
        ]})
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="A", user_input="quero investir melhor")

        ran = [x["agent_id"] for x in invoked]
        assert "A" in ran and "B" in ran
        assert "C" not in ran, "2º filho cuja regra NÃO casa deveria ser pulado (era o bug)"
        assert _statuses(res)["C"] == "skipped_conditional"

    @pytest.mark.asyncio
    async def test_routes_to_the_other_branch(self, monkeypatch):
        """Espelho do anterior: a pergunta casa com C, então B é pulado e C roda.
        Garante que é 1-de-N de verdade (não 'o primeiro filho sempre vence')."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "A": _agent("A", "Triagem"),
            "B": _agent("B", "Investimentos"),
            "C": _agent("C", "Suporte"),
        })
        _patch_topology(monkeypatch, {"A": [
            _conn("A", "B", ctype="conditional", expr="'invest' in input_lower"),
            _conn("A", "C", ctype="conditional", expr="'suporte' in input_lower"),
        ]})
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="A", user_input="preciso de suporte agora")

        ran = [x["agent_id"] for x in invoked]
        assert "A" in ran and "C" in ran
        assert "B" not in ran
        assert _statuses(res)["B"] == "skipped_conditional"

    @pytest.mark.asyncio
    async def test_each_child_derives_context_from_common_parent(self, monkeypatch):
        """Com 2 filhos rodando, o 2º (C) deve ver o output do PARENT (A),
        não o do irmão anterior (B). Output do mock = 'output-of-<id>', então
        dá pra cravar a origem do contexto."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "A": _agent("A", "Triagem"),
            "B": _agent("B", "Invest"),
            "C": _agent("C", "Cambio"),
        })
        _patch_topology(monkeypatch, {"A": [
            _conn("A", "B", ctype="conditional", expr="'dinheiro' in input_lower"),
            _conn("A", "C", ctype="conditional", expr="'dinheiro' in input_lower"),
        ]})
        invoked = _patch_executions(monkeypatch)

        await eng.execute_pipeline(entry_agent_id="A", user_input="quero render dinheiro")

        by_id = {x["agent_id"]: x for x in invoked}
        assert "B" in by_id and "C" in by_id  # ambos casaram → ambos rodam
        # C deriva do PARENT comum (A), não do irmão B:
        assert by_id["C"]["pipeline_context"] == "output-of-A"
        assert by_id["C"]["pipeline_context"] != "output-of-B"
        assert "output-of-A" in by_id["C"]["user_input"]

    @pytest.mark.asyncio
    async def test_fanout_can_route_on_parent_output_too(self, monkeypatch):
        """Além de input_lower (pergunta do usuário), o gate continua podendo
        ramificar pelo OUTPUT do parent (output_lower) — confirma que o output
        do PARENT chega correto ao gate de cada filho do fan-out."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "A": _agent("A", "Triagem"), "B": _agent("B", "B"), "C": _agent("C", "C"),
        })
        _patch_topology(monkeypatch, {"A": [
            # A.output == "output-of-A" → output_lower contém "output-of-a"
            _conn("A", "B", ctype="conditional", expr="'output-of-a' in output_lower"),
            _conn("A", "C", ctype="conditional", expr="'zzz-nao-existe' in output_lower"),
        ]})
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="A", user_input="tanto faz")

        ran = [x["agent_id"] for x in invoked]
        assert "B" in ran and "C" not in ran
        assert _statuses(res)["C"] == "skipped_conditional"


# ─── regressão: cadeia linear segue idêntica ────────────────────────


class TestLinearStillGatesAgainstImmediateParent:
    @pytest.mark.asyncio
    async def test_linear_conditional_false_skips_next(self, monkeypatch):
        """A→B→C com B→C conditional=false. Aqui parent==chain[i-1] (linear),
        então usa o caminho histórico (last_result) — byte-idêntico."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "A": _agent("A", "A"), "B": _agent("B", "B"), "C": _agent("C", "C"),
        })
        _patch_topology(monkeypatch, {
            "A": [_conn("A", "B")],  # sequential
            "B": [_conn("B", "C", ctype="conditional", expr="'nunca' in output_lower")],
        })
        invoked = _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="A", user_input="oi")

        ran = [x["agent_id"] for x in invoked]
        assert ran == ["A", "B"]  # C pulado pela regra linear
        assert _statuses(res)["C"] == "skipped_conditional"

    @pytest.mark.asyncio
    async def test_linear_all_run_in_order(self, monkeypatch):
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "A": _agent("A", "A"), "B": _agent("B", "B"), "C": _agent("C", "C"),
        })
        _patch_topology(monkeypatch, {"A": [_conn("A", "B")], "B": [_conn("B", "C")]})
        invoked = _patch_executions(monkeypatch)

        await eng.execute_pipeline(entry_agent_id="A", user_input="oi")

        assert [x["agent_id"] for x in invoked] == ["A", "B", "C"]


# ─── display honesto do tipo de conexão ─────────────────────────────


class TestMeshChainDisplayHonesty:
    @pytest.mark.asyncio
    async def test_mesh_chain_shows_real_connection_type(self, monkeypatch):
        """trace.mesh_chain não pode cravar 'sequential' quando a aresta é
        conditional (era uma mentira de display que confundiu o usuário)."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {"A": _agent("A", "A"), "B": _agent("B", "B")})
        _patch_topology(monkeypatch, {"A": [
            _conn("A", "B", ctype="conditional", expr="'x' in input_lower"),
        ]})
        _patch_executions(monkeypatch)

        res = await eng.execute_pipeline(entry_agent_id="A", user_input="x marca o ramo")

        mesh = {m["id"]: m for m in res["trace"]["mesh_chain"]}
        assert mesh["B"]["connection"] == "conditional"
