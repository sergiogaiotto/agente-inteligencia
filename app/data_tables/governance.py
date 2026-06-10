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
    MASK_PLACEHOLDER,
    PII_PLACEHOLDERS,
    OutputTreatment,
    PiiCategory,
    SqlOperator,
    UNCATALOGED_PLACEHOLDER,
    effective_treatment,
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


def _mask_token_for_category(pii_category: str) -> str:
    """Placeholder de uma coluna MASCARADA: por categoria de PII, ou genérico
    ([PROTEGIDO]) quando a categoria é 'none' (curador mascarou uma não-PII)."""
    if pii_category != _NONE:
        return PII_PLACEHOLDERS.get(pii_category, PII_PLACEHOLDERS[PiiCategory.OTHER.value])
    return MASK_PLACEHOLDER


# ─── Revelação PARCIAL (ciente da categoria) ─────────────────────


def _partial_tail(value: Any, keep: int = 4) -> str:
    """Revela os últimos `keep` chars; mascara o resto (identificadores).
    Prefixo fixo de 4 pontos → não revela o comprimento original."""
    s = str(value)
    return ("••••" + s[-keep:]) if len(s) > keep else s


def _partial_email(value: Any) -> str:
    s = str(value)
    if "@" not in s:
        return _partial_tail(s)
    local, _, domain = s.partition("@")
    return f"{local[:1]}•••@{domain}" if local else f"•••@{domain}"


def _partial_name(value: Any) -> str:
    parts = str(value).split()
    if len(parts) <= 1:
        return _partial_tail(value)
    return parts[0] + " " + " ".join(f"{p[:1]}." for p in parts[1:] if p)


# Faixas de magnitude p/ valores financeiros — k-anonymity leve: revela a ORDEM
# de grandeza, NUNCA o valor exato. PT-BR.
_FINANCIAL_BANDS = (
    (100, "< R$ 100"),
    (1_000, "R$ 100 – 1 mil"),
    (10_000, "R$ 1 – 10 mil"),
    (100_000, "R$ 10 – 100 mil"),
    (1_000_000, "R$ 100 mil – 1 mi"),
)


def _partial_financial(value: Any) -> str:
    try:
        n = abs(float(value))
    except (ValueError, TypeError):
        return PII_PLACEHOLDERS[PiiCategory.FINANCIAL.value]  # não-numérico → mascara total
    for hi, label in _FINANCIAL_BANDS:
        if n < hi:
            return label
    return "> R$ 1 mi"


def partial_value(pii_category: str, value: Any) -> Any:
    """Revelação PARCIAL ciente da categoria. None → None (célula vazia).

    - email      → inicial + domínio (``j•••@gmail.com``);
    - financial  → faixa de magnitude (``R$ 1 – 10 mil``), nunca o valor exato;
    - name       → primeiro nome + iniciais (``João S.``);
    - demais (cpf/cnpj/phone/address/other/none) → últimos 4 chars (``••••3-44``).
    """
    if value is None:
        return None
    cat = normalize_pii_category(pii_category)
    if cat == PiiCategory.EMAIL.value:
        return _partial_email(value)
    if cat == PiiCategory.FINANCIAL.value:
        return _partial_financial(value)
    if cat == PiiCategory.NAME.value:
        return _partial_name(value)
    return _partial_tail(value)


def apply_display_treatment(rows: list[dict], catalog: Any) -> list[dict]:
    """Higiene de EXIBIÇÃO (render default Tier 1 / resultado NL) dirigida pelo
    TRATAMENTO DE SAÍDA por coluna do Catálogo — separado da classificação PII.

    Por coluna, o tratamento EFETIVO (override do curador, senão default derivado
    da categoria: não-PII=Exibir, PII=Mascarar) decide:
      - SHOW     → valor cru;
      - MASK     → célula inteira vira placeholder ([CATEGORIA] / [PROTEGIDO]);
      - PARTIAL  → revela PARTE, ciente da categoria (``partial_value``);
      - SUPPRESS → a coluna é REMOVIDA da saída.
    Coluna NÃO catalogada PASSA (Exibir) — no Tier 1 o autor escolheu o select
    (caminho confiado) e a maioria das tabelas não tem catálogo curado; mascarar
    tudo inutilizaria o render. NÃO muta ``rows``.

    Substitui o antigo ``mask_rows_pii_only`` (que acoplava exibição à
    classificação): financial+Exibir agora MOSTRA o valor sem mentir na categoria.
    """
    actions: dict[str, tuple[str, str]] = {}   # name -> (treatment, pii_category)
    for c in _columns(catalog):
        name = c.get("name")
        if not name:
            continue
        pii = normalize_pii_category(c.get("pii_category"))
        treat = effective_treatment(pii, c.get("output_treatment"))
        if treat == OutputTreatment.SHOW.value:
            continue  # nada a fazer — exibe
        actions[name] = (treat, pii)
    if not actions:
        return list(rows or [])
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        new_row: dict = {}
        for k, v in row.items():
            act = actions.get(k)
            if act is None:
                new_row[k] = v
            elif act[0] == OutputTreatment.SUPPRESS.value:
                continue          # remove a coluna da saída
            elif act[0] == OutputTreatment.PARTIAL.value:
                new_row[k] = partial_value(act[1], v)
            else:                 # MASK
                new_row[k] = _mask_token_for_category(act[1])
        out.append(new_row)
    return out


def display_columns(catalog: Any, columns: Iterable[str]) -> list[str]:
    """Filtra a lista de colunas removendo as SUPRIMIDAS pelo tratamento — p/ o
    cabeçalho do render default acompanhar as células (que apply_display_treatment
    removeu). Colunas fora do catálogo PASSAM (exibidas)."""
    suppressed = set()
    for c in _columns(catalog):
        name = c.get("name")
        if not name:
            continue
        treat = effective_treatment(normalize_pii_category(c.get("pii_category")),
                                    c.get("output_treatment"))
        if treat == OutputTreatment.SUPPRESS.value:
            suppressed.add(name)
    return [c for c in (columns or []) if c not in suppressed]


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
