"""PR-A1 (Trilha A) — execução SELADA: a BFS pode ser delimitada ao subgrafo
do pipeline (allowed_agent_ids), sem vazar para o mesh global.

Cobre: _resolve_ordered_chain_with_parents (delimitado vs global);
resolve_pipeline_exec (root + membros do snapshot); execute_pipeline_entry
repassa allowed_agent_ids ao motor.
"""
import asyncio

import app.core.database as db
import app.agents.engine as engine
import app.catalog.executor as ex
import app.catalog.pipeline_defs as pdefs
from app.agents.engine import _resolve_ordered_chain_with_parents
from app.catalog.pipeline_defs import resolve_pipeline_exec


def _mesh(graph):
    async def find_all(*a, **k):
        src = k.get("source_agent_id")
        return [{"id": f"{src}->{t}", "target_agent_id": t} for t in graph.get(src, [])]
    return find_all


def _noop_async(value=None):
    async def _fn(*a, **k):
        return value
    return _fn


# ───────────── BFS delimitada vs global ─────────────
def test_bounded_bfs_excludes_non_members(monkeypatch):
    graph = {"root": ["A", "C"], "A": ["B", "X"]}
    monkeypatch.setattr(db.mesh_repo, "find_all", _mesh(graph))
    chain, parent = asyncio.run(_resolve_ordered_chain_with_parents("root", {"root", "A", "B"}))
    assert set(chain) == {"root", "A", "B"}      # C e X (fora do conjunto) excluídos
    assert "C" not in chain and "X" not in chain
    assert parent["A"] == "root" and parent["B"] == "A"


def test_unbounded_bfs_includes_all(monkeypatch):
    graph = {"root": ["A", "C"], "A": ["B", "X"]}
    monkeypatch.setattr(db.mesh_repo, "find_all", _mesh(graph))
    chain, _ = asyncio.run(_resolve_ordered_chain_with_parents("root", None))
    assert set(chain) == {"root", "A", "B", "C", "X"}  # global (default — zero regressão)


def test_bounded_accepts_list_not_only_set(monkeypatch):
    graph = {"root": ["A"], "A": ["B"]}
    monkeypatch.setattr(db.mesh_repo, "find_all", _mesh(graph))
    chain, _ = asyncio.run(_resolve_ordered_chain_with_parents("root", ["root", "A"]))
    assert set(chain) == {"root", "A"}  # B fora → excluído; aceita list (convertido p/ set)


# ───────────── resolve_pipeline_exec ─────────────
def test_resolve_pipeline_exec_from_snapshot(monkeypatch):
    async def fake_def(eid):
        return {"root_agent_id": "r", "nodes": [{"id": "r"}, {"id": "a"}, {"id": "b"}], "edges": []}
    monkeypatch.setattr(pdefs, "get_pipeline_def", fake_def)
    root, members = asyncio.run(resolve_pipeline_exec({"id": "e1", "artifact_id": "p1"}))
    assert root == "r"
    assert members == {"r", "a", "b"}


def test_resolve_pipeline_exec_fallback_live(monkeypatch):
    # Sem snapshot → computa do mesh vivo (_build_subgraph).
    monkeypatch.setattr(pdefs, "get_pipeline_def", _noop_async(None))
    async def fake_sub(pid):
        return {"root_agent_id": "r", "nodes": [{"id": "r"}, {"id": "a"}], "edges": []}
    monkeypatch.setattr(pdefs, "_build_subgraph", fake_sub)
    root, members = asyncio.run(resolve_pipeline_exec({"id": "e1", "artifact_id": "p1"}))
    assert root == "r" and members == {"r", "a"}


# ───────────── execute_pipeline_entry repassa allowed ─────────────
def test_execute_pipeline_entry_forwards_allowed(monkeypatch):
    captured = {}
    async def fake_pipeline(**k):
        captured.update(k)
        return {"pipeline_steps": [], "completed_agents": 0, "duration_ms": 1, "interaction_id": None, "status": "completed"}
    monkeypatch.setattr(engine, "execute_pipeline", fake_pipeline)
    monkeypatch.setattr(ex, "append_step_result", _noop_async())
    monkeypatch.setattr(ex, "finalize_execution", _noop_async())
    monkeypatch.setattr(ex, "record_invocation_cost", _noop_async())
    asyncio.run(ex.execute_pipeline_entry(
        execution_id="x", pipeline_entry_id="pe", root_agent_id="r",
        consumer_user={"id": "u"}, user_input="oi", allowed_agent_ids={"r", "a"},
    ))
    assert captured.get("allowed_agent_ids") == {"r", "a"}
    assert captured.get("entry_agent_id") == "r"
