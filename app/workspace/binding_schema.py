"""Normalizador de schemas de bindings → CanonicalFormSchema.

Cada tipo de binding (MCP, API, RAG, Tabular) tem schema nativo num
formato diferente. Este módulo converte tudo numa forma canônica única,
que a UI usa pra renderizar form dinâmico e o backend usa pra validar
payload antes de invocar.

Onda A.1 (esta versão): só MCP. Onda A.2+ adiciona os outros 3.

Causa raiz que esta normalização resolve:
- SKILL Context7 declara `## Inputs` com {action, subject, content}
- Engine MCP em build_openai_tools força {operation, query} pro LLM
- LLM tenta "comprimir" → erra → bugs Context7 #1-#5

Com slash command + form dirigido pelo schema da SKILL, o usuário
manda payload DIRETO no shape correto. Zero compressão, zero LLM
intermediation, zero alucinação.
"""
from __future__ import annotations

import re
from typing import Optional


# ───────────────────────────────────────────────────────────────
# Tipos canônicos
# ───────────────────────────────────────────────────────────────


# Tipos suportados no form canônico. Mapeiam direto pros tipos do
# JSON Schema, com adaptação: "enum" é o subtype quando há lista de
# opções (independente de o type JSON Schema ser string).
CANONICAL_FIELD_TYPES = ("string", "number", "integer", "boolean", "enum")


def _make_field(
    *,
    name: str,
    type: str = "string",
    enum: Optional[list[str]] = None,
    required: bool = False,
    description: str = "",
    placeholder: str = "",
    multiline: bool = False,
    default: Optional[object] = None,
) -> dict:
    """Helper: cria 1 field do CanonicalFormSchema, com valores default
    consistentes. Evita typo nos dicts inline."""
    return {
        "name": name,
        "type": type if type in CANONICAL_FIELD_TYPES else "string",
        "enum": list(enum) if enum else None,
        "required": bool(required),
        "description": description or "",
        "placeholder": placeholder or "",
        "multiline": bool(multiline),
        "default": default,
    }


def _infer_multiline(name: str, description: str) -> bool:
    """Heurística pra render: campos cujo nome ou descrição sugerem
    texto longo viram textarea. Mesma heurística do skill_form.html
    pra UX consistente entre dry-run e workspace."""
    if description and len(description) > 80:
        return True
    return bool(re.search(r"content|body|text|payload|prompt|message", name, re.IGNORECASE))


# ───────────────────────────────────────────────────────────────
# Extração do schema declarado pela SKILL em `## Inputs`
# ───────────────────────────────────────────────────────────────


def _extract_inputs_schema(skill_md: str) -> Optional[dict]:
    """Espelha app.routes.skill_dryrun._extract_inputs_schema (canônico).

    Mantemos cópia local pra binding_schema ser autossuficiente em
    contexto de import circular evitado. Lógica idêntica.
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
    import json
    try:
        schema = json.loads(fence.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(schema, dict):
        return None
    if not isinstance(schema.get("properties"), dict):
        return None
    return schema


# ───────────────────────────────────────────────────────────────
# Normalizador MCP
# ───────────────────────────────────────────────────────────────


def _fields_from_json_schema(schema: dict) -> list[dict]:
    """Converte properties + required de um JSON Schema em lista de
    fields canônicos. Preserva enum, description, type."""
    out: list[dict] = []
    props = schema.get("properties") or {}
    required_set = set(r for r in (schema.get("required") or []) if isinstance(r, str))
    for name, meta in props.items():
        if not isinstance(meta, dict):
            meta = {}
        json_type = meta.get("type") or "string"
        # Mapeia JSON Schema type → canonical type
        if isinstance(meta.get("enum"), list) and meta["enum"]:
            canon_type = "enum"
            enum_vals = [str(v) for v in meta["enum"]]
        elif json_type in ("integer",):
            canon_type, enum_vals = "integer", None
        elif json_type in ("number",):
            canon_type, enum_vals = "number", None
        elif json_type in ("boolean",):
            canon_type, enum_vals = "boolean", None
        else:
            canon_type, enum_vals = "string", None
        desc = str(meta.get("description") or "")
        out.append(_make_field(
            name=name,
            type=canon_type,
            enum=enum_vals,
            required=(name in required_set),
            description=desc,
            placeholder=str(meta.get("examples", [""])[0]) if isinstance(meta.get("examples"), list) and meta["examples"] else "",
            multiline=_infer_multiline(name, desc),
            default=meta.get("default"),
        ))
    return out


def _fields_from_operations(ops: list[str]) -> list[dict]:
    """Fallback Onda A.1: SKILL não declara `## Inputs` parseável.
    Form vira `{operation, query}` (igual ao que o engine força hoje).
    UX degrada elegantemente — usuário ainda invoca, só sem precisão
    por field."""
    return [
        _make_field(
            name="operation", type="enum" if ops else "string",
            enum=ops if ops else None,
            required=True,
            description=("Operação a executar. Disponíveis: " + ", ".join(ops)) if ops else "Operação a executar.",
        ),
        _make_field(
            name="query", type="string", required=True,
            description="Consulta/parâmetros para a operação.",
            multiline=True,
        ),
    ]


def normalize_mcp_binding(
    tool: dict,
    skill_md: Optional[str] = None,
) -> dict:
    """Produz CanonicalFormSchema pra uma tool MCP.

    Args:
        tool: dict enriquecido pelo match_with_registry (tem `name`,
            `db_id`/`id`, `operations`, opcionalmente `inputSchema`).
        skill_md: markdown completo da SKILL.md (pra extrair `## Inputs`).

    Returns:
        CanonicalFormSchema = {
            "binding_kind": "mcp",
            "binding_id": str,        # tool.db_id ou tool.id
            "binding_label": str,     # tool.name humano
            "operations": [str],      # operações declaradas no Registry
            "fields": [field_dict],
            "schema_source": "skill_inputs" | "mcp_input_schema" | "legacy_fallback",
        }

    Precedência do schema (mais específico → mais genérico):
    1. ## Inputs da SKILL (mais específico — o autor declarou)
    2. tool.inputSchema (vem do servidor MCP discovery)
    3. Fallback {operation, query} (igual ao engine MCP hoje)

    Onda A.1 sempre vai pelo (1) quando disponível — é a vitória contra
    a compressão LLM. (2) e (3) ficam pra cobertura defensiva.
    """
    binding_id = str(tool.get("db_id") or tool.get("id") or "")
    binding_label = str(tool.get("name") or "(sem nome)")
    operations = list(tool.get("operations") or [])
    # Normaliza operations CSV string → list
    if len(operations) == 1 and isinstance(operations[0], str) and "," in operations[0]:
        operations = [op.strip() for op in operations[0].split(",") if op.strip()]

    # 1. SKILL ## Inputs (preferência)
    skill_schema = _extract_inputs_schema(skill_md or "")
    if skill_schema:
        fields = _fields_from_json_schema(skill_schema)
        schema_source = "skill_inputs"
    else:
        # 2. tool.inputSchema (do servidor MCP)
        tool_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else None
        if tool_schema and isinstance(tool_schema.get("properties"), dict):
            fields = _fields_from_json_schema(tool_schema)
            schema_source = "mcp_input_schema"
        else:
            # 3. Fallback legacy
            fields = _fields_from_operations(operations)
            schema_source = "legacy_fallback"

    return {
        "binding_kind": "mcp",
        "binding_id": binding_id,
        "binding_label": binding_label,
        "operations": operations,
        "fields": fields,
        "schema_source": schema_source,
    }


# ───────────────────────────────────────────────────────────────
# Validação de payload contra CanonicalFormSchema
# ───────────────────────────────────────────────────────────────


def validate_params_against_schema(
    schema: dict,
    params: dict,
) -> tuple[bool, list[str]]:
    """Valida o payload do user contra o CanonicalFormSchema.

    Returns:
        (ok, errors). ok=True quando todos required estão presentes
        e nenhum valor enum é inválido. Type coercion fica pra próxima
        camada (cada engine MCP/API já coerge à sua maneira).
    """
    errors: list[str] = []
    fields_by_name = {f["name"]: f for f in (schema.get("fields") or [])}
    # Required ausentes
    for name, field in fields_by_name.items():
        if field.get("required"):
            val = params.get(name)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                errors.append(f"Campo '{name}' é obrigatório.")
    # Enum inválido
    for name, val in (params or {}).items():
        field = fields_by_name.get(name)
        if not field:
            continue
        if field.get("type") == "enum" and field.get("enum"):
            if val and val not in field["enum"]:
                errors.append(
                    f"Campo '{name}' tem valor '{val}' que não está no enum "
                    f"{field['enum']}."
                )
    return (len(errors) == 0, errors)
