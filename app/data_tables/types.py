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


# ─── Catálogo de Dados (Onda Catálogo) ────────────────────────────

class PiiCategory(str, Enum):
    """Categorias de PII por coluna no Catálogo de Dados.

    Enum FECHADO (paridade com SqlOperator): validado server-side tanto na
    reconciliação da sugestão da IA quanto na curadoria humana (PUT). Valor fora
    do enum é coagido para NONE (sugestão) ou rejeitado (curadoria). A partir do
    Tier 2 (text-to-SQL governado) DEIXA de ser puro metadata: a camada
    determinística ``app/data_tables/governance.py`` o consome como allow-list de
    coluna, mascaramento célula-inteira por categoria e bloqueio de predicado
    (liberação fina via curadoria humana de saved_queries — HITL).
    """

    NONE = "none"
    CPF = "cpf"
    CNPJ = "cnpj"
    EMAIL = "email"
    PHONE = "phone"
    NAME = "name"
    ADDRESS = "address"
    FINANCIAL = "financial"
    HEALTH = "health"
    BIOMETRIC = "biometric"
    OTHER = "other"


def normalize_pii_category(value) -> str:
    """Coage qualquer valor para uma PiiCategory válida (string).

    Fora do enum / None / tipo inesperado → 'none' (fail-safe neutro). É o ponto
    onde o output do LLM é domado: o modelo NUNCA injeta categoria fora do enum.
    """
    try:
        return PiiCategory(str(value).strip().lower()).value
    except (ValueError, AttributeError):
        return PiiCategory.NONE.value


# ─── Tier 2 — text-to-SQL governado: constantes dos gates ─────────
# Consumidas por app/data_tables/governance.py para materializar o pii_category
# (antes puro metadata) como allow-list + mascaramento REAIS.

# Nº máximo de filtros (WHERE) numa consulta Tier 2. Hardcoded como os demais
# limites operacionais (MAX_ROWS_RETURNED etc.) — vira platform_settings só com
# demanda de edição via UI. Defesa contra payload gigante de predicados.
TIER2_MAX_FILTERS = 20

# Mapa FECHADO categoria de PII → placeholder de mascaramento (substitui a CÉLULA
# INTEIRA). Cobre TODAS as categorias de PiiCategory EXCETO NONE (não-PII não é
# mascarada). Determinístico e INDEPENDENTE de dlp.redact (que só casa formatos
# pontuados em texto e é no-op em int/None/valor cru — vazaria metade do enum).
PII_PLACEHOLDERS = {
    PiiCategory.CPF.value: "[CPF]",
    PiiCategory.CNPJ.value: "[CNPJ]",
    PiiCategory.EMAIL.value: "[EMAIL]",
    PiiCategory.PHONE.value: "[TELEFONE]",
    PiiCategory.NAME.value: "[NOME]",
    PiiCategory.ADDRESS.value: "[ENDEREÇO]",
    PiiCategory.FINANCIAL.value: "[FINANCEIRO]",
    PiiCategory.HEALTH.value: "[SAÚDE]",
    PiiCategory.BIOMETRIC.value: "[BIOMÉTRICO]",
    PiiCategory.OTHER.value: "[PII]",
}

# Placeholder para coluna sensível por ser DESCONHECIDA (não catalogada por humano
# como não-PII). Distinto das categorias: sinaliza "catalogue para liberar/ver".
UNCATALOGED_PLACEHOLDER = "[NÃO CATALOGADO]"

# Placeholder genérico p/ coluna MASCARADA cuja categoria é 'none' (o curador
# escolheu mascarar uma coluna não-PII via tratamento de saída).
MASK_PLACEHOLDER = "[PROTEGIDO]"


# ─── Catálogo: TRATAMENTO DE SAÍDA por coluna (separa do pii_category) ─────
# DECISÃO (2026-06-10, com o usuário): a CLASSIFICAÇÃO (pii_category — o que o
# dado É) e o TRATAMENTO DE EXIBIÇÃO (como mostrar na saída) são ortogonais. Antes
# o pii_category fazia os dois (qualquer PII → mascarava), forçando o curador a
# MENTIR na classificação (marcar financial→none) só p/ exibir o valor. Agora o
# tratamento é um campo PRÓPRIO, com DEFAULT derivado da categoria (não-PII=show,
# PII=mask) — comportamento de fábrica idêntico ao anterior, mas sobrescrevível.
# Dirige SÓ a EXIBIÇÃO (render default/NL). A allow-list/predicado do Tier 2
# (texto livre) segue dirigida pela CLASSIFICAÇÃO (exibir ≠ consultável).

class OutputTreatment(str, Enum):
    """Tratamento de saída por coluna no Catálogo. Enum FECHADO."""

    SHOW = "show"          # Exibir — valor cru
    MASK = "mask"          # Mascarar — placeholder total ([CATEGORIA]/[PROTEGIDO])
    SUPPRESS = "suppress"  # Suprimir — remove a coluna da saída
    # PARTIAL = "partial"  # (PR2) revela parte — lógica por categoria


def default_treatment_for(pii_category) -> str:
    """Default do tratamento derivado da CLASSIFICAÇÃO: não-PII exibe; PII mascara.

    Preserva o comportamento de fábrica (PII mascarada) quando o curador não
    sobrescreve explicitamente o tratamento.
    """
    if normalize_pii_category(pii_category) == PiiCategory.NONE.value:
        return OutputTreatment.SHOW.value
    return OutputTreatment.MASK.value


def normalize_output_treatment(value) -> "str | None":
    """Coage p/ OutputTreatment válido. None / fora do enum → None (= usar default
    derivado da categoria). Diferente de normalize_pii_category: aqui None é
    SIGNIFICATIVO (ausência = 'herda o default'), então NÃO coage p/ um valor.
    """
    if value is None:
        return None
    try:
        return OutputTreatment(str(value).strip().lower()).value
    except (ValueError, AttributeError):
        return None


def effective_treatment(pii_category, output_treatment) -> str:
    """Tratamento EFETIVO de uma coluna: o override do curador, senão o default
    derivado da categoria. É o que o masking de exibição consome."""
    t = normalize_output_treatment(output_treatment)
    return t if t is not None else default_treatment_for(pii_category)
