"""Helper canônico pra extrair JSON Schema da seção `## Inputs` de uma SKILL.md.

Antes deste módulo (até Onda B):
- Cópia 1: app/routes/skill_dryrun.py:_extract_inputs_schema (canônico do dry-run)
- Cópia 2: app/workspace/binding_schema.py:_extract_inputs_schema (canônico Onda A)
- Cópia 3 (implícita em workspace.py:23): regex simples sem _walk

Risco da duplicação: regras de parsing divergem ao longo do tempo. Bugs em
um lugar não aparecem em outro. Onda B unifica — agora qualquer caller
(runtime, dry-run, workspace) usa a mesma implementação.

Política: este módulo NÃO importa do app/* além de stdlib/typing — evita
ciclos de import com runtime/parser/binding_schema.
"""
from __future__ import annotations

import json
import re
from typing import Optional


def extract_inputs_schema(skill_md: str) -> Optional[dict]:
    """Extrai o JSON Schema declarado em `## Inputs` da SKILL.md.

    Aceita formato canônico:

        ## Inputs
        ```json
        {"type":"object","required":[...],"properties":{...}}
        ```

    Returns:
        dict com o JSON Schema parseado, OU None quando:
        - skill_md vazio
        - Seção ## Inputs ausente
        - Sem bloco fenced JSON
        - JSON malformado
        - Schema sem `properties` (não vira function spec útil)

    Por que rejeitar schema sem `properties`:
    Engine MCP usa `properties` pra construir o function spec do LLM.
    Sem isso, não há fields pra preencher. Caller pode então cair pro
    fallback (legacy {operation, query} ou tool.inputSchema do MCP discovery).
    """
    if not skill_md:
        return None
    m = re.search(r"##\s+Inputs\s*\n([\s\S]*?)(?=\n##\s|$)", skill_md)
    if not m:
        return None
    block = m.group(1)
    fence = re.search(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n```", block)
    if not fence:
        return None
    try:
        schema = json.loads(fence.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(schema, dict):
        return None
    if not isinstance(schema.get("properties"), dict):
        return None
    return schema
