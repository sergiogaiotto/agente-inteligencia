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
    """Onda B: delega pro helper canônico app.skill_parser.inputs_schema.
    Mantemos o nome local pra preservar back-compat dos callers existentes."""
    from app.skill_parser.inputs_schema import extract_inputs_schema
    return extract_inputs_schema(skill_md or "")


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
    """Alias de back-compat — chama _extract_template_vars (versão Onda
    A.3 que aceita api_bindings OU data_tables OU qualquer mix de dicts)."""
    return _extract_template_vars(parsed_bindings)


def _extract_template_vars(parsed_bindings: list) -> list[str]:
    """Extrai variáveis Jinja2 (`{{ inputs.X }}`, `{{ X }}`) referenciadas
    em qualquer campo string de uma lista de bindings parsed (api_bindings
    OU data_tables — ambos compartilham o mesmo modelo de templating).

    Usado como fallback quando SKILL não declarou ## Inputs com schema
    parseável: sintetizamos fields a partir das vars realmente usadas
    nos templates.

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
                if name in ("session_id", "context", "inputs", "outputs", "tables"):
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
    """Back-compat alias (Onda A.2 → A.3). Delega pro normalizer
    declarativo generalizado, mas retorna None quando o resultado seria
    kind=tabular (a.2 não suportava). Use normalize_declarative_skill_binding
    direto pra novos callers."""
    result = normalize_declarative_skill_binding(skill, skill_md, parsed_skill)
    if result is None:
        return None
    # API-only ou hybrid → retorna; tabular-only → omite (A.2 não enxergava)
    if result.get("binding_kind") == "api":
        return result
    return None


def normalize_declarative_skill_binding(
    skill: dict,
    skill_md: Optional[str] = None,
    parsed_skill=None,
) -> Optional[dict]:
    """Produz CanonicalFormSchema pra SKILL declarativa que tenha
    ## API Bindings, ## Data Tables, ou ambos.

    Modelo: 1 item por SKILL (não por binding). Razão: api_bindings_parsed
    E data_tables_parsed compartilham `## Inputs` via Jinja2 e rodam como
    UNIDADE através de execute_declarative.

    binding_kind reflete o conteúdo:
    - "api"     → SKILL tem api_bindings (com ou sem data_tables hybrid)
    - "tabular" → SKILL tem só data_tables (sem api_bindings)

    Precedência do schema:
    1. SKILL ## Inputs explícito
    2. Template vars de api_bindings + data_tables (fallback)

    Onda A.3: introduz suporte a tabular-only SKILLs.
    """
    md = (skill_md if skill_md is not None else (skill.get("raw_content") or "")) or ""
    if not md.strip():
        return None

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

    # Gate 2: tem api_bindings_parsed OU data_tables_parsed
    api_bindings = getattr(parsed_skill, "api_bindings_parsed", None) or []
    data_tables = getattr(parsed_skill, "data_tables_parsed", None) or []
    if not api_bindings and not data_tables:
        return None

    # binding_kind: api wins quando há api_bindings (cobre hybrid também
    # — a UI ainda invoca o execute_declarative que roda os 2 grupos).
    binding_kind = "api" if api_bindings else "tabular"

    # 1. ## Inputs como schema (preferência)
    inputs_schema = _extract_inputs_schema(md)
    if inputs_schema:
        fields = _fields_from_json_schema(inputs_schema)
        schema_source = "skill_inputs"
    else:
        # 2. Fallback: vars de templates (api_bindings + data_tables)
        template_vars = _extract_template_vars(api_bindings + data_tables)
        if not template_vars:
            fields = []
        else:
            ref_label = (
                "API Bindings" if api_bindings and not data_tables
                else ("Data Tables" if data_tables and not api_bindings
                else "API Bindings + Data Tables")
            )
            fields = [
                _make_field(
                    name=v, type="string", required=True,
                    description=f"Variável referenciada em {ref_label}",
                    multiline=_infer_multiline(v, ""),
                )
                for v in template_vars
            ]
        schema_source = "template_vars"

    skill_name = skill.get("name") or "(skill sem nome)"

    return {
        "binding_kind": binding_kind,
        "binding_id": str(skill.get("id") or ""),
        "binding_label": skill_name,
        "operations": [],
        "fields": fields,
        "schema_source": schema_source,
        # Metadata: o que vai realmente rodar quando invocar
        "api_meta": {
            "binding_count": len(api_bindings),
            "binding_ids": [b.get("id") for b in api_bindings if isinstance(b, dict)],
            "tables_count": len(data_tables),
            "tables_ids": [t.get("id") for t in data_tables if isinstance(t, dict)],
        },
    }


# ───────────────────────────────────────────────────────────────
# Normalizador RAG — Onda A.3
# ───────────────────────────────────────────────────────────────


def normalize_rag_binding(source: dict) -> dict:
    """Produz CanonicalFormSchema pra busca direta em 1 knowledge_source.

    Diferente de API/MCP, RAG tem schema FIXO: {query, top_n}. O servidor
    real (Retriever.search) só aceita esses 2 params + allowed_source_ids
    (gerenciado por nós). Por isso não há precedência de schema source.

    Args:
        source: row do knowledge_sources (id, name, source_type,
            confidentiality_label, kb_mode, ...).

    Returns:
        CanonicalFormSchema com kind="rag", binding_id=source.id,
        fields=[query, top_n], + rag_meta com metadata da source pra
        UI exibir contexto (tipo, confidencialidade, modo).
    """
    return {
        "binding_kind": "rag",
        "binding_id": str(source.get("id") or ""),
        "binding_label": source.get("name") or "(source sem nome)",
        "operations": [],
        "fields": [
            _make_field(
                name="query",
                type="string",
                required=True,
                multiline=True,
                description="O que buscar nesta base. Texto livre — RAG usa BM25 + vetorial.",
                placeholder="Ex.: política de reembolso",
            ),
            _make_field(
                name="top_n",
                type="integer",
                required=False,
                default=5,
                description="Quantos trechos retornar (1-50).",
            ),
        ],
        "schema_source": "rag_fixed",
        "rag_meta": {
            "source_type": source.get("source_type") or "",
            "confidentiality": source.get("confidentiality_label") or "internal",
            "kb_mode": source.get("kb_mode") or "hybrid",
            "authorized": bool(source.get("authorized", 0)),
            # Embedding info: user precisa saber qual model/dim está ativo
            # pra debugar quando RAG vetorial não devolve nada (ex: collection
            # criada com dim antiga, ou provider trocado em /settings sem reindex).
            # Best-effort: se settings/qdrant não disponíveis (import circular,
            # ambiente de teste), cai pra '?'/0 sem quebrar.
            "embedding_provider": _safe_get_embedding_provider(),
            "embedding_dim": _safe_get_embedding_dim(),
        },
    }


def _safe_get_embedding_provider() -> str:
    """Retorna o embedding provider ativo (qwen3/azure/etc.) sem propagar
    exception. Caller usa pra UX info; falha não pode derrubar render."""
    try:
        from app.core.config import get_settings
        return (get_settings().embedding_provider or "azure").lower()
    except Exception:
        return "?"


def _safe_get_embedding_dim() -> int:
    """Retorna dim do embedder ativo. Best-effort.

    Onda Q: helper migrou de qdrant_store pra embedder.py (backend-neutral).
    """
    try:
        from app.evidence.embedder import get_active_embedding_dim
        return int(get_active_embedding_dim())
    except Exception:
        return 0


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
