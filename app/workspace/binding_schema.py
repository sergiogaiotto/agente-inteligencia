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
# Normalizador API — Onda A.2
# ───────────────────────────────────────────────────────────────


def _extract_template_vars_from_api_bindings(parsed_bindings: list) -> list[str]:
    """Extrai variáveis Jinja2 (`{{ inputs.X }}`, `{{ X }}`) referenciadas
    em qualquer campo string das api_bindings parsed.

    Usado como fallback quando SKILL não declarou ## Inputs com schema
    parseável: sintetizamos fields a partir das vars realmente usadas
    nos templates HTTP.

    Limitação: regex simples — não casa expressões complexas com filtros,
    aritmética, etc. Casa só padrão `{{ name }}` ou `{{ inputs.name }}`.
    Suficiente pra UX inicial — autor pode declarar ## Inputs pra ter
    controle preciso.
    """
    if not parsed_bindings:
        return []
    var_pattern = re.compile(r"\{\{\s*(?:inputs\.)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
    seen: set[str] = set()
    out: list[str] = []

    def _walk(value):
        if isinstance(value, str):
            for m in var_pattern.finditer(value):
                name = m.group(1)
                # Filtra contexto interno (session_id, context.*, etc.) —
                # esses não devem aparecer no form pro user.
                if name in ("session_id", "context", "inputs", "outputs"):
                    continue
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for v in value:
                _walk(v)

    for binding in parsed_bindings:
        if isinstance(binding, dict):
            _walk(binding)
    return out


def normalize_api_binding_from_skill(
    skill: dict,
    skill_md: Optional[str] = None,
    parsed_skill=None,
) -> Optional[dict]:
    """Produz CanonicalFormSchema pra invocação de uma SKILL declarativa
    com `## API Bindings`. Diferente de MCP (1 item por tool), API é
    1 item por SKILL — porque api_bindings compartilham `## Inputs`
    via Jinja2 e rodam como unidade através do execute_declarative.

    Args:
        skill: row do skills_repo (id, name, kind, raw_content).
        skill_md: alias pra raw_content (passe diretamente quando já
            tiver carregado).
        parsed_skill: ParsedSkill já calculado (opcional; se None,
            chamamos parse_skill_md(skill_md) aqui).

    Returns:
        CanonicalFormSchema ou None quando a SKILL NÃO é declarativa
        OU não tem ## API Bindings. Caller decide se omite do contexto.

    Precedência do schema (igual ao MCP):
    1. SKILL ## Inputs (explícito, com types/required)
    2. Vars sintetizadas das api_bindings_parsed (fallback)

    Onda A.2: NÃO toca em ## Data Tables. Se a SKILL declarativa tem
    SÓ data_tables (sem api_bindings), retorna None — A.3 cobre isso.
    """
    md = (skill_md if skill_md is not None else (skill.get("raw_content") or "")) or ""
    if not md.strip():
        return None

    # Parse só quando necessário (evita import circular em algum caller).
    if parsed_skill is None:
        try:
            from app.skill_parser.parser import parse_skill_md
            parsed_skill = parse_skill_md(md)
        except Exception:
            return None

    if not parsed_skill:
        return None

    # Gate 1: SKILL precisa ser declarativa
    exec_mode = (
        getattr(parsed_skill, "execution_mode", "")
        or getattr(getattr(parsed_skill, "frontmatter", None), "execution_mode", "")
        or ""
    )
    if exec_mode != "declarative":
        return None

    # Gate 2: tem ## API Bindings parseado
    api_bindings = getattr(parsed_skill, "api_bindings_parsed", None) or []
    if not api_bindings:
        return None

    # 1. ## Inputs como schema (preferência)
    inputs_schema = _extract_inputs_schema(md)
    if inputs_schema:
        fields = _fields_from_json_schema(inputs_schema)
        schema_source = "skill_inputs"
    else:
        # 2. Fallback: vars dos templates Jinja
        template_vars = _extract_template_vars_from_api_bindings(api_bindings)
        if not template_vars:
            # SKILL declarativa sem inputs e sem vars referenciadas —
            # ainda invocável (params={}), só não terá form. Retorna
            # vazio mas mantém schema_source pra UI saber.
            fields = []
        else:
            fields = [
                _make_field(
                    name=v, type="string", required=True,
                    description=f"Variável referenciada nas API Bindings",
                    multiline=_infer_multiline(v, ""),
                )
                for v in template_vars
            ]
        schema_source = "template_vars" if not inputs_schema else "skill_inputs"

    # Label: nome da skill + dica de binding_kind
    skill_name = skill.get("name") or "(skill sem nome)"

    return {
        "binding_kind": "api",
        "binding_id": str(skill.get("id") or ""),
        "binding_label": skill_name,
        "operations": [],  # N/A pra API — execução é skill inteira
        "fields": fields,
        "schema_source": schema_source,
        # Metadata pra UI: quantidade de bindings, list de IDs (telemetria)
        "api_meta": {
            "binding_count": len(api_bindings),
            "binding_ids": [b.get("id") for b in api_bindings if isinstance(b, dict)],
        },
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
