"""Snapshot do GRAFO de pipelines no Catálogo (PR5, Parte B).

Quando um pipeline (kind='pipeline') é publicado, congelamos o subgrafo —
membros do pipeline + as conexões tipadas ENTRE eles + a raiz (entrada da
cadeia) — em `catalog_pipeline_defs`. É o que diferencia pipeline (GRAFO) de
recipe (linear). A def alimenta a UI (PR6, mini-fluxograma read-only) e dá a
raiz pela qual a execução reusa `execute_pipeline` (engine do mesh).

A execução em si NÃO usa o snapshot congelado: roda o mesh VIVO a partir da
raiz (reusar execute_pipeline). O snapshot é registro/auditoria + descoberta da
raiz. (Executar o grafo congelado é evolução futura.)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from app.core.database import (
    _get_pool,
    agents_repo,
    mesh_repo,
    pipeline_membership,
    pipelines_repo,
)

logger = logging.getLogger(__name__)


async def _build_subgraph(pipeline_id: str) -> dict:
    """Monta {root_agent_id, nodes, edges} do subgrafo do pipeline a partir do
    estado VIVO (membership + mesh_connections intra-pipeline)."""
    members = await pipeline_membership.agents_of(pipeline_id)
    member_set = set(members)

    conns = await mesh_repo.find_all(limit=1000)
    edges = []
    for c in conns:
        s, t = c.get("source_agent_id"), c.get("target_agent_id")
        if s in member_set and t in member_set:
            # config é TEXT (JSON string) em mesh_connections — parseia p/ objeto
            # de verdade, senão o snapshot JSONB guardaria a string "{}" (quebra o
            # contrato de edge {..., config: {...}} que a UI do PR6 vai consumir).
            edges.append({
                "id": c["id"],
                "source": s,
                "target": t,
                "type": c.get("connection_type") or "sequential",
                "config": _parse_jsonish(c.get("config")) or {},
            })

    # Raiz: ponto de entrada EXPLÍCITO (entry_agent_id) tem prioridade, desde que
    # seja membro — dá controle e desempata 2+ raízes / 0 conexões. Sem entry válido,
    # cai na raiz topológica (source-never-target, fonte única do PR3) → members[0].
    entry = None
    try:
        p = await pipelines_repo.find_by_id(pipeline_id)
        entry = (p or {}).get("entry_agent_id")
    except Exception:
        entry = None
    if entry and entry in member_set:
        root = entry
    else:
        from app.routes.mesh import _detect_roots
        roots = _detect_roots(edges)
        root = roots[0] if roots else (members[0] if members else None)

    nodes = []
    for aid in members:
        a = await agents_repo.find_by_id(aid)
        if a:
            nodes.append({
                "id": a["id"],
                "name": a.get("name"),
                "kind": a.get("kind", "subagent"),
                "status": a.get("status"),
                "version": a.get("version", "1.0.0"),
            })

    return {"root_agent_id": root, "nodes": nodes, "edges": edges}


async def snapshot_pipeline_def(entry: dict) -> Optional[dict]:
    """Congela o subgrafo do pipeline (entry.artifact_id) em catalog_pipeline_defs
    (upsert por entry_id). Retorna a def ou None se o pipeline não tem agentes."""
    pipeline_id = entry.get("artifact_id")
    if not pipeline_id:
        return None
    sub = await _build_subgraph(pipeline_id)
    if not sub["root_agent_id"]:
        return None  # pipeline sem agentes → nada a snapshotar
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO catalog_pipeline_defs (entry_id, root_agent_id, nodes, edges, snapshot_at, updated_at)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, now(), now())
            ON CONFLICT (entry_id) DO UPDATE SET
              root_agent_id = EXCLUDED.root_agent_id,
              nodes = EXCLUDED.nodes,
              edges = EXCLUDED.edges,
              snapshot_at = now(),
              updated_at = now()
            """,
            entry["id"], sub["root_agent_id"], json.dumps(sub["nodes"]), json.dumps(sub["edges"]),
        )
    return {"entry_id": entry["id"], **sub}


def _parse_jsonish(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return []
    return v if v is not None else []


async def get_pipeline_def(entry_id: str) -> Optional[dict]:
    """Lê a def (snapshot) de um pipeline publicado. None se ainda não gerada."""
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow("SELECT * FROM catalog_pipeline_defs WHERE entry_id=$1", entry_id)
    if not r:
        return None
    d = dict(r)
    d["nodes"] = _parse_jsonish(d.get("nodes"))
    d["edges"] = _parse_jsonish(d.get("edges"))
    for k in ("snapshot_at", "created_at", "updated_at"):
        v = d.get(k)
        if v is not None and hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


async def resolve_pipeline_root(entry: dict) -> Optional[str]:
    """Raiz p/ execução: do snapshot (def) se houver; senão computa do mesh vivo
    (entries publicadas antes do PR5, ou snapshot que falhou)."""
    root, _ = await resolve_pipeline_exec(entry)
    return root


async def resolve_pipeline_exec(entry: dict) -> tuple[Optional[str], set]:
    """(root, allowed_agent_ids) p/ execução SELADA (Trilha A / PR-A1).

    Do snapshot (def) se houver — execução determinística pelo grafo congelado
    na publicação; senão computa do mesh vivo (fallback). allowed_agent_ids = ids
    dos membros (nodes), passado a execute_pipeline para delimitar a BFS.
    """
    d = await get_pipeline_def(entry["id"])
    if d and d.get("root_agent_id"):
        members = {n.get("id") for n in (d.get("nodes") or []) if n.get("id")}
        return d["root_agent_id"], members
    pipeline_id = entry.get("artifact_id")
    if not pipeline_id:
        return None, set()
    sub = await _build_subgraph(pipeline_id)
    members = {n.get("id") for n in sub.get("nodes", []) if n.get("id")}
    return sub.get("root_agent_id"), members
