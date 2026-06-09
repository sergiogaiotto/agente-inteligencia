"""Queries especializadas e helpers da Onda Tabular.

Visibility é herdada da knowledge_source de origem (JOIN com
knowledge_sources). Repository genérico não serve porque precisamos
do JOIN e da filtragem por confidentiality (convenção #2).

URN gerado: urn:table:<ks_short>:<slug>:<version>
- ks_short: primeiros 8 chars do uuid da KS (curto, mas único o suficiente)
- slug: nome do arquivo normalizado (lowercase, alnum + hyphens)
- version: sequencial (1, 2, 3...) quando re-promove a mesma KS+slug
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any, Optional

from app.data_tables.types import normalize_pii_category

from app.core.database import _get_pool

logger = logging.getLogger(__name__)


# ─── URN / slug ───────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Normaliza para slug ASCII lowercase com hífens. Aceita acentos PT-BR.

    Ex: "Vendas Q4 2025.csv" → "vendas-q4-2025-csv"
    """
    if not name:
        return "tabela"
    nfd = unicodedata.normalize("NFKD", name)
    ascii_only = nfd.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_only).strip("-")
    return slug or "tabela"


def build_urn(ks_id: str, slug: str, version: str | int) -> str:
    """urn:table:<ks_short>:<slug>:<version>. Estável para referência em SKILL.md."""
    ks_short = (ks_id or "")[:8]
    return f"urn:table:{ks_short}:{slug}:{version}"


# ─── Visibility (herdada da KS) ───────────────────────────────────


def is_root(user: dict) -> bool:
    return (user.get("role") or "").lower() == "root"


def _user_domains(user: dict) -> list[str]:
    raw = user.get("domains")
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def can_user_see(user: dict, table_with_ks: dict) -> bool:
    """Decide se o user pode VER/CONSULTAR uma tabela.

    `table_with_ks` é o dict retornado pelas queries com JOIN que
    inclui `ks_confidentiality_label` e `ks_authorized`.

    Regras (em ordem):
    1. Root vê tudo.
    2. KS não autorizada (authorized=0) → ninguém vê (a não ser root).
    3. confidentiality_label == 'public' → todos veem.
    4. confidentiality_label == 'internal' → qualquer user logado (default).
    5. confidentiality_label == 'restricted' → só Root.
    6. confidentiality_label == 'confidential' → só Root.

    NOTA: este é o modelo conservador. Se o futuro pedir "department"
    granular, adicionar `ks_visibility_scope` igual ao catalog.
    """
    if is_root(user):
        return True
    if not table_with_ks.get("ks_authorized"):
        return False
    label = (table_with_ks.get("ks_confidentiality_label") or "internal").lower()
    if label == "public":
        return True
    if label == "internal":
        return True  # qualquer user logado
    # restricted / confidential / outros → bloqueia para não-root
    return False


# ─── Conversão de row → dict serializável ─────────────────────────


# Campos JSONB decodificados defensivamente (string legacy/mock/None → estrutura).
# Lista e objeto têm fallback de erro DIFERENTE — não confundir [] com {}.
_JSON_LIST_FIELDS = ("schema_json",)
_JSON_OBJECT_FIELDS = ("catalog_json",)


def _decode_json_field(out: dict, key: str, empty) -> None:
    """Decode defensivo de 1 campo JSONB; string inválida ou None → `empty`."""
    v = out.get(key)
    if isinstance(v, str):
        try:
            out[key] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            out[key] = empty
    elif v is None:
        out[key] = empty


def reconcile_catalog(schema: Any, catalog: Any, table_description: str = "") -> dict:
    """Reconcilia o catálogo curado (catalog_json) com o schema VIVO (schema_json).

    "left join" por NOME de coluna sobre o schema ATUAL:
      - coluna do schema SEM entry no catálogo → metadata neutra (não catalogada);
      - entry do catálogo SEM coluna no schema (removida num re-promote) → IGNORADO
        na saída (mas continua preservado no catalog_json do DB, p/ voltar se a
        coluna reaparecer).
    Coração determinístico do anti-alucinação: o catálogo NUNCA expõe coluna que
    não existe no schema vivo, e toda pii_category passa por normalize (enum fechado).
    """
    catalog = catalog if isinstance(catalog, dict) else {}
    cat_cols = catalog.get("columns")
    cat_cols = cat_cols if isinstance(cat_cols, dict) else {}
    cat_table = catalog.get("table")
    cat_table = cat_table if isinstance(cat_table, dict) else {}
    columns = []
    for col in (schema if isinstance(schema, list) else []):
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        entry = cat_cols.get(name)
        entry = entry if isinstance(entry, dict) else {}
        columns.append({
            "name": name,
            "type": col.get("type"),
            "nullable": col.get("nullable"),
            "description": str(entry.get("description") or ""),
            "pii_category": normalize_pii_category(entry.get("pii_category")),
            "source": entry.get("source"),  # 'ai' | 'human' | None (não catalogado)
        })
    return {
        "table": {
            "description": table_description or "",
            "source": cat_table.get("description_source"),
            "curated_by": cat_table.get("curated_by"),
            "curated_at": cat_table.get("curated_at"),
        },
        "columns": columns,
    }


def db_row_to_table_dict(row: Any) -> dict:
    """Converte asyncpg.Record (ou dict) em dict serializável.

    JSONB do Postgres já vem como dict/list via asyncpg; defendemos contra o caso
    de vir string (legacy/migration manual/mock). Além disso expõe `catalog`: o
    Catálogo de Dados RECONCILIADO com o schema vivo (ver reconcile_catalog).
    """
    out = dict(row) if not isinstance(row, dict) else dict(row)
    for key in _JSON_LIST_FIELDS:
        _decode_json_field(out, key, [])
    for key in _JSON_OBJECT_FIELDS:
        _decode_json_field(out, key, {})
    # Reconciliação só quando o row traz schema_json (toda data_table traz).
    # catalog_json ausente (DB não migrado / row parcial) → catálogo neutro.
    if "schema_json" in out:
        out["catalog"] = reconcile_catalog(
            out.get("schema_json"),
            out.get("catalog_json"),
            out.get("description") or "",
        )
    return out


# ─── Queries com JOIN para visibility-aware ──────────────────────


_BASE_SELECT_WITH_KS = """
    SELECT
        dt.*,
        ks.confidentiality_label AS ks_confidentiality_label,
        ks.authorized AS ks_authorized,
        ks.name AS ks_name
    FROM data_tables dt
    JOIN knowledge_sources ks ON ks.id = dt.knowledge_source_id
"""


async def find_by_id_with_ks(table_id: str) -> Optional[dict]:
    """Busca tabela + metadata da KS. Retorna None se não existir."""
    pool = _get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            _BASE_SELECT_WITH_KS + " WHERE dt.id = $1",
            table_id,
        )
        return db_row_to_table_dict(row) if row else None


async def find_by_urn_with_ks(urn: str) -> Optional[dict]:
    """Lookup por URN canônico (usado pelo declarative_engine ao resolver
    `table_ref` em ## Data Tables)."""
    pool = _get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            _BASE_SELECT_WITH_KS + " WHERE dt.urn = $1",
            urn,
        )
        return db_row_to_table_dict(row) if row else None


async def list_for_user(user: dict, ks_id: Optional[str] = None) -> list[dict]:
    """Lista tabelas visíveis ao user. Filtragem em SQL (não Python)
    para paginação correta no futuro (convenção #3).
    """
    pool = _get_pool()
    async with pool.acquire() as con:
        if is_root(user):
            # Root vê tudo (status != deleted)
            if ks_id:
                rows = await con.fetch(
                    _BASE_SELECT_WITH_KS
                    + " WHERE dt.status != 'deleted' AND dt.knowledge_source_id = $1"
                    + " ORDER BY dt.created_at DESC",
                    ks_id,
                )
            else:
                rows = await con.fetch(
                    _BASE_SELECT_WITH_KS
                    + " WHERE dt.status != 'deleted'"
                    + " ORDER BY dt.created_at DESC",
                )
        else:
            # Não-root: só KS autorizada + label public/internal
            base_filter = (
                " WHERE dt.status != 'deleted'"
                " AND ks.authorized = 1"
                " AND lower(coalesce(ks.confidentiality_label, 'internal'))"
                " IN ('public', 'internal')"
            )
            if ks_id:
                rows = await con.fetch(
                    _BASE_SELECT_WITH_KS + base_filter + " AND dt.knowledge_source_id = $1"
                    + " ORDER BY dt.created_at DESC",
                    ks_id,
                )
            else:
                rows = await con.fetch(
                    _BASE_SELECT_WITH_KS + base_filter + " ORDER BY dt.created_at DESC",
                )
        return [db_row_to_table_dict(r) for r in rows]


async def next_version_for_slug(ks_id: str, slug: str) -> int:
    """Retorna próxima versão para evitar URN duplicado quando re-promove
    o mesmo CSV/slug em uma KS. Começa em 1.
    """
    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT version FROM data_tables WHERE knowledge_source_id = $1 AND urn LIKE $2",
            ks_id,
            f"urn:table:%:{slug}:%",
        )
        if not rows:
            return 1
        max_v = 0
        for r in rows:
            try:
                v = int(r["version"])
                if v > max_v:
                    max_v = v
            except (ValueError, TypeError):
                continue
        return max_v + 1
