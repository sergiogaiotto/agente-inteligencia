"""Fluxograma de agentes (PR5) — grupos do mesh para tingir nós.

O Fluxograma reusa a chave `mesh_groups` de platform_settings (mesma fonte da
Topologia) para colorir/rotular nós por grupo. GET /api/v1/mesh/groups faz o
parse defensivo num shape estável; o canvas só renderiza.

Cobertura:
- vazio / JSON inválido → []
- parse de grupos válidos (id/name/color/agent_ids)
- descarta entradas sem id ou sem name; default color 'teal'; agent_ids coeridos a str
"""
from __future__ import annotations

import json

import pytest


class _FakeStore:
    def __init__(self, value=""):
        self.value = value

    async def get(self, key, default=""):
        return self.value if key == "mesh_groups" else default


def _patch(monkeypatch, value):
    monkeypatch.setattr("app.core.database.settings_store", _FakeStore(value))


@pytest.mark.asyncio
async def test_empty_or_invalid_returns_empty(monkeypatch):
    from app.routes import mesh
    for v in ("", "{not json", json.dumps({"not": "a list"})):
        _patch(monkeypatch, v)
        assert await mesh.get_groups() == {"groups": []}


@pytest.mark.asyncio
async def test_parses_valid_groups(monkeypatch):
    from app.routes import mesh
    _patch(monkeypatch, json.dumps([
        {"id": "g1", "name": "Atendimento", "color": "amber", "agent_ids": ["a1", "a2"]},
        {"id": "g2", "name": "Crédito"},                        # sem color/agent_ids → defaults
        {"name": "sem id"},                                     # descartado
        {"id": "g3"},                                           # sem name → descartado
        "lixo",                                                 # não-dict → descartado
    ]))
    res = await mesh.get_groups()
    groups = res["groups"]
    assert [g["id"] for g in groups] == ["g1", "g2"]
    g1 = groups[0]
    assert g1["name"] == "Atendimento" and g1["color"] == "amber" and g1["agent_ids"] == ["a1", "a2"]
    g2 = groups[1]
    assert g2["color"] == "teal" and g2["agent_ids"] == []      # defaults


@pytest.mark.asyncio
async def test_agent_ids_coerced_to_str(monkeypatch):
    from app.routes import mesh
    _patch(monkeypatch, json.dumps([{"id": 7, "name": "N", "agent_ids": [1, 2, None, "x"]}]))
    res = await mesh.get_groups()
    g = res["groups"][0]
    assert g["id"] == "7"
    assert g["agent_ids"] == ["1", "2", "x"]                    # None descartado, números → str
