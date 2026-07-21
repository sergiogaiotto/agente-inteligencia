"""Autoria de entidades via audit_log (66.3.0) — batelado e best-effort.

Extraído do `_agents_authorship` da 66.2.0 para reuso (agents, skills, …).
Regras herdadas dos achados de revisão daquela rodada:
- "última alteração" usa WHITELIST de ações de mudança humana — nunca
  blacklist (eventos de RUNTIME como 'tool_strategy_degraded'/'invoked'
  atribuiriam autoria ao último invocador);
- actor pode ser username OU user_id cru (fallback do AuditRepository via
  user_id_var) — resolvido em 1 query batelada;
- best-effort: qualquer falha → {} (decoração nunca derruba a listagem).
Requer o índice idx_audit_log_entity (66.2.0).
"""
from __future__ import annotations


async def audit_entity_authorship(entity_type: str, ids: list[str],
                                  change_actions: list[str]) -> dict:
    """id → {created_by, created_by_name, updated_by, updated_by_name,
    last_change_action, last_change_at} — 2 queries + resolução de nomes."""
    if not ids:
        return {}
    try:
        from app.core.database import _get_pool
        async with _get_pool().acquire() as con:
            created = await con.fetch(
                "SELECT DISTINCT ON (entity_id) entity_id, actor "
                "FROM audit_log WHERE entity_type=$2 AND action='created' "
                "AND entity_id = ANY($1::text[]) ORDER BY entity_id, id ASC",
                ids, entity_type)
            changed = await con.fetch(
                "SELECT DISTINCT ON (entity_id) entity_id, actor, action, created_at "
                "FROM audit_log WHERE entity_type=$3 AND action = ANY($2::text[]) "
                "AND entity_id = ANY($1::text[]) ORDER BY entity_id, id DESC",
                ids, change_actions, entity_type)
        out: dict = {}
        for r in created:
            out.setdefault(r["entity_id"], {})["created_by"] = r["actor"]
        for r in changed:
            d = out.setdefault(r["entity_id"], {})
            d["updated_by"] = r["actor"]
            d["last_change_action"] = r["action"]
            d["last_change_at"] = str(r["created_at"] or "")
        actors = {v.get(k) for v in out.values()
                  for k in ("created_by", "updated_by") if v.get(k)}
        from app.routes.dashboard import _resolve_user_names
        names = await _resolve_user_names(list(actors))
        for d in out.values():
            d["created_by_name"] = names.get(d.get("created_by"), d.get("created_by"))
            d["updated_by_name"] = names.get(d.get("updated_by"), d.get("updated_by"))
        return out
    except Exception:
        return {}
