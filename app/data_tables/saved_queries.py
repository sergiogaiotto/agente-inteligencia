"""Tier 2 — saved_queries: curadoria HUMANA de consultas em linguagem natural.

``apply_saved_query`` é o ÚNICO writer (DNA do Catálogo: IA propõe, HUMANO cura,
metadata curada é determinística). Revalida o struct contra a allow-list do
Catálogo VIVO (reusa ``validate_compiled_query`` — NÃO confia no que veio do
cliente), grava ``status='approved'`` + ``source='human'``, REDATA a pergunta
(``dlp``, pode conter PII) e faz ``json.dumps`` nos campos JSONB ANTES do asyncpg
(armadilha do Repository genérico). Em runtime SÓ executa ``status='approved'``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from app.core.database import saved_queries_repo
from app.core.dlp import redact_for_log
from app.data_tables.text_to_sql import render_sql_preview, validate_compiled_query
from app.evidence.tabular import TabularError

# Campos JSONB decodificados defensivamente na leitura (string legacy/mock → estrutura).
_JSON_FIELDS = (("query_json", dict), ("pii_columns_allowed", list))


def serialize_saved_query(row: Any) -> dict:
    """asyncpg.Record/dict → dict serializável; decoda os JSONB (query_json,
    pii_columns_allowed). Defensivo p/ string (legacy/mock) e None."""
    out = dict(row) if not isinstance(row, dict) else dict(row)
    for key, kind in _JSON_FIELDS:
        v = out.get(key)
        empty = {} if kind is dict else []
        if isinstance(v, str):
            try:
                out[key] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[key] = empty
        elif v is None:
            out[key] = empty
    return out


async def apply_saved_query(
    table_row: dict,
    name: str,
    question_nl: str,
    compiled: dict,
    pii_columns_allowed: list[str],
    user: dict,
    saved_query_id: Optional[str] = None,
) -> dict:
    """Cria/atualiza uma saved_query CURADA (``status='approved'``, ``source='human'``).

    ``table_row`` vem de ``find_by_id_with_ks`` (já autorizado por visibility no
    caller, e traz ``catalog`` reconciliado). REVALIDA o struct determinísticamente
    contra a allow-list do Catálogo — coluna/op inválida ou PII não liberada são
    descartadas; struct vazio após validação → 400 (não persiste consulta inútil).

    Retorna a saved_query serializada + ``blocked`` (o que a validação descartou).
    """
    catalog = table_row.get("catalog") or {}
    allowed_pii = [c for c in (pii_columns_allowed or []) if isinstance(c, str)]

    result = validate_compiled_query(compiled or {}, catalog, allowed_pii)
    safe = result["compiled"]
    blocked = result["blocked"]
    if not safe.get("select") and not safe.get("filters"):
        detail = "; ".join(blocked[:3]) if blocked else "verifique colunas/operadores."
        raise TabularError(
            f"Consulta vazia após validação — nada a salvar. {detail}",
            status_code=400,
        )

    now = datetime.utcnow()
    uid = (user or {}).get("id") or ""
    sq_id = saved_query_id or str(uuid.uuid4())

    payload = {
        "name": (str(name or "").strip()[:200]) or "Consulta",
        # PII fora da persistência: a pergunta crua pode conter CPF/e-mail/etc.
        "question_nl": redact_for_log(str(question_nl or "")),
        # JSONB: dumps ANTES do asyncpg (Repository passa o valor cru).
        "query_json": json.dumps(safe, ensure_ascii=False),
        "sql_preview": render_sql_preview(safe),
        "status": "approved",
        "source": "human",
        "pii_columns_allowed": json.dumps(allowed_pii, ensure_ascii=False),
        "curated_by": uid,
        "curated_at": now,
        "updated_at": now,
    }

    existing = await saved_queries_repo.find_by_id(sq_id) if saved_query_id else None
    if existing:
        if existing.get("data_table_id") != table_row.get("id"):
            raise TabularError("Consulta pertence a outra tabela.", status_code=400)
        await saved_queries_repo.update(sq_id, payload)
    else:
        await saved_queries_repo.create({
            "id": sq_id,
            "data_table_id": table_row.get("id"),
            "created_at": now,
            **payload,
        })

    row = await saved_queries_repo.find_by_id(sq_id)
    out = serialize_saved_query(row) if row else {"id": sq_id, **payload}
    out["blocked"] = blocked
    return out


async def list_saved_queries(table_id: str) -> list[dict]:
    """Lista as saved_queries de uma tabela (mais recentes primeiro)."""
    rows = await saved_queries_repo.find_all(limit=200, data_table_id=table_id)
    return [serialize_saved_query(r) for r in rows]


async def get_saved_query(sq_id: str) -> Optional[dict]:
    row = await saved_queries_repo.find_by_id(sq_id)
    return serialize_saved_query(row) if row else None


async def delete_saved_query(sq_id: str) -> bool:
    return await saved_queries_repo.delete(sq_id)
