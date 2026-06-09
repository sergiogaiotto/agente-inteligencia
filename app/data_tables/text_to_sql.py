"""Tier 2 — compilador NL→struct (bancada "Perguntar à Tabela").

PURO + LLM INJETADO (espelha ``data_tables/catalog.py::generate_catalog_suggestion``):
o LLM emite uma INTENÇÃO estruturada ``{select, filters, order_by, limit}`` —
NUNCA SQL cru. O struct é a lei:

1. ``build_compile_messages``  — PURA: monta o prompt expondo SÓ as colunas
   LIBERADAS pelo Catálogo (allow-list de ``governance``). O LLM nunca vê coluna
   PII/não-catalogada → não consegue referenciá-la.
2. ``parse_compiled_query``    — DEFENSIVA: tira cercas markdown, ``json.loads``
   em try/except, NUNCA levanta; formato inesperado → struct vazio.
3. ``validate_compiled_query`` — DETERMINÍSTICA: reconcilia por NOME contra a
   allow-list, valida ``op ∈ SqlOperator``, bloqueia predicado PII (Gate 4) e
   aplica caps. Devolve o struct saneado + a lista de itens BLOQUEADOS.
4. ``render_sql_preview``      — DRY-RUN: SQL com ``?`` (sem bind values), só p/
   o operador ver o que seria executado.

Este módulo NÃO executa e NÃO persiste. O LLM é fonte de INTENÇÃO, nunca da
verdade estrutural — coluna alucinada é descartada (mesmo DNA anti-alucinação do
Catálogo: IA propõe, validação determinística doma).
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from app.data_tables.governance import (
    allowed_cols_from_catalog,
    is_predicate_blocked,
)
from app.data_tables.types import (
    MAX_ROWS_RETURNED,
    SqlOperator,
    TIER2_MAX_FILTERS,
    render_where_clause,
)

_DEFAULT_LIMIT = 100


def _quote_ident(name: str) -> str:
    """Escapa identificador para DuckDB (paridade com tabular._quote_ident)."""
    return '"' + str(name or "").replace('"', '""') + '"'


def _empty_struct() -> dict:
    return {"select": [], "filters": [], "order_by": [], "limit": _DEFAULT_LIMIT}


# ─── 1. PROMPT (puro) ────────────────────────────────────────────


def build_compile_messages(
    question: str,
    table_name: str,
    table_description: str,
    allowed_columns: list[dict],
    sample_rows: list,
) -> list[dict]:
    """Monta as messages do LLM p/ compilar a pergunta em struct. Função PURA.

    `allowed_columns`: list[{name, type, description}] — SÓ as colunas LIBERADAS
    (allow-list do Catálogo). `sample_rows`: amostra JÁ restrita a essas colunas
    (sem valores PII); pode ser [] (omitida p/ bases sensíveis pelo caller).
    """
    cols = "\n".join(
        f"- {c.get('name')}: {c.get('type')}"
        + (f" — {c.get('description')}" if c.get("description") else "")
        for c in (allowed_columns or [])
        if isinstance(c, dict) and c.get("name")
    )
    ops = [o.value for o in SqlOperator]
    sample_block = ""
    if sample_rows:
        dump = json.dumps(sample_rows[:5], ensure_ascii=False, default=str)
        sample_block = f"\n\nAMOSTRA (só dica de formato — NÃO invente valores):\n{dump[:1500]}"
    system = (
        "Você compila uma PERGUNTA em português numa CONSULTA ESTRUTURADA sobre UMA tabela. "
        "Você NÃO escreve SQL. Responda APENAS com JSON válido (sem markdown, sem cercas ```), "
        'no formato: {"select": [nomes de coluna], "filters": [{"col": nome, "op": operador, '
        '"value": valor}], "order_by": ["coluna" ou "coluna DESC"], "limit": inteiro}. '
        "REGRAS: use SOMENTE as colunas listadas, com os NOMES EXATOS; NÃO invente colunas; "
        f"'op' DEVE pertencer a {ops}; se a pergunta não casar com nenhuma coluna disponível, "
        f"devolva select vazio; limit no máximo {MAX_ROWS_RETURNED}; no máximo {TIER2_MAX_FILTERS} "
        "filtros. Agregação (COUNT/SUM/GROUP BY) e JOIN NÃO são suportados — apenas selecione as "
        "colunas relevantes."
    )
    user = (
        f"Tabela: {table_name}\n"
        + (f"Descrição: {table_description}\n" if table_description else "")
        + f"Colunas disponíveis:\n{cols or '(nenhuma)'}{sample_block}\n\n"
        f"Pergunta do usuário: {question}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ─── 2. PARSE (defensivo, nunca levanta) ─────────────────────────


def parse_compiled_query(content: str) -> dict:
    """Parse DEFENSIVO do output do LLM → struct normalizado. NUNCA levanta.

    Tira cercas markdown, ``json.loads`` em try/except; JSON inválido / formato
    inesperado → struct vazio. NÃO reconcilia/valida ainda (isso é determinístico
    em ``validate_compiled_query``) — aqui só normaliza a forma.
    """
    raw = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    select = [c for c in (data.get("select") or []) if isinstance(c, str)]

    filters = []
    for f in (data.get("filters") or []):
        if not isinstance(f, dict):
            continue
        col = f.get("col") or f.get("column")
        op = f.get("op") or f.get("operator")
        filters.append({
            "col": col if isinstance(col, str) else None,
            "op": (op.value if hasattr(op, "value") else str(op)) if op else None,
            "value": f.get("value"),
        })

    order_by = [ob for ob in (data.get("order_by") or []) if isinstance(ob, str)]

    # None → default; 0/negativo são preservados p/ o clamp determinístico de
    # validate_compiled_query (não usar `or`: 0 é falsy e viraria o default).
    lim = data.get("limit")
    try:
        limit = int(lim) if lim is not None else _DEFAULT_LIMIT
    except (ValueError, TypeError):
        limit = _DEFAULT_LIMIT

    return {"select": select, "filters": filters, "order_by": order_by, "limit": limit}


# ─── 3. VALIDATE (determinístico, consome o Catálogo) ────────────


def validate_compiled_query(
    raw: dict,
    catalog: Any,
    pii_columns_allowed: list[str] | tuple[str, ...] = (),
) -> dict:
    """Reconcilia + valida o struct contra a allow-list do Catálogo (Gates 3/4/8).

    - ``select``/``filters`` só com colunas da allow-list (humano-não-PII + PII
      liberada explicitamente pelo curador); coluna fora → BLOQUEADA.
    - ``op`` deve pertencer ao enum FECHADO ``SqlOperator``.
    - predicado em coluna sensível → bloqueado (``is_predicate_blocked``); só
      igualdade exata aprovada passa.
    - ``ORDER BY`` SÓ em coluna humano-não-PII (ordenar PII é oracle, mesmo
      aprovada) — usa a allow-list ESTRITA (sem aprovações).
    - caps: ``limit ∈ [1, MAX_ROWS_RETURNED]``, ``len(filters) ≤ TIER2_MAX_FILTERS``.

    Retorna ``{"compiled": <struct saneado>, "blocked": [motivos PT-BR]}``.
    """
    allowed = set(allowed_cols_from_catalog(catalog, pii_columns_allowed))
    orderable = set(allowed_cols_from_catalog(catalog))  # estrita: sem PII aprovada
    valid_ops = {o.value for o in SqlOperator}
    blocked: list[str] = []

    select: list[str] = []
    for c in raw.get("select") or []:
        if c in allowed:
            select.append(c)
        else:
            blocked.append(f"coluna '{c}' fora da allow-list do Catálogo")

    filters: list[dict] = []
    for f in raw.get("filters") or []:
        col, op = f.get("col"), f.get("op")
        if not col or not op:
            blocked.append(f"filtro inválido (col/op ausente): {f}")
            continue
        if op not in valid_ops:
            blocked.append(f"operador '{op}' não suportado")
            continue
        if is_predicate_blocked(col, op, catalog, pii_columns_allowed):
            blocked.append(
                f"coluna '{col}' não permitida em filtro "
                "(PII/não-catalogada, ou operador não liberado)"
            )
            continue
        if col not in allowed:
            blocked.append(f"coluna '{col}' fora da allow-list do Catálogo")
            continue
        filters.append({"col": col, "op": op, "value": f.get("value")})
    if len(filters) > TIER2_MAX_FILTERS:
        blocked.append(f"filtros truncados em {TIER2_MAX_FILTERS} (máximo)")
        filters = filters[:TIER2_MAX_FILTERS]

    order_by: list[str] = []
    for ob in raw.get("order_by") or []:
        tokens = ob.strip().split()
        if not tokens:
            continue
        col = tokens[0]
        direction = tokens[1].upper() if len(tokens) > 1 else "ASC"
        if col not in orderable:
            blocked.append(f"order_by '{ob}' não permitido (coluna sensível/fora da allow-list)")
            continue
        if direction not in ("ASC", "DESC"):
            blocked.append(f"direção inválida em order_by '{ob}'")
            continue
        order_by.append(f"{col} {direction}" if len(tokens) > 1 else col)

    # None → default; 0/negativo clampam p/ 1 (não usar `or`: 0 é falsy).
    lim = raw.get("limit")
    try:
        limit = int(lim) if lim is not None else _DEFAULT_LIMIT
    except (ValueError, TypeError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_ROWS_RETURNED))

    return {
        "compiled": {"select": select, "filters": filters, "order_by": order_by, "limit": limit},
        "blocked": blocked,
    }


# ─── 4. SQL PREVIEW (dry-run, só `?`) ────────────────────────────


def render_sql_preview(compiled: dict) -> str:
    """SQL com ``?`` (sem bind values) só p/ exibição. Best-effort: um filtro
    malformado vira ``col op ?`` em vez de derrubar. Espelha o SELECT do
    ``execute_query`` (FROM travado em ``data``)."""
    select = compiled.get("select") or []
    sel = ", ".join(_quote_ident(c) for c in select) if select else "*"
    sql = f"SELECT {sel} FROM data"

    where_parts: list[str] = []
    for f in compiled.get("filters") or []:
        col, op = f.get("col"), f.get("op")
        try:
            clause, _ = render_where_clause(_quote_ident(col), SqlOperator(op), f.get("value"))
        except (ValueError, KeyError):
            clause = f"{_quote_ident(col)} {op} ?"
        where_parts.append(clause)
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    order_by = compiled.get("order_by") or []
    if order_by:
        parts = []
        for ob in order_by:
            tokens = str(ob).split()
            col = _quote_ident(tokens[0]) if tokens else ""
            parts.append(f"{col} {tokens[1]}" if len(tokens) > 1 else col)
        sql += " ORDER BY " + ", ".join(parts)

    sql += " LIMIT ?"
    return sql


# ─── Orquestrador (LLM injetado) ─────────────────────────────────


async def compile_question(
    row: dict,
    catalog: Any,
    sample_rows: list,
    question: str,
    llm_complete: Callable[[list], Awaitable[str]],
    pii_columns_allowed: list[str] | tuple[str, ...] = (),
) -> dict:
    """Compila a pergunta em struct VALIDADO. NÃO executa, NÃO persiste.

    `llm_complete(messages) -> content` é injetado pelo endpoint (caminho
    resiliente do Wizard, temperature=0 + JSON-mode). Tabela sem coluna liberada
    → retorna struct vazio + `note` (fail-safe: cure o Catálogo antes).

    Retorna ``{compiled, blocked, sql_preview, allowed_columns, note}``.
    """
    catalog = catalog if isinstance(catalog, dict) else {}
    allowed_names = allowed_cols_from_catalog(catalog, pii_columns_allowed)
    if not allowed_names:
        return {
            "compiled": _empty_struct(),
            "blocked": [],
            "sql_preview": "",
            "allowed_columns": [],
            "note": "Nenhuma coluna liberada — cure o Catálogo de Dados (PII por coluna) desta tabela antes de perguntar.",
        }

    allowed_set = set(allowed_names)
    allowed_columns = [
        {"name": c.get("name"), "type": c.get("type"), "description": c.get("description")}
        for c in (catalog.get("columns") or [])
        if isinstance(c, dict) and c.get("name") in allowed_set
    ]
    table_desc = (catalog.get("table") or {}).get("description") or row.get("description") or ""
    messages = build_compile_messages(
        question, row.get("name") or "", table_desc, allowed_columns, sample_rows or []
    )
    content = await llm_complete(messages)
    raw = parse_compiled_query(content)
    result = validate_compiled_query(raw, catalog, pii_columns_allowed)
    result["sql_preview"] = render_sql_preview(result["compiled"])
    result["allowed_columns"] = allowed_names
    result["note"] = ""
    return result
