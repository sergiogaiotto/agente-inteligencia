"""Regressão do falso positivo `declarative_without_bindings`.

O linter só olhava `api_bindings_parsed`; skill declarativa que consulta APENAS
## Data Tables (DuckDB) — caso real: "Limite de Cheque Especial por Cliente" —
recebia error no /lint apesar de o parser aceitar (parser.py valida
api_bindings_parsed OU data_tables_parsed) e de funcionar em produção.
"""
from __future__ import annotations

from app.skill_parser.linter import lint_skill
from app.skill_parser.parser import parse_skill_md


SKILL_DATA_TABLES_ONLY = """---
id: urn:skill:test:subagent:dt-only
version: 0.1.0
kind: subagent
execution_mode: declarative
---

# Consulta Declarativa Só-Tabela

## Purpose
Consulta uma tabela governada e devolve o registro.

## Inputs
```json
{"type": "object", "properties": {"cd": {"type": "integer"}}, "required": ["cd"]}
```

## Data Tables
```yaml
tables:
  - id: tabela_teste
    table_ref: urn:table:abcd1234:tabela-teste:1
    query:
      select: [cd, valor]
      filters:
        - col: cd
          op: "="
          value: "{{ inputs.cd }}"
          if_present: cd
```
"""

SKILL_API_BINDINGS_ONLY = """---
id: urn:skill:test:subagent:api-only
version: 0.1.0
kind: subagent
execution_mode: declarative
---

# Consulta Declarativa Só-API

## Purpose
Chama um endpoint HTTP e mapeia a resposta.

## API Bindings
```yaml
- id: b1
  connector: c1
  connector_id: c1
  name: Consulta
  method: GET
  path: /x/{cd}
  output_mapping:
    - from: $.valor
      to: valor
```
"""

SKILL_DECLARATIVE_EMPTY = """---
id: urn:skill:test:subagent:decl-vazia
version: 0.1.0
kind: subagent
execution_mode: declarative
---

# Declarativa Sem Fonte

## Purpose
Não tem nem API Bindings nem Data Tables.
"""


def _codes(raw: str) -> list[str]:
    parsed = parse_skill_md(raw)
    return [i["code"] for i in lint_skill(parsed)]


class TestDeclarativeSourceGate:
    def test_data_tables_only_passes(self):
        codes = _codes(SKILL_DATA_TABLES_ONLY)
        assert "declarative_without_bindings" not in codes

    def test_api_bindings_only_still_passes(self):
        codes = _codes(SKILL_API_BINDINGS_ONLY)
        assert "declarative_without_bindings" not in codes

    def test_no_source_still_errors(self):
        codes = _codes(SKILL_DECLARATIVE_EMPTY)
        assert "declarative_without_bindings" in codes

    def test_parity_with_parser_validation(self):
        """Parser e linter devem concordar: se o parser aceita (is_valid) a
        skill declarativa só-Data-Tables, o linter não pode dar error."""
        parsed = parse_skill_md(SKILL_DATA_TABLES_ONLY)
        assert parsed.data_tables_parsed, "fixture deveria parsear a tabela"
        assert not any(
            "declarative" in e for e in (parsed.validation_errors or [])
        )
        assert "declarative_without_bindings" not in [
            i["code"] for i in lint_skill(parsed)
        ]
