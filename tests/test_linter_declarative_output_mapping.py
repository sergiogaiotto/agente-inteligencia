"""Linter: binding declarativo SEM output_mapping é ERROR, não warning.

CONTEXTO (2026-06-07 — causa estrutural do bug "resultado parcial" da CEP):
O runtime declarativo trata `output_mapping` como OBRIGATÓRIO
(`declarative_engine`: "output_mapping é obrigatório") — sem ele a chamada dá
2xx, mas o engine marca erro → `final_state=partial` → a UI mostra
"· resultado parcial" e o endereço não chega ao contexto. Mas o linter só
emitia WARNING (`empty_output_mapping`) → a skill era salva quebrada e só
falhava em runtime, de forma opaca.

Fix: para `execution_mode == "declarative"`, binding sem `output_mapping` é
ERROR (`/lint` reporta `is_valid=false`; o painel da UI marca vermelho ANTES do
save). Para skills NÃO-declarativas, segue WARNING (back-compat — bindings só
rodam em modo declarativo).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.skill_parser.linter import lint_skill


def _binding(**over) -> dict:
    b = {"id": "b1", "connector": "c1", "method": "GET", "path": "/x"}
    b.update(over)
    return b


def _parsed(exec_mode: str, bindings: list[dict]) -> SimpleNamespace:
    # lint_skill lê via getattr: execution_mode, api_bindings_parsed, output_contract.
    return SimpleNamespace(
        execution_mode=exec_mode, api_bindings_parsed=bindings, output_contract=""
    )


def _codes(issues, severity=None) -> list[str]:
    return [i["code"] for i in issues if severity is None or i["severity"] == severity]


def test_declarative_binding_without_output_mapping_is_error():
    issues = lint_skill(_parsed("declarative", [_binding()]))  # sem output_mapping
    assert "missing_output_mapping_declarative" in _codes(issues, "error"), issues
    # não deve duplicar com o warning antigo para o mesmo caso
    assert "empty_output_mapping" not in _codes(issues)


def test_declarative_binding_with_output_mapping_ok():
    b = _binding(output_mapping=[{"from": "$.bairro", "to": "bairro"}])
    issues = lint_skill(_parsed("declarative", [b]))
    assert "missing_output_mapping_declarative" not in _codes(issues)
    assert "empty_output_mapping" not in _codes(issues)


def test_non_declarative_binding_without_mapping_stays_warning():
    # Skill não-declarativa com binding solto: bindings não rodam → segue warning,
    # não bloqueia (preserva o comportamento legado).
    issues = lint_skill(_parsed("standard", [_binding()]))
    assert "missing_output_mapping_declarative" not in _codes(issues)
    assert "empty_output_mapping" in _codes(issues, "warning")
