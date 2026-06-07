"""Metadata de ajuda dos tipos de conexão (protótipo do ?-help, 2026-06-07).

Fonte ÚNICA de verdade para os textos "o que é / quando usar" exibidos no
popover `?` ao lado de cada card de Tipo de Conexão (mesh.html). Mantido no
backend (não hardcoded no template) para não fragmentar/drift — mesma filosofia
de `CONTEXT_SCOPE_VARS_META` / `/context-scope-vars`.

Estes testes garantem que a metadata cobre os 3 tipos reais que a UI oferece
(`sequential`/`parallel`/`conditional`) com todos os campos preenchidos, e que o
endpoint a expõe.
"""
from __future__ import annotations

import asyncio

from app.routes.mesh import MESH_CONNECTION_TYPES_HELP, connection_types


def test_covers_the_three_ui_connection_types():
    ids = {t["id"] for t in MESH_CONNECTION_TYPES_HELP}
    assert ids == {"sequential", "parallel", "conditional"}


def test_every_entry_has_label_what_when():
    for t in MESH_CONNECTION_TYPES_HELP:
        assert t.get("label"), f"label vazio em {t.get('id')}"
        assert t.get("what"), f"'what' (o que é) vazio em {t.get('id')}"
        assert t.get("when"), f"'when' (quando usar) vazio em {t.get('id')}"


def test_endpoint_exposes_the_metadata():
    data = asyncio.run(connection_types())
    assert data["types"] == MESH_CONNECTION_TYPES_HELP
    # shape estável p/ o frontend montar o mapa id→help
    assert isinstance(data["types"], list) and len(data["types"]) == 3
