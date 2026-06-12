"""Fluxograma de agentes (PR2) — validação canônica de connection_type na rota.

O canvas de edição cria/edita arestas via POST/PUT /api/v1/mesh/connections,
incluindo o 4º tipo `default` (else do fan-out), que o engine já honra mas a UI
antiga não criava. A coluna mesh_connections.connection_type não tem CHECK no
DB; a validação de rota (mesh.py::_VALID_CONNECTION_TYPES) impede tipos inválidos
e self-loop por API, sem afetar os testes de engine (que usam mesh_repo direto).

Cobertura:
- cria: rejeita tipo inválido (422) e self-loop (422)
- cria: aceita os 4 tipos canônicos (incl. `default`) e propaga type+config
- edita: rejeita tipo inválido (422); aceita `default`
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models.schemas import MeshConnectionCreate


def _conn(**kw):
    base = dict(source_agent_id="a", target_agent_id="b", connection_type="sequential", config="{}")
    base.update(kw)
    return MeshConnectionCreate(**base)


@pytest.mark.asyncio
async def test_create_rejects_invalid_type(monkeypatch):
    from app.routes import mesh
    monkeypatch.setattr(mesh, "agents_repo", AsyncMock())
    monkeypatch.setattr(mesh, "mesh_repo", AsyncMock())
    with pytest.raises(HTTPException) as ei:
        await mesh.create_connection(_conn(connection_type="bogus"))
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_self_loop(monkeypatch):
    from app.routes import mesh
    monkeypatch.setattr(mesh, "agents_repo", AsyncMock())
    monkeypatch.setattr(mesh, "mesh_repo", AsyncMock())
    with pytest.raises(HTTPException) as ei:
        await mesh.create_connection(_conn(source_agent_id="x", target_agent_id="x"))
    assert ei.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("ctype", ["sequential", "parallel", "conditional", "default"])
async def test_create_accepts_canonical_types(monkeypatch, ctype):
    from app.routes import mesh
    agents = AsyncMock()
    agents.find_by_id = AsyncMock(return_value={"id": "x"})
    repo = AsyncMock()
    repo.create = AsyncMock(return_value=None)
    monkeypatch.setattr(mesh, "agents_repo", agents)
    monkeypatch.setattr(mesh, "mesh_repo", repo)

    res = await mesh.create_connection(_conn(connection_type=ctype, config='{"expr":"has_document"}'))
    assert "id" in res
    persisted = repo.create.call_args.args[0]
    assert persisted["connection_type"] == ctype
    assert persisted["config"] == '{"expr":"has_document"}'


@pytest.mark.asyncio
async def test_update_rejects_invalid_type(monkeypatch):
    from app.routes import mesh
    repo = AsyncMock()
    repo.find_by_id = AsyncMock(return_value={"id": "c1"})
    repo.update = AsyncMock(return_value={})
    monkeypatch.setattr(mesh, "mesh_repo", repo)
    with pytest.raises(HTTPException) as ei:
        await mesh.update_connection("c1", _conn(connection_type="weird"))
    assert ei.value.status_code == 422
    assert not repo.update.called


@pytest.mark.asyncio
async def test_update_accepts_default(monkeypatch):
    from app.routes import mesh
    repo = AsyncMock()
    repo.find_by_id = AsyncMock(return_value={"id": "c1"})
    repo.update = AsyncMock(return_value={"id": "c1", "connection_type": "default"})
    monkeypatch.setattr(mesh, "mesh_repo", repo)
    await mesh.update_connection("c1", _conn(connection_type="default"))
    assert repo.update.called
    upd = repo.update.call_args.args[1]
    assert upd["connection_type"] == "default"
