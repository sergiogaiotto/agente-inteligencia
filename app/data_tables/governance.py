"""Tier 2 — gates determinísticos que consomem o Catálogo de Dados.

Camada PURA (sem LLM, sem DB): recebe o catálogo já RECONCILIADO
(``app/data_tables/queries.reconcile_catalog``) e materializa o ``pii_category``
— antes puro metadata — como governança REAL, com postura FAIL-SAFE (deny):

- allow-list de coluna   (Gate 3): só colunas humano-curadas como não-PII entram
  no SELECT/WHERE. Não-catalogada (source ausente) ou PII → fora por padrão.
- bloqueio de predicado  (Gate 4): coluna sensível em WHERE/ORDER é bloqueada;
  liberação fina = só igualdade EXATA (=) e só com aprovação explícita do curador
  (fecha oracle de exfiltração por busca binária em row_count).
- mascaramento           (Gate 6): célula INTEIRA → placeholder por categoria
  (``PII_PLACEHOLDERS``), independente de ``dlp.redact``. Última linha de defesa.

Na dúvida, NEGA. Tabela sem catálogo curado → nada consultável até curar.

O formato do catálogo é o de ``reconcile_catalog``::

    {"table": {...}, "columns": [
        {"name", "type", "nullable", "description", "pii_category", "source"}, ...
    ]}

onde ``source`` é ``'human'`` (curado), ``'ai'`` (sugestão volátil) ou ``None``
(coluna do schema vivo SEM entry no catálogo = não catalogada).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from app.data_tables.types import (
    PII_PLACEHOLDERS,
    PiiCategory,
    SqlOperator,
    UNCATALOGED_PLACEHOLDER,
    normalize_pii_category,
)

# Só uma coluna que um HUMANO catalogou explicitamente como não-PII é confiável
# para liberar sem masking. Sugestão de IA não curada ('ai') e coluna não
# catalogada (source ausente) são tratadas como SENSÍVEIS (fail-safe). Hoje, na
# prática, o único writer do catálogo (apply_catalog) grava source='human'; 'ai'
# só existe em sugestão volátil que não persiste.
_TRUSTED_NON_PII_SOURCES = frozenset({"human"})

_NONE = PiiCategory.NONE.value


def _columns(catalog: Any) -> list[dict]:
    """Colunas do catálogo reconciliado. Defensivo: [] em formato inesperado."""
    if not isinstance(catalog, dict):
        return []
    cols = catalog.get("columns")
    if not isinstance(cols, list):
        return []
    return [c for c in cols if isinstance(c, dict)]


def _by_name(catalog: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in _columns(catalog):
        name = c.get("name")
        if name is not None:
            out[name] = c
    return out


def is_column_sensitive(col: Any) -> bool:
    """True se a coluna deve ser tratada como sensível (PII ou desconhecida).

    NÃO-sensível (liberada) só quando: ``pii_category == 'none'`` E ``source``
    confiável (humano afirmou não-PII). Qualquer outra coisa — categoria de PII,
    source ausente (não catalogada) ou sugestão de IA não curada — é sensível.
    """
    if not isinstance(col, dict):
        return True
    if normalize_pii_category(col.get("pii_category")) != _NONE:
        return True
    return col.get("source") not in _TRUSTED_NON_PII_SOURCES


def allowed_cols_from_catalog(
    catalog: Any, pii_columns_allowed: Iterable[str] = ()
) -> list[str]:
    """Colunas liberadas p/ SELECT/WHERE (política deny), na ordem do catálogo.

    = colunas humano-curadas como não-PII + colunas PII que o curador liberou
    EXPLICITAMENTE (``pii_columns_allowed``) — estas últimas ainda serão
    MASCARADAS na saída. Tabela sem catálogo → lista vazia (nada consultável até
    curar).
    """
    approved = set(pii_columns_allowed or ())
    out: list[str] = []
    for c in _columns(catalog):
        name = c.get("name")
        if not name:
            continue
        if not is_column_sensitive(c) or name in approved:
            out.append(name)
    return out


def is_predicate_blocked(
    col_name: str,
    op: Any,
    catalog: Any,
    pii_columns_allowed: Iterable[str] = (),
) -> bool:
    """True se a coluna NÃO pode aparecer num predicado (WHERE/ORDER) — Gate 4.

    - coluna humano-não-PII → liberada (False).
    - coluna sensível → bloqueada (True), salvo se liberada pelo curador
      (``pii_columns_allowed``) E o operador for igualdade EXATA (``=``).
      range/LIKE/ILIKE/IN/BETWEEN/etc. permanecem bloqueados MESMO aprovados
      (fecham o oracle de exfiltração por busca binária em row_count).
    - coluna desconhecida (fora do catálogo) → bloqueada (True).
    """
    col = _by_name(catalog).get(col_name)
    if col is None:
        return True  # desconhecida → fail-safe
    if not is_column_sensitive(col):
        return False
    if col_name in set(pii_columns_allowed or ()):
        op_val = op.value if hasattr(op, "value") else str(op)
        if op_val == SqlOperator.EQ.value:
            return False  # único afrouxamento: igualdade exata aprovada
    return True


def _mask_token(col: Any) -> Optional[str]:
    """Placeholder p/ uma coluna sensível; None se não-sensível (não mascara)."""
    if not is_column_sensitive(col):
        return None
    pii = normalize_pii_category(col.get("pii_category")) if isinstance(col, dict) else _NONE
    if pii != _NONE:
        return PII_PLACEHOLDERS.get(pii, PII_PLACEHOLDERS[PiiCategory.OTHER.value])
    return UNCATALOGED_PLACEHOLDER  # sensível por não-catalogada


def mask_rows_pii_only(rows: list[dict], catalog: Any) -> list[dict]:
    """Higiene de EXIBIÇÃO do Tier 1: mascara SÓ colunas explicitamente
    catalogadas como PII (``pii_category != none``) — célula inteira vira o
    placeholder da categoria.

    Diferente de ``mask_rows_by_catalog`` (gate do Tier 2, fail-safe: coluna
    não-catalogada = sensível), aqui coluna não-catalogada PASSA: no Tier 1 o
    AUTOR da skill escolheu as colunas do select explicitamente (caminho
    confiado) e a maioria das tabelas não tem catálogo curado — mascarar tudo
    inutilizaria o render default. PII catalogada, porém, nunca vaza num render
    default (um ## Response Template custom pode citá-la — escolha consciente
    do autor). NÃO muta ``rows``.
    """
    cols = _columns(catalog)
    tokens: dict[str, str] = {}
    for c in cols:
        name = c.get("name")
        if not name:
            continue
        pii = normalize_pii_category(c.get("pii_category"))
        if pii != _NONE:
            tokens[name] = PII_PLACEHOLDERS.get(pii, PII_PLACEHOLDERS[PiiCategory.OTHER.value])
    if not tokens:
        return list(rows or [])
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        out.append({k: (tokens[k] if k in tokens else v) for k, v in row.items()})
    return out


def mask_rows_by_catalog(
    rows: list[dict],
    columns: Optional[Iterable[str]],
    catalog: Any,
) -> list[dict]:
    """Mascara a CÉLULA INTEIRA de colunas sensíveis — Gate 6, última defesa.

    Toda coluna que NÃO seja humano-curada-como-não-PII tem suas células trocadas
    por um placeholder determinístico (categoria de PII → ``PII_PLACEHOLDERS``;
    sensível por desconhecida → ``UNCATALOGED_PLACEHOLDER``). Colunas liberadas
    passam intactas. ``None`` / ``int`` / tipos não-string são tratados: a célula
    INTEIRA vira o placeholder, sem depender do valor. NÃO muta ``rows`` de
    entrada (retorna nova lista).

    ``columns`` (ordem do resultado) é opcional — a decisão é por NOME de coluna
    via catálogo; uma coluna PRESENTE na row mas AUSENTE no catálogo é tratada
    como desconhecida (mascarada).
    """
    by_name = _by_name(catalog)

    def token_for(name: str) -> Optional[str]:
        col = by_name.get(name)
        if col is None:
            return UNCATALOGED_PLACEHOLDER  # no resultado, mas fora do catálogo
        return _mask_token(col)

    # Cache de token por nome de coluna; pré-aquece com a ordem do resultado.
    token_cache: dict[str, Optional[str]] = {}
    for name in list(columns or []):
        token_cache[name] = token_for(name)

    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        new_row: dict = {}
        for k, v in row.items():
            if k not in token_cache:
                token_cache[k] = token_for(k)
            tok = token_cache[k]
            new_row[k] = tok if tok is not None else v
        out.append(new_row)
    return out
