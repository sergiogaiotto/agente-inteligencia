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


# ─── External Platforms metadata (Onda 2, PK = entry_id) ─────────


_EXTERNAL_META_WRITABLE_COLS = (
    "vendor",
    "vendor_url",
    "contract_status",
    "contract_renewal_date",
    "monthly_cost_usd",
    "vendor_contact",
    "approved_use_cases",
    "restrictions",
    "approved_by_user_id",
    "approved_at",
)


async def get_external_metadata(entry_id: str) -> Optional[dict]:
    """Busca metadata de plataforma externa. None se ausente."""
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow(
            "SELECT * FROM catalog_external_metadata WHERE entry_id=$1",
            entry_id,
        )
    return dict(r) if r else None


async def upsert_external_metadata(entry_id: str, payload: dict) -> dict:
    """Upsert metadata externo. vendor é o único campo obrigatório no schema."""
    filtered: dict[str, Any] = {}
    for k in _EXTERNAL_META_WRITABLE_COLS:
        if k in payload:
            filtered[k] = payload[k]

    # vendor é NOT NULL — exige no insert. Em update opcional (mantém valor).
    pool = _get_pool()
    async with pool.acquire() as con:
        existing = await con.fetchrow(
            "SELECT entry_id FROM catalog_external_metadata WHERE entry_id=$1",
            entry_id,
        )
        if existing is None:
            # Primeira escrita exige vendor
            if "vendor" not in filtered or not filtered["vendor"]:
                raise ValueError("vendor é obrigatório na criação de external_metadata")
            cols = ["entry_id"] + list(filtered.keys())
            values = [entry_id] + list(filtered.values())
            placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
            sql = (
                f"INSERT INTO catalog_external_metadata ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING *"
            )
            r = await con.fetchrow(sql, *values)
        elif filtered:
            sets = ", ".join(f"{k} = ${i+1}" for i, k in enumerate(filtered.keys()))
            values = list(filtered.values()) + [entry_id]
            sql = (
                f"UPDATE catalog_external_metadata SET {sets}, updated_at = now() "
                f"WHERE entry_id = ${len(values)} RETURNING *"
            )
            r = await con.fetchrow(sql, *values)
        else:
            # No-op: payload vazio com row existente
            r = await con.fetchrow(
                "SELECT * FROM catalog_external_metadata WHERE entry_id=$1",
                entry_id,
            )

    return dict(r) if r else {"entry_id": entry_id}


async def delete_external_metadata(entry_id: str) -> bool:
    """Remove metadata externo. Cascade já remove ao deletar entry — útil
    se publisher quiser limpar antes de mudar kind."""
    pool = _get_pool()
    async with pool.acquire() as con:
        res = await con.execute(
            "DELETE FROM catalog_external_metadata WHERE entry_id=$1",
            entry_id,
        )
    try:
        n = int(res.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        n = 0
    return n > 0


# ─── Inventário Regulatório (Onda 2) ─────────────────────────────


# Whitelist de flags filtráveis em disclosure.
# Usado pelo endpoint e na construção dinâmica de SQL — protege contra
# column injection caso o caller passe nome arbitrário.
_INVENTORY_FLAGS = (
    "processes_pii",
    "processes_financial",
    "processes_health",
    "calls_external_apis",
    "accesses_internet",
    "stores_input",
    "writes_user_kb",
    "reads_user_kb",
    "trains_on_input",
)


async def list_inventory(
    *,
    flags: Optional[dict[str, bool]] = None,
    residency: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Inventário regulatório: cross-entries com filtros por capability disclosure.

    Faz LEFT JOIN com disclosure (entries sem disclosure aparecem com flags
    NULL) + LEFT JOIN com external_metadata (preenche vendor/cost quando
    aplicável). Filtros por flag são opcionais.

    Args:
        flags: dict {flag_name: bool} — só flags em _INVENTORY_FLAGS são aceitas.
        residency: filtra por data_residency exato.
        kind: filtra entry.kind.
        status: filtra entry.status.
    """
    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = []

    if kind:
        params.append(kind)
        where_parts.append(f"e.kind = ${len(params)}")
    if status:
        params.append(status)
        where_parts.append(f"e.status = ${len(params)}")

    if flags:
        for col, val in flags.items():
            if col not in _INVENTORY_FLAGS or val is None:
                continue
            # Filtra disclosure exigindo flag = val. NULL não casa.
            params.append(val)
            where_parts.append(f"d.{col} = ${len(params)}")

    if residency:
        params.append(residency)
        where_parts.append(f"d.data_residency = ${len(params)}")

    base_join = (
        "FROM catalog_entries e "
        "LEFT JOIN catalog_capability_disclosure d ON d.entry_id = e.id "
        "LEFT JOIN catalog_external_metadata x ON x.entry_id = e.id"
    )
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    select_cols = (
        "e.id, e.urn, e.name, e.kind, e.status, e.version, e.domain, "
        "e.owner_user_id, e.steward_team, e.visibility, e.visibility_scope, "
        "e.created_at, e.published_at, "
        "d.processes_pii, d.processes_financial, d.processes_health, "
        "d.calls_external_apis, d.accesses_internet, d.stores_input, "
        "d.writes_user_kb, d.reads_user_kb, d.trains_on_input, "
        "d.data_residency, d.external_apis_list, d.storage_retention_days, "
        "x.vendor, x.monthly_cost_usd, x.contract_status, x.contract_renewal_date"
    )

    count_sql = f"SELECT COUNT(*) {base_join} {where_sql}"

    params_paginated = params + [limit, offset]
    limit_ph = f"${len(params_paginated)-1}"
    offset_ph = f"${len(params_paginated)}"
    list_sql = (
        f"SELECT {select_cols} {base_join} {where_sql} "
        f"ORDER BY e.created_at DESC LIMIT {limit_ph} OFFSET {offset_ph}"
    )

    async with pool.acquire() as con:
        rows = await con.fetch(list_sql, *params_paginated)
        total = await con.fetchval(count_sql, *params) or 0

    # Parseia external_apis_list (TEXT JSON) — outras flags são bool/null nativos
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("external_apis_list")
        if isinstance(raw, str):
            try:
                d["external_apis_list"] = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                d["external_apis_list"] = []
        out.append(d)

    return out, int(total)


# ─── Stewardship (Onda 2) ────────────────────────────────────────


# Threshold de "stale": entry published sem invocações há N dias.
# Calibração conservadora — 30 dias é onde steward já precisa olhar.
STALE_THRESHOLD_DAYS = 30
# Threshold de "low_reliability": trust_reliability < este valor.
LOW_RELIABILITY_THRESHOLD = 0.5


async def list_stewardship(
    *,
    steward_team: Optional[str] = None,
    restrict_to_teams: Optional[list[str]] = None,
    limit: int = 500,
) -> tuple[list[dict], dict]:
    """Lista entries com info de stewardship + flags de saúde.

    Joina users para detectar owner inativo (is_orphan). Calcula flags
    derivadas em SQL (is_stale, has_low_reliability).

    Args:
        steward_team: filtro exato por uma área (UI filter).
        restrict_to_teams: lista de áreas permitidas (auth filter para
            non-root). Quando lista vazia, retorna 0 entries. None = sem
            restrição (Root).

    Returns:
        (entries enriched, aggregates_by_team)
    """
    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = []

    if steward_team:
        params.append(steward_team)
        where_parts.append(f"e.steward_team = ${len(params)}")

    if restrict_to_teams is not None:
        if not restrict_to_teams:
            # Lista vazia: user é non-root sem nenhum domain — vê nada.
            return [], {}
        params.append(restrict_to_teams)
        where_parts.append(f"e.steward_team = ANY(${len(params)})")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)
    limit_ph = f"${len(params)}"

    sql = f"""
        SELECT
            e.id, e.name, e.kind, e.status, e.version, e.urn,
            e.owner_user_id, e.steward_team, e.domain, e.visibility,
            e.created_at, e.published_at, e.deprecated_at,
            e.trust_reliability, e.trust_invocation_count, e.trust_last_invoked_at,
            u.status AS owner_status,
            u.username AS owner_username,
            u.display_name AS owner_display_name,
            (u.id IS NULL OR u.status != 'active') AS is_orphan,
            (
                e.status = 'published' AND (
                    e.trust_last_invoked_at IS NULL
                    OR e.trust_last_invoked_at < (now() - interval '{STALE_THRESHOLD_DAYS} days')
                )
            ) AS is_stale,
            (
                e.trust_reliability IS NOT NULL
                AND e.trust_reliability < {LOW_RELIABILITY_THRESHOLD}
            ) AS has_low_reliability
        FROM catalog_entries e
        LEFT JOIN users u ON u.id = e.owner_user_id
        {where_sql}
        ORDER BY e.steward_team NULLS LAST, e.name
        LIMIT {limit_ph}
    """

    async with pool.acquire() as con:
        rows = await con.fetch(sql, *params)

    entries = [dict(r) for r in rows]

    by_team: dict[str, dict] = {}
    for e in entries:
        team = e.get("steward_team") or "(sem steward)"
        t = by_team.setdefault(team, {
            "total": 0, "orphan": 0, "stale": 0, "low_reliability": 0,
            "published": 0, "deprecated": 0,
        })
        t["total"] += 1
        if e.get("is_orphan"):
            t["orphan"] += 1
        if e.get("is_stale"):
            t["stale"] += 1
        if e.get("has_low_reliability"):
            t["low_reliability"] += 1
        if e.get("status") == "published":
            t["published"] += 1
        elif e.get("status") == "deprecated":
            t["deprecated"] += 1

    return entries, by_team


# ─── Cost & Consumption (Onda 3) ─────────────────────────────────


# Group-by allowlist — protege a coluna gerada de SQL injection.
_COST_GROUP_COLS = {
    "entry": "entry_id",
    "consumer": "consumer_user_id",
    "department": "consumer_department",
    "day": "DATE(invoked_at)",
}


async def record_invocation_cost(
    entry_id: str,
    *,
    consumer_user_id: str,
    consumer_department: Optional[str] = None,
    interaction_id: Optional[str] = None,
    cost_usd: float = 0.0,
    tokens_used: int = 0,
    latency_ms: float = 0.0,
) -> dict:
    """Insere uma row em catalog_costs. Insert-only; nunca update."""
    import uuid
    cost_id = str(uuid.uuid4())
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO catalog_costs
              (id, entry_id, consumer_user_id, consumer_department,
               interaction_id, cost_usd, tokens_used, latency_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            cost_id, entry_id, consumer_user_id, consumer_department,
            interaction_id, cost_usd, tokens_used, latency_ms,
        )
        # Bump métricas agregadas na entry — para o catálogo refletir uso
        await con.execute(
            """
            UPDATE catalog_entries SET
              trust_invocation_count = COALESCE(trust_invocation_count, 0) + 1,
              trust_last_invoked_at = now()
            WHERE id = $1
            """,
            entry_id,
        )
    return {
        "id": cost_id,
        "entry_id": entry_id,
        "consumer_user_id": consumer_user_id,
        "cost_usd": cost_usd,
        "tokens_used": tokens_used,
        "latency_ms": latency_ms,
    }


async def aggregate_costs(
    *,
    group_by: str = "entry",
    since: Optional[str] = None,  # ISO date YYYY-MM-DD
    until: Optional[str] = None,
    entry_id: Optional[str] = None,
    consumer_user_id: Optional[str] = None,
    consumer_department: Optional[str] = None,
    limit: int = 200,
) -> tuple[list[dict], dict]:
    """Agrega catalog_costs por grupo (entry|consumer|department|day).

    Returns:
        (rows, totals) — rows tem {group_key, invocations, total_cost_usd,
                                    total_tokens, avg_latency_ms};
                        totals tem agregados globais para os mesmos filtros.
    """
    if group_by not in _COST_GROUP_COLS:
        raise ValueError(f"group_by inválido. Opções: {list(_COST_GROUP_COLS)}")
    group_expr = _COST_GROUP_COLS[group_by]

    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = []

    if since:
        params.append(since)
        where_parts.append(f"invoked_at >= ${len(params)}::date")
    if until:
        params.append(until)
        # +1 dia para incluir o dia 'until' inteiro
        where_parts.append(f"invoked_at < (${len(params)}::date + interval '1 day')")
    if entry_id:
        params.append(entry_id)
        where_parts.append(f"entry_id = ${len(params)}")
    if consumer_user_id:
        params.append(consumer_user_id)
        where_parts.append(f"consumer_user_id = ${len(params)}")
    if consumer_department:
        params.append(consumer_department)
        where_parts.append(f"consumer_department = ${len(params)}")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    params.append(limit)
    limit_ph = f"${len(params)}"

    rows_sql = f"""
        SELECT
            {group_expr}::text AS group_key,
            COUNT(*) AS invocations,
            COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
            COALESCE(SUM(tokens_used), 0) AS total_tokens,
            COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
        FROM catalog_costs
        {where_sql}
        GROUP BY {group_expr}
        ORDER BY total_cost_usd DESC
        LIMIT {limit_ph}
    """

    totals_sql = f"""
        SELECT
            COUNT(*) AS invocations,
            COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
            COALESCE(SUM(tokens_used), 0) AS total_tokens,
            COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
            COUNT(DISTINCT entry_id) AS distinct_entries,
            COUNT(DISTINCT consumer_user_id) AS distinct_consumers
        FROM catalog_costs
        {where_sql}
    """

    async with pool.acquire() as con:
        rows = await con.fetch(rows_sql, *params)
        totals = await con.fetchrow(totals_sql, *params[:-1])  # exclui o limit

    return [dict(r) for r in rows], (dict(totals) if totals else {})


# ─── Recipes (Onda 3) ────────────────────────────────────────────


async def get_recipe(entry_id: str) -> Optional[dict]:
    """Lê o manifest do recipe + enriquece cada step com target_name/kind/status
    via lookup em catalog_entries. None se ainda não declarado."""
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow(
            "SELECT steps, created_at, updated_at FROM catalog_recipes WHERE entry_id=$1",
            entry_id,
        )
        if not r:
            return None
        out = dict(r)
        raw_steps = out.get("steps") or []
        # asyncpg pode retornar JSONB como str OU dict — normaliza
        if isinstance(raw_steps, str):
            try:
                raw_steps = json.loads(raw_steps)
            except json.JSONDecodeError:
                raw_steps = []
        # Enriquece cada step com info do target (uma query por target — OK
        # para Onda 3; otimiza com JOIN se virar gargalo)
        target_ids = [s.get("target_entry_id") for s in raw_steps if s.get("target_entry_id")]
        enriched_steps = []
        if target_ids:
            targets_rows = await con.fetch(
                "SELECT id, name, kind, status FROM catalog_entries WHERE id = ANY($1)",
                target_ids,
            )
            tmap = {r["id"]: dict(r) for r in targets_rows}
            for s in raw_steps:
                tid = s.get("target_entry_id")
                t = tmap.get(tid)
                enriched_steps.append({
                    "order": s.get("order"),
                    "target_entry_id": tid,
                    "notes": s.get("notes"),
                    "target_name": t["name"] if t else None,
                    "target_kind": t["kind"] if t else None,
                    "target_status": t["status"] if t else None,
                    "target_exists": t is not None,
                })
        out["steps"] = enriched_steps
    return out


async def upsert_recipe(entry_id: str, steps: list[dict]) -> dict:
    """Upsert do manifest. Valida que cada target_entry_id existe e não
    referencia o próprio recipe (anti-ciclo trivial). Persiste como JSONB."""
    # Anti-ciclo: target_entry_id não pode ser o próprio recipe
    for s in steps:
        if s.get("target_entry_id") == entry_id:
            raise ValueError("recipe não pode invocar a si mesmo")

    # Valida existência de todos os targets em uma única query
    target_ids = [s.get("target_entry_id") for s in steps]
    pool = _get_pool()
    async with pool.acquire() as con:
        existing = await con.fetch(
            "SELECT id FROM catalog_entries WHERE id = ANY($1)", target_ids,
        )
        existing_ids = {r["id"] for r in existing}
        missing = [tid for tid in target_ids if tid not in existing_ids]
        if missing:
            raise ValueError(f"target_entry_id(s) inexistente(s): {missing}")

        # Persiste — INSERT ON CONFLICT como nos outros 1:1 helpers
        steps_json = json.dumps(steps)
        r = await con.fetchrow(
            """
            INSERT INTO catalog_recipes (entry_id, steps)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (entry_id) DO UPDATE SET
              steps = EXCLUDED.steps,
              updated_at = now()
            RETURNING entry_id, steps, created_at, updated_at
            """,
            entry_id, steps_json,
        )
    out = dict(r)
    raw = out.get("steps")
    if isinstance(raw, str):
        try:
            out["steps"] = json.loads(raw)
        except json.JSONDecodeError:
            out["steps"] = []
    return out


async def delete_recipe(entry_id: str) -> bool:
    """Remove o manifest do recipe. Cascade já remove ao deletar a entry."""
    pool = _get_pool()
    async with pool.acquire() as con:
        res = await con.execute(
            "DELETE FROM catalog_recipes WHERE entry_id=$1",
            entry_id,
        )
    try:
        n = int(res.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        n = 0
    return n > 0


async def list_costs_raw(
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    entry_id: Optional[str] = None,
    consumer_user_id: Optional[str] = None,
    consumer_department: Optional[str] = None,
    limit: int = 5000,
) -> list[dict]:
    """Lista rows cruas de catalog_costs para export CSV. Sem agrupamento."""
    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = []

    if since:
        params.append(since)
        where_parts.append(f"invoked_at >= ${len(params)}::date")
    if until:
        params.append(until)
        where_parts.append(f"invoked_at < (${len(params)}::date + interval '1 day')")
    if entry_id:
        params.append(entry_id)
        where_parts.append(f"entry_id = ${len(params)}")
    if consumer_user_id:
        params.append(consumer_user_id)
        where_parts.append(f"consumer_user_id = ${len(params)}")
    if consumer_department:
        params.append(consumer_department)
        where_parts.append(f"consumer_department = ${len(params)}")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)
    limit_ph = f"${len(params)}"

    sql = f"""
        SELECT id, entry_id, consumer_user_id, consumer_department,
               interaction_id, cost_usd, tokens_used, latency_ms, invoked_at
        FROM catalog_costs
        {where_sql}
        ORDER BY invoked_at DESC
        LIMIT {limit_ph}
    """
    async with pool.acquire() as con:
        rows = await con.fetch(sql, *params)
    return [dict(r) for r in rows]


# ─── Recipe Executions (Onda 4) ──────────────────────────────────


def can_user_see_execution(user: dict, execution: dict, recipe_entry: Optional[dict]) -> bool:
    """Pode ver execution: root | consumer (quem rodou) | owner do recipe."""
    if is_root(user):
        return True
    if execution.get("consumer_user_id") == user.get("id"):
        return True
    if recipe_entry and recipe_entry.get("owner_user_id") == user.get("id"):
        return True
    return False


async def create_execution(
    *,
    recipe_entry_id: str,
    consumer_user_id: str,
    input_text: str,
    is_sandbox: bool = False,
) -> dict:
    """Cria row em status='running' e retorna o dict completo.

    is_sandbox=True marca run de teste — não persiste em catalog_costs e
    fica filtravel no histórico. Veja [[onda4-sandbox]] em docs/catalog.
    """
    import uuid
    exec_id = str(uuid.uuid4())
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow(
            """
            INSERT INTO catalog_recipe_executions
              (id, recipe_entry_id, consumer_user_id, input, is_sandbox)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, recipe_entry_id, consumer_user_id, input,
                      steps_results, status, total_cost_usd, total_latency_ms,
                      error_message, started_at, finished_at, is_sandbox
            """,
            exec_id, recipe_entry_id, consumer_user_id, input_text, is_sandbox,
        )
    return _normalize_execution_row(r)


async def get_execution(execution_id: str, *, enrich: bool = True) -> Optional[dict]:
    """Lê execution. Quando enrich=True, faz lookup do nome do recipe e
    enriquece cada step_result com target_name (best-effort)."""
    pool = _get_pool()
    async with pool.acquire() as con:
        r = await con.fetchrow(
            """
            SELECT id, recipe_entry_id, consumer_user_id, input,
                   steps_results, status, total_cost_usd, total_latency_ms,
                   error_message, started_at, finished_at,
                   COALESCE(is_sandbox, FALSE) AS is_sandbox
            FROM catalog_recipe_executions
            WHERE id=$1
            """,
            execution_id,
        )
        if not r:
            return None
        d = _normalize_execution_row(r)
        if enrich:
            rec = await con.fetchrow(
                "SELECT name FROM catalog_entries WHERE id=$1",
                d["recipe_entry_id"],
            )
            d["recipe_name"] = rec["name"] if rec else None
            # Cada step pode já trazer target_name persistido pelo executor;
            # se não tiver, faz lookup. Evita N queries quando possível.
            missing = [
                s.get("target_entry_id")
                for s in d["steps_results"]
                if s.get("target_entry_id") and not s.get("target_name")
            ]
            if missing:
                rows = await con.fetch(
                    "SELECT id, name FROM catalog_entries WHERE id = ANY($1::text[])",
                    list(set(missing)),
                )
                name_by_id = {row["id"]: row["name"] for row in rows}
                for s in d["steps_results"]:
                    if s.get("target_entry_id") in name_by_id and not s.get("target_name"):
                        s["target_name"] = name_by_id[s["target_entry_id"]]
        return d


async def list_executions_for_entry(
    recipe_entry_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Histórico paginado de execuções de um recipe, mais recentes primeiro."""
    pool = _get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT id, recipe_entry_id, consumer_user_id, input,
                   steps_results, status, total_cost_usd, total_latency_ms,
                   error_message, started_at, finished_at,
                   COALESCE(is_sandbox, FALSE) AS is_sandbox
            FROM catalog_recipe_executions
            WHERE recipe_entry_id=$1
            ORDER BY started_at DESC
            LIMIT $2 OFFSET $3
            """,
            recipe_entry_id, limit, offset,
        )
    return [_normalize_execution_row(r) for r in rows]


async def append_step_result(execution_id: str, step_result: dict) -> None:
    """Adiciona um step_result ao array JSONB. Usa concatenação atômica."""
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE catalog_recipe_executions
            SET steps_results = steps_results || $2::jsonb
            WHERE id=$1
            """,
            execution_id, json.dumps([step_result]),
        )


async def finalize_execution(
    execution_id: str,
    *,
    status: str,
    total_cost_usd: float,
    total_latency_ms: int,
    error_message: Optional[str] = None,
) -> None:
    """Sela status final e finished_at."""
    if status not in ("completed", "partial", "failed"):
        raise ValueError(f"status final inválido: {status}")
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE catalog_recipe_executions
            SET status=$2,
                total_cost_usd=$3,
                total_latency_ms=$4,
                error_message=$5,
                finished_at=now()
            WHERE id=$1
            """,
            execution_id, status, total_cost_usd, total_latency_ms, error_message,
        )


def _normalize_execution_row(r) -> dict:
    """Converte asyncpg.Record → dict, parseando JSONB em steps_results."""
    d = dict(r)
    raw_steps = d.get("steps_results")
    if isinstance(raw_steps, str):
        try:
            d["steps_results"] = json.loads(raw_steps)
        except (json.JSONDecodeError, TypeError):
            d["steps_results"] = []
    elif raw_steps is None:
        d["steps_results"] = []
    return d


# ─── Fila de revisão — filtra submissions órfãs (entry deletada) ──
# Root só pode decidir sobre submissions cuja entry ainda existe.
# Manter órfãs na fila gera ruído e ações inválidas. O FK CASCADE
# deveria limpar automaticamente; este filtro é defesa em profundidade
# para legado histórico (e para o caso raro de delete fora da API).


async def list_submissions_for_review(
    *,
    status: Optional[str] = "pending",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Lista submissões com a entry ainda existente (INNER JOIN).

    Submissions cuja entry foi deletada são excluídas — Root não pode
    decidir sobre algo que não existe. Cada submission retornada inclui
    três blocos aninhados para a UI Root decidir com contexto rico sem
    fazer N+1 lookups:

    - `entry`     — identidade + metadata da entry (name, kind, version,
                    urn, description, domain, visibility, scope, steward,
                    owner, status)
    - `disclosure`— capability disclosure (LEFT JOIN — pode ser None se
                    a entry submeteu sem declarar). Inclui flags + residency
                    + retention para a UI mostrar chips de risco
    - `submitter` — {id, email, role} do usuário que submeteu, ou None
                    se o user foi deletado depois

    Retorna (rows, total) onde total também respeita o filtro de existência.
    """
    pool = _get_pool()
    params: list[Any] = []
    where_parts: list[str] = []
    if status:
        params.append(status)
        where_parts.append(f"s.review_status = ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    count_sql = (
        f"SELECT COUNT(*) FROM catalog_submissions s "
        f"INNER JOIN catalog_entries e ON e.id = s.entry_id {where_sql}"
    )

    params_with_pag = params + [limit, offset]
    limit_ph = f"${len(params_with_pag)-1}"
    offset_ph = f"${len(params_with_pag)}"
    # Prefixos _entry_/_disc_/_user_ evitam colisão e permitem reagrupar
    # em dicts aninhados no Python. LEFT JOIN porque nem toda submission
    # tem disclosure (rejeições antigas) ou submitter ainda existente.
    list_sql = (
        f"SELECT s.*, "
        f"  e.name              AS _entry_name, "
        f"  e.kind              AS _entry_kind, "
        f"  e.version           AS _entry_version, "
        f"  e.urn               AS _entry_urn, "
        f"  e.description       AS _entry_description, "
        f"  e.domain            AS _entry_domain, "
        f"  e.visibility        AS _entry_visibility, "
        f"  e.visibility_scope  AS _entry_visibility_scope, "
        f"  e.steward_team      AS _entry_steward_team, "
        f"  e.owner_user_id     AS _entry_owner_user_id, "
        f"  e.status            AS _entry_status, "
        f"  d.reads_user_kb     AS _disc_reads_user_kb, "
        f"  d.writes_user_kb    AS _disc_writes_user_kb, "
        f"  d.calls_external_apis AS _disc_calls_external_apis, "
        f"  d.accesses_internet AS _disc_accesses_internet, "
        f"  d.stores_input      AS _disc_stores_input, "
        f"  d.storage_retention_days AS _disc_storage_retention_days, "
        f"  d.processes_pii     AS _disc_processes_pii, "
        f"  d.processes_financial AS _disc_processes_financial, "
        f"  d.processes_health  AS _disc_processes_health, "
        f"  d.trains_on_input   AS _disc_trains_on_input, "
        f"  d.output_is_deterministic AS _disc_output_is_deterministic, "
        f"  d.data_residency    AS _disc_data_residency, "
        f"  d.additional_notes  AS _disc_additional_notes, "
        f"  u.email             AS _user_email, "
        f"  u.role              AS _user_role "
        f"FROM catalog_submissions s "
        f"INNER JOIN catalog_entries e ON e.id = s.entry_id "
        f"LEFT JOIN catalog_capability_disclosure d ON d.entry_id = s.entry_id "
        f"LEFT JOIN users u ON u.id = s.submitted_by "
        f"{where_sql} "
        f"ORDER BY s.submitted_at DESC "
        f"LIMIT {limit_ph} OFFSET {offset_ph}"
    )

    async with pool.acquire() as con:
        rows = await con.fetch(list_sql, *params_with_pag)
        total = await con.fetchval(count_sql, *params) or 0

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        entry = {
            "id": d.get("entry_id"),
            "name": d.pop("_entry_name", None),
            "kind": d.pop("_entry_kind", None),
            "version": d.pop("_entry_version", None),
            "urn": d.pop("_entry_urn", None),
            "description": d.pop("_entry_description", None),
            "domain": d.pop("_entry_domain", None),
            "visibility": d.pop("_entry_visibility", None),
            "visibility_scope": d.pop("_entry_visibility_scope", None),
            "steward_team": d.pop("_entry_steward_team", None),
            "owner_user_id": d.pop("_entry_owner_user_id", None),
            "status": d.pop("_entry_status", None),
        }
        # Disclosure: se TODOS os campos vieram None (sem row no JOIN), retorna None.
        # Senão monta o dict — UI usa isto para os chips de capability.
        disc_keys = [
            "reads_user_kb", "writes_user_kb", "calls_external_apis", "accesses_internet",
            "stores_input", "storage_retention_days", "processes_pii", "processes_financial",
            "processes_health", "trains_on_input", "output_is_deterministic",
            "data_residency", "additional_notes",
        ]
        disc_values = {k: d.pop(f"_disc_{k}", None) for k in disc_keys}
        disclosure = disc_values if any(v is not None for v in disc_values.values()) else None

        submitter_email = d.pop("_user_email", None)
        submitter_role = d.pop("_user_role", None)
        submitter = {
            "id": d.get("submitted_by"),
            "email": submitter_email,
            "role": submitter_role,
        } if submitter_email is not None else None

        d["entry"] = entry
        d["disclosure"] = disclosure
        d["submitter"] = submitter
        out.append(d)
    return out, int(total)


async def cleanup_orphan_submissions() -> int:
    """Deleta submissions cuja entry referenciada não existe mais.

    Idempotente. Retorna a quantidade de rows deletadas. Usado pelo
    endpoint admin de cleanup; em condições normais (FK CASCADE ativo),
    nada deve ser deletado.
    """
    pool = _get_pool()
    async with pool.acquire() as con:
        res = await con.execute(
            """
            DELETE FROM catalog_submissions
            WHERE entry_id NOT IN (SELECT id FROM catalog_entries)
            """
        )
    # asyncpg.execute retorna "DELETE <n>"
    try:
        return int(res.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0
