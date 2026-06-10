"""Workspace — texto livre do chat → input nomeado de skill declarativa.

Bug (2026-06-10): chat de um agente declarativo mandava o texto digitado como
`{"question": <texto>}`, mas a skill declarativa filtra por `{{ inputs.cd_cliente }}`
→ filtro vazio → 0 linhas silenciosas. Fix: quando o input é inequívoco (1 campo
required ou 1 property), mapeia o texto pro campo nomeado (e a coerção converte o tipo).
"""
from __future__ import annotations

from app.routes.workspace import _coerce_inputs_by_schema, _single_required_input


def test_single_required_input_one_required():
    schema = {"required": ["cd_cliente"], "properties": {"cd_cliente": {"type": "integer"}}}
    assert _single_required_input(schema) == "cd_cliente"


def test_single_required_input_one_property_no_required():
    assert _single_required_input({"properties": {"codigo": {"type": "string"}}}) == "codigo"


def test_single_required_input_ambiguous_returns_none():
    assert _single_required_input({"required": ["a", "b"], "properties": {"a": {}, "b": {}}}) is None
    assert _single_required_input({"properties": {"a": {}, "b": {}}}) is None


def test_single_required_input_edge_cases():
    assert _single_required_input(None) is None
    assert _single_required_input({}) is None
    assert _single_required_input({"required": []}) is None


def test_freetext_maps_to_single_input_and_coerces_type():
    # simula o caminho do chat: texto "2" + schema de 1 input integer
    schema = {"required": ["cd_cliente"], "properties": {"cd_cliente": {"type": "integer"}}}
    target = _single_required_input(schema)
    inputs = {target: "2"} if target else {"question": "2"}
    inputs = _coerce_inputs_by_schema(inputs, schema)
    assert inputs == {"cd_cliente": 2}   # mapeado + coagido a int → filtro casa


def test_freetext_multi_input_falls_back_to_question():
    schema = {"required": ["origem", "destino"],
              "properties": {"origem": {"type": "string"}, "destino": {"type": "string"}}}
    target = _single_required_input(schema)
    inputs = {target: "x"} if target else {"question": "x"}
    assert inputs == {"question": "x"}   # ambíguo → genérico (precisa inputs estruturados)
