"""Queries especializadas do catálogo.

Regras de visibilidade implementadas em SQL (escala melhor que filtro
em Python; paginação correta). Conversões de TEXT-JSON ficam aqui também
para serialização correta na API.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.core.database import _get_pool

logger = logging.getLogger(__name__)


# ─── Regras de visibilidade (puras, testáveis) ────────────────────


def is_root(user: dict) -> bool:
    """True se o user tem role root (case-insensitive)."""
    return (user.get("role") or "").lower() == "root"


def _user_domains(user: dict) -> list[str]:
    """Decodifica o campo `domains` (TEXT JSON list) de forma tolerante."""
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


def can_user_see(user: dict, entry: dict) -> bool:
    """Decide se o user tem permissão para ver uma entry específica.

    Regras (em ordem):
    1. Root vê tudo.
    2. Owner vê suas próprias entries em qualquer status/visibilidade.
    3. Demais só veem entries com status terminal-público
       (published ou deprecated) E visibility compatível.
    """
    if is_root(user):
        return True
    if entry.get("owner_user_id") == user.get("id"):
        return True
    if entry.get("status") not in ("published", "deprecated"):
        return False
    visibility = entry.get("visibility")
    if visibility == "company":
        return True
    if visibility == "department":
        scope = entry.get("visibility_scope")
        return bool(scope) and scope in _user_domains(user)
    # private (e qualquer outro valor) → bloqueia
    return False


# ─── Conversão de row Postgres → dict serializável ────────────────


_JSON_FIELDS = ("tags", "adapter_config")


def db_row_to_entry_dict(row: Any) -> dict:
    """Converte um asyncpg.Record (ou dict) em dict serializável.

    Parseia campos JSON armazenados como TEXT. Mantém datetimes
    intactos (FastAPI serializa para ISO 8601 na resposta).
    """
    out = dict(row) if not isinstance(row, dict) else dict(row)
    for key in _JSON_FIELDS:
        v = out.get(key)
        if isinstance(v, str):
            try:
                out[key] = json.loads(v) if v else ([] if key == "tags" else {})
            except json.JSONDecodeError:
                out[key] = [] if key == "tags" else {}
    return out


# ─── List com visibility-awareness (SQL nativo) ───────────────────


async def list_visible_entries(
    user: dict,
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    domain: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Lista entries visíveis para o user, com filtros opcionais.

    Pagina corretamente (LIMIT/OFFSET aplicados após visibilidade).
    Retorna (rows, total) — total considera o filtro de visibilidade.
    """
    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = []

    if not is_root(user):
        params.append(user.get("id"))
        uid_ph = f"${len(params)}"
        user_doms = _user_domains(user)
        if user_doms:
            params.append(user_doms)
            doms_ph = f"${len(params)}"
            where_parts.append(
                f"(owner_user_id = {uid_ph} OR "
                f"(status IN ('published','deprecated') AND "
                f"(visibility = 'company' OR "
                f"(visibility = 'department' AND visibility_scope = ANY({doms_ph})))))"
            )
        else:
            # Sem domains do user, dept-visibility nunca casa
            where_parts.append(
                f"(owner_user_id = {uid_ph} OR "
                f"(status IN ('published','deprecated') AND visibility = 'company'))"
            )

    if kind:
        params.append(kind)
        where_parts.append(f"kind = ${len(params)}")
    if status:
        params.append(status)
        where_parts.append(f"status = ${len(params)}")
    if domain:
        params.append(domain)
        where_parts.append(f"domain = ${len(params)}")
    if owner_user_id:
        params.append(owner_user_id)
        where_parts.append(f"owner_user_id = ${len(params)}")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    # Count usa só os params do WHERE; list adiciona limit/offset.
    count_sql = f"SELECT COUNT(*) FROM catalog_entries {where_sql}"

    params_with_pagination = params + [limit, offset]
    limit_ph = f"${len(params_with_pagination)-1}"
    offset_ph = f"${len(params_with_pagination)}"
    list_sql = (
        f"SELECT * FROM catalog_entries {where_sql} "
        f"ORDER BY created_at DESC "
        f"LIMIT {limit_ph} OFFSET {offset_ph}"
    )

    async with pool.acquire() as con:
        rows = await con.fetch(list_sql, *params_with_pagination)
        total = await con.fetchval(count_sql, *params) or 0

    return [db_row_to_entry_dict(r) for r in rows], int(total)


# ─── Capability Disclosure (PK = entry_id, não 'id') ─────────────


# Colunas escrevíveis via API. Exclui PK (entry_id), timestamps
# automáticos (created_at/updated_at) e campos preenchidos por
# verificação por execução (Onda 2: verified_at, declared_vs_detected).
_DISCLOSURE_WRITABLE_COLS = (
    "reads_user_kb",
    "writes_user_kb",
    "calls_external_apis",
    "external_apis_list",
    "stores_input",
    "storage_retention_days",
    "accesses_internet",
    "processes_pii",
    "processes_financial",
    "processes_health",
    "trains_on_input",
    "output_is_deterministic",
    "data_residency",
    "additional_notes",
    "verification_method",
)


async def get_disclosure(entry_id: str) -> Optional[dict]:
    """Busca capability disclosure por entry_id. None se ausente."""
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow(
            "SELECT * FROM catalog_capability_disclosure WHERE entry_id=$1",
            entry_id,
        )
    if not r:
        return None
    out = dict(r)
    # external_apis_list é TEXT JSON — parseia para list[str]
    raw_apis = out.get("external_apis_list")
    if isinstance(raw_apis, str):
        try:
            out["external_apis_list"] = json.loads(raw_apis) if raw_apis else []
        except json.JSONDecodeError:
            out["external_apis_list"] = []
    return out


async def upsert_disclosure(entry_id: str, payload: dict) -> dict:
    """Upsert disclosure. payload = subset de _DISCLOSURE_WRITABLE_COLS.

    Usa ON CONFLICT (entry_id) DO UPDATE — mesmo padrão de SettingsStore.
    Campos ausentes em UPDATE mantêm valor anterior (não zera).
    """
    # Filtra para campos conhecidos + serializa list/dict como JSON
    filtered: dict[str, Any] = {}
    for k in _DISCLOSURE_WRITABLE_COLS:
        if k in payload:
            v = payload[k]
            if k == "external_apis_list" and isinstance(v, list):
                v = json.dumps(v)
            filtered[k] = v

    pool = _get_pool()
    async with pool.acquire() as con:
        if filtered:
            cols = ["entry_id"] + list(filtered.keys())
            values = [entry_id] + list(filtered.values())
            placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
            updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in filtered.keys())
            sql = (
                f"INSERT INTO catalog_capability_disclosure ({', '.join(cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (entry_id) DO UPDATE SET {updates}, updated_at = now() "
                f"RETURNING *"
            )
            r = await con.fetchrow(sql, *values)
        else:
            # Payload vazio: cria row stub se ausente, senão no-op
            sql = (
                "INSERT INTO catalog_capability_disclosure (entry_id) VALUES ($1) "
                "ON CONFLICT (entry_id) DO UPDATE SET updated_at = now() "
                "RETURNING *"
            )
            r = await con.fetchrow(sql, entry_id)

    out = dict(r) if r else {"entry_id": entry_id}
    raw_apis = out.get("external_apis_list")
    if isinstance(raw_apis, str):
        try:
            out["external_apis_list"] = json.loads(raw_apis) if raw_apis else []
        except json.JSONDecodeError:
            out["external_apis_list"] = []
    return out


async def delete_disclosure(entry_id: str) -> bool:
    """Remove disclosure. True se removeu, False se não existia."""
    pool = _get_pool()
    async with pool.acquire() as con:
        res = await con.execute(
            "DELETE FROM catalog_capability_disclosure WHERE entry_id=$1",
            entry_id,
        )
    try:
        n = int(res.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        n = 0
    return n > 0
