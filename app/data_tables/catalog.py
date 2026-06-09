"""Catálogo de Dados — service de curadoria (Onda Catálogo).

`apply_catalog` é a ÚNICA escrita do catálogo (DNA: IA sugere, HUMANO cura,
metadata curada é determinística). A geração por IA (sugestão volátil) vem no
PR3 e NÃO persiste — só este caminho grava.

Valida nomes ∈ schema_json VIVO e pii_category ∈ enum FECHADO, monta o
catalog_json indexado por NOME com proveniência 'human', e grava com
json.dumps ANTES do asyncpg (armadilha JSONB+asyncpg: Repository.update passa o
valor cru → dict em coluna JSONB quebra com "expected str, got dict").
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.core.database import data_tables_repo
from app.data_tables.queries import find_by_id_with_ks
from app.data_tables.types import PiiCategory
from app.evidence.tabular import TabularError


async def apply_catalog(
    row: dict,
    table_description: str,
    columns: list[dict],
    user: dict,
) -> dict:
    """Persiste a curadoria HUMANA do catálogo de uma data_table.

    `row` vem de find_by_id_with_ks (já autorizado por visibility no caller).
    Cada coluna é validada contra o schema VIVO (nome inexistente → 400) e a
    pii_category contra o enum fechado (inválida → 400; aqui REJEITA, diferente
    da sugestão da IA que coage p/ 'none'). Retorna o row reconciliado atualizado.
    """
    table_id = row.get("id")
    schema = row.get("schema_json") or []
    schema_names = {c.get("name") for c in schema if isinstance(c, dict)}

    now_iso = datetime.utcnow().isoformat()
    uid = (user or {}).get("id") or ""

    cat_columns: dict[str, Any] = {}
    for c in (columns or []):
        name = str(c.get("name") or "").strip()
        if name not in schema_names:
            raise TabularError(
                f"Coluna '{name}' não existe no schema da tabela.", status_code=400
            )
        pii_raw = c.get("pii_category", "none")
        try:
            pii = PiiCategory(str(pii_raw).strip().lower()).value
        except ValueError:
            raise TabularError(
                f"pii_category inválida: '{pii_raw}'. Aceitas: "
                f"{[p.value for p in PiiCategory]}.",
                status_code=400,
            )
        cat_columns[name] = {
            "description": str(c.get("description") or "").strip(),
            "pii_category": pii,
            "source": "human",
            "curated_by": uid,
            "curated_at": now_iso,
        }

    catalog_json = {
        "version": 1,
        "table": {
            "description_source": "human",
            "curated_by": uid,
            "curated_at": now_iso,
        },
        "columns": cat_columns,
    }

    await data_tables_repo.update(table_id, {
        # JSONB: dumps ANTES do asyncpg (Repository.update passa o valor cru).
        "catalog_json": json.dumps(catalog_json, ensure_ascii=False),
        "description": str(table_description or "").strip(),
        "updated_at": datetime.utcnow(),
    })
    return await find_by_id_with_ks(table_id)
