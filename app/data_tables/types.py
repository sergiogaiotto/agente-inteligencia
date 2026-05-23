"""Tipos e enums compartilhados da Onda Tabular.

Centraliza operadores SQL aceitos no Query Builder e limites hardcoded
(paridade com convenção #9 — hardcoded enquanto não houver demanda de
edição via UI; quando houver, migra para platform_settings).
"""

from __future__ import annotations

from enum import Enum


# ─── Limites operacionais ─────────────────────────────────────────

# Linhas máximas devolvidas por uma única query. UI permite limit menor;
# > MAX_ROWS_RETURNED é hard-cap (rejeita com 400).
MAX_ROWS_RETURNED = 1000

# Tamanho máximo do arquivo CSV/XLSX promovido. Acima disso a análise
# rejeita ANTES de tentar carregar no DuckDB (proteção de RAM).
MAX_TABLE_SIZE_MB = 50

# Colunas máximas. Planilhas com > 100 colunas tipicamente são pivot/wide
# e não casam bem com Query Builder (UX vira inviável).
MAX_COLUMNS = 100

# Score mínimo para sugerir promoção via modal automático. Abaixo disso
# a UI mostra warning ("planilha não parece estruturada") mas permite
# forçar manualmente.
TABULAR_READY_THRESHOLD = 0.5


# ─── Operadores aceitos no Query Builder ──────────────────────────

class SqlOperator(str, Enum):
    """Operadores WHERE liberados no MVP.

    Cada operador mapeia para um template SQL parametrizado com bind vars
    (`?`) — nunca interpolação de string. RENDER_TEMPLATES traduz o enum
    para SQL real; o engine valida que `op` pertence a este enum ANTES
    de tocar no SQL (defense in depth contra injeção via payload).
    """

    EQ = "="
    NEQ = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    LIKE = "LIKE"            # case-sensitive
    ILIKE = "ILIKE"          # case-insensitive (DuckDB suporta nativo)
    IN = "IN"                # value = list
    NOT_IN = "NOT IN"        # value = list
    BETWEEN = "BETWEEN"      # value = [low, high]
    IS_NULL = "IS NULL"      # value ignorado
    IS_NOT_NULL = "IS NOT NULL"  # value ignorado


# Operadores que NÃO consomem placeholder (`?`) — IS NULL/IS NOT NULL.
# Render gera só "col IS NULL" sem bind value.
_NO_VALUE_OPS = {SqlOperator.IS_NULL, SqlOperator.IS_NOT_NULL}

# Operadores que consomem MÚLTIPLOS placeholders.
# IN/NOT IN: N placeholders (tamanho da lista).
# BETWEEN: 2 placeholders.
_MULTI_VALUE_OPS = {SqlOperator.IN, SqlOperator.NOT_IN, SqlOperator.BETWEEN}


def render_where_clause(column: str, op: SqlOperator, value):
    """Retorna (clause_sql, bind_values).

    `column` deve ter sido validado contra o schema da tabela ANTES
    desta chamada (não há quoting aqui; column entra inline no SQL).
    Validação fica no service.

    Exemplos:
    - render_where_clause("nome", EQ, "alice") → ("nome = ?", ["alice"])
    - render_where_clause("idade", BETWEEN, [18, 65]) → ("idade BETWEEN ? AND ?", [18, 65])
    - render_where_clause("status", IN, ["a", "b", "c"]) → ("status IN (?, ?, ?)", ["a", "b", "c"])
    - render_where_clause("email", IS_NULL, None) → ("email IS NULL", [])
    """
    if op in _NO_VALUE_OPS:
        return f"{column} {op.value}", []
    if op == SqlOperator.BETWEEN:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"BETWEEN requer lista [low, high]; recebido: {value!r}")
        return f"{column} BETWEEN ? AND ?", list(value)
    if op in (SqlOperator.IN, SqlOperator.NOT_IN):
        if not isinstance(value, (list, tuple)) or len(value) == 0:
            raise ValueError(f"{op.value} requer lista não-vazia; recebido: {value!r}")
        placeholders = ", ".join("?" for _ in value)
        return f"{column} {op.value} ({placeholders})", list(value)
    # Operadores unários (=, !=, >, >=, <, <=, LIKE, ILIKE)
    return f"{column} {op.value} ?", [value]
