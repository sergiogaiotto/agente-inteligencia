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


_JSON_FIELDS = ("schema_json",)


def db_row_to_table_dict(row: Any) -> dict:
    """Converte asyncpg.Record (ou dict) em dict serializável.

    JSONB do Postgres já vem como dict/list via asyncpg, mas defendemos
    contra o caso de schema vir como string (legacy ou migration manual).
    """
    out = dict(row) if not isinstance(row, dict) else dict(row)
    for key in _JSON_FIELDS:
        v = out.get(key)
        if isinstance(v, str):
            try:
                out[key] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[key] = []
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
