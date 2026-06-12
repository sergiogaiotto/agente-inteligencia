"""Fluxograma de agentes (PR1) — endpoints de LAYOUT posicional do AI Mesh.

A nova view "Fluxograma de agentes" lê o MESMO grafo de mesh_connections que a
"Topologia de conexões"; a única coisa extra é a posição x,y de cada nó, que
NÃO pode contaminar o modelo de execução. Por isso o layout vive em
platform_settings sob a chave `mesh_node_positions` (precedente: mesh_groups /
mesh_chain_names) e tem endpoint DEDICADO — nunca o PUT /settings genérico, que
re-serializaria SettingsSave inteiro e zeraria mcp_per_tool_enabled / configs de
LLM a cada drag.

Cobertura:
- GET vazio → {positions: {}}
- PUT → GET roundtrip (arredonda x,y)
- PUT escreve SÓ a chave mesh_node_positions (não toca outras settings)
- sanitização: descarta entradas malformadas e bool (subclasse de int)
- payload inválido → 422
- GET resiliente a JSON malformado no store
- página /mesh/flow registrada como 2º submenu do AI Mesh
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException


class _FakeStore:
    """Mímica do SettingsStore: dict key→value, upsert por-chave (como o
    ON CONFLICT (key) do Postgres). Registra writes para asseverar isolamento."""

    def __init__(self, initial: dict | None = None):
        self.data = dict(initial or {})
        self.writes: list[str] = []

    async def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    async def set(self, key: str, value: str):
        self.writes.append(key)
        self.data[key] = str(value)


def _patch_store(monkeypatch, store):
    # save_layout/get_layout fazem `from app.core.database import settings_store`
    # em tempo de chamada → basta trocar o atributo do módulo.
    monkeypatch.setattr("app.core.database.settings_store", store)


@pytest.mark.asyncio
async def test_get_layout_empty(monkeypatch):
    from app.routes import mesh
    _patch_store(monkeypatch, _FakeStore())
    res = await mesh.get_layout()
    assert res == {"positions": {}}


@pytest.mark.asyncio
async def test_put_then_get_roundtrip_rounds_coords(monkeypatch):
    from app.routes import mesh
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    res = await mesh.save_layout({"positions": {"a1": {"x": 10.04, "y": 20.96}}})
    assert res["count"] == 1
    got = await mesh.get_layout()
    assert got["positions"] == {"a1": {"x": 10.0, "y": 21.0}}


@pytest.mark.asyncio
async def test_put_writes_only_positions_key(monkeypatch):
    """Isolamento: salvar layout NÃO pode reescrever outras settings."""
    from app.routes import mesh
    store = _FakeStore({"mcp_per_tool_enabled": "True", "azure_key": "secret"})
    _patch_store(monkeypatch, store)
    await mesh.save_layout({"positions": {"a1": {"x": 1, "y": 2}}})
    assert store.writes == ["mesh_node_positions"]          # só esta chave foi escrita
    assert store.data["mcp_per_tool_enabled"] == "True"      # intacta
    assert store.data["azure_key"] == "secret"               # intacta


@pytest.mark.asyncio
async def test_put_sanitizes_malformed_and_bool(monkeypatch):
    from app.routes import mesh
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    res = await mesh.save_layout({"positions": {
        "ok": {"x": 5, "y": 6},
        "missing_y": {"x": 7},
        "str_x": {"x": "nope", "y": 1},
        "bool_x": {"x": True, "y": 1},   # bool é subclasse de int — deve cair
        "not_obj": 42,
    }})
    assert res["count"] == 1
    saved = json.loads(store.data["mesh_node_positions"])
    assert saved == {"ok": {"x": 5.0, "y": 6.0}}


@pytest.mark.asyncio
async def test_put_invalid_payload_raises_422(monkeypatch):
    from app.routes import mesh
    _patch_store(monkeypatch, _FakeStore())
    with pytest.raises(HTTPException) as ei:
        await mesh.save_layout({"positions": "not-a-dict"})
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_get_layout_tolerates_corrupt_json(monkeypatch):
    from app.routes import mesh
    _patch_store(monkeypatch, _FakeStore({"mesh_node_positions": "{not valid json"}))
    res = await mesh.get_layout()
    assert res == {"positions": {}}


def test_mesh_flow_page_registered_as_submenu():
    from app.routes.frontend import PAGES
    assert "/mesh/flow" in PAGES
    p = PAGES["/mesh/flow"]
    assert p["template"] == "pages/mesh_flow.html"
    # mesma 'section' que /mesh → o item-pai "AI Mesh" fica ativo nas duas views
    assert p["section"] == "mesh"
    assert PAGES["/mesh"]["section"] == "mesh"
