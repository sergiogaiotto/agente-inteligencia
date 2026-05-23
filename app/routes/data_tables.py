"""Onda Tabular — endpoints REST para promoção e consulta de tabelas.

Endpoints:
- POST /api/v1/knowledge-sources/{ks_id}/analyze-tabular
    Multipart file → análise sem ingerir. Retorna score + schema + warnings.
    Usado pelo modal "Promover para tabela?" no frontend de KB.

- POST /api/v1/knowledge-sources/{ks_id}/promote-to-table
    Multipart file + name + description → cria .duckdb + registra em data_tables.
    Idempotente via versionamento por slug (re-upload gera v2, v3...).

- GET /api/v1/data-tables
    Lista tabelas visíveis ao user. Filtragem em SQL (visibility-aware).
    Query param `ks_id` opcional para filtrar por knowledge_source.

- GET /api/v1/data-tables/{table_id}
    Detalhes + schema completo. 403 se user não pode ver (visibility).

- POST /api/v1/data-tables/{table_id}/query
    Executa SELECT parametrizado. Body: { inputs, select, filters, order_by, limit }.
    Audit em data_table_query_logs (toda chamada).

Convenções aplicadas:
- Auth: Depends(require_user) em todos (#1)
- Audit: audit_repo.create em promote + delete (eventos discretos) (#4)
- Visibility: filtragem em SQL via data_tables.queries (#3)
- Hardcoded: limites em data_tables/types.py (#9)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.core.auth import require_user
from app.core.database import audit_repo
from app.data_tables.queries import (
    can_user_see,
    find_by_id_with_ks,
    list_for_user,
)
from app.evidence.tabular import (
    TabularError,
    analyze_tabular,
    append_to_table,
    execute_query,
    promote_to_table,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["data_tables"])


# ─── Helpers ─────────────────────────────────────────────────────


async def _audit(action: str, table_id: str, actor_id: str, details: Optional[dict] = None) -> None:
    """Best-effort. Falha não bloqueia a request."""
    try:
        await audit_repo.create({
            "entity_type": "data_table",
            "entity_id": table_id,
            "action": action,
            "actor": actor_id,
            "details": json.dumps(details or {}),
        })
    except Exception as e:
        logger.warning(f"audit log falhou para {action} on {table_id}: {e}")


def _raise_tabular(e: TabularError) -> HTTPException:
    """Converte TabularError em HTTPException com status_code adequado."""
    return HTTPException(e.status_code, str(e))


# ─── Models de request/response ──────────────────────────────────


class FilterSpec(BaseModel):
    """Filtro WHERE no Query Builder.

    `op` é string do SqlOperator (=, !=, >, >=, <, <=, LIKE, ILIKE,
    IN, NOT IN, BETWEEN, IS NULL, IS NOT NULL).
    `value` para IN/NOT IN deve ser lista; para BETWEEN, lista [low, high];
    para IS NULL/IS NOT NULL, ignorado.
    `if_present` (opcional): nome da chave em `inputs` — se ausente/vazia
    pula o filtro (útil para filtros opcionais).
    """
    col: str
    op: str
    value: Any = None
    if_present: Optional[str] = None


class QueryRequest(BaseModel):
    inputs: dict = Field(default_factory=dict)
    select: list[str] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    limit: int = 100


# ─── Endpoints ───────────────────────────────────────────────────


@router.post("/knowledge-sources/{ks_id}/analyze-tabular")
async def analyze_tabular_endpoint(
    ks_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    """Analisa CSV/XLSX SEM ingerir. Retorna score + schema + warnings.

    O usuário decide se vale promover. Não há efeito colateral além de
    consumir RAM temporariamente (DuckDB :memory:).
    """
    try:
        data = await file.read()
        return await analyze_tabular(data, file.filename or "upload.bin", file.content_type)
    except TabularError as e:
        raise _raise_tabular(e)


@router.post("/knowledge-sources/{ks_id}/promote-to-table")
async def promote_to_table_endpoint(
    ks_id: str,
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    description: str = Form(""),
    sheet_name: Optional[str] = Form(None),
    header_row: Optional[int] = Form(None),
    user: dict = Depends(require_user),
):
    """Cria .duckdb persistente + registra em data_tables.

    Idempotente via versionamento: re-promover o mesmo CSV gera v2, v3...
    URN é único.

    XLSX multi-aba: `sheet_name` escolhe qual aba promover. Sem ele, usa
    a "primária" (maior score na análise). Para promover N abas, chame N
    vezes com sheet_name diferente — cada uma gera 1 data_table.

    `header_row` (1-based) força a linha do header. None = usar a que a
    análise auto-detectou (default = 1, ou 2 se a heurística achou que
    a linha 1 era título mergeado).
    """
    try:
        data = await file.read()
        result = await promote_to_table(
            ks_id=ks_id,
            data=data,
            filename=file.filename or "upload.bin",
            name=name,
            description=description,
            created_by=user.get("id"),
            sheet_name=sheet_name,
            header_row=header_row,
        )
        await _audit(
            action="data_table.promote",
            table_id=result.get("id", ""),
            actor_id=user.get("id", ""),
            details={
                "ks_id": ks_id,
                "urn": result.get("urn"),
                "rows": result.get("row_count"),
                "columns": result.get("column_count"),
                "score": result.get("quality_score"),
            },
        )
        return result
    except TabularError as e:
        raise _raise_tabular(e)


@router.post("/data-tables/{table_id}/append")
async def append_to_table_endpoint(
    table_id: str,
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
    header_row: Optional[int] = Form(None),
    user: dict = Depends(require_user),
):
    """Adiciona linhas a uma data_table EXISTENTE (incremento).

    Diferente de `/promote-to-table` (que cria nova versão), este endpoint
    abre o arquivo .duckdb da tabela em modo WRITE e faz INSERT das novas
    linhas. Schema NÃO muda — colunas extras no arquivo novo são ignoradas;
    faltantes ficam NULL.

    Útil para KS tipo 'tabular' onde re-upload é semanticamente "adicionar
    linhas" e não "criar nova versão".
    """
    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para appendar nesta tabela.")

    try:
        data = await file.read()
        result = await append_to_table(
            target_table_id=table_id,
            data=data,
            filename=file.filename or "upload.bin",
            sheet_name=sheet_name,
            header_row=header_row,
        )
        await _audit(
            action="data_table.append",
            table_id=table_id,
            actor_id=user.get("id", ""),
            details={
                "rows_added": result.get("rows_added"),
                "row_count_after": result.get("row_count_after"),
                "sheet_name": sheet_name,
            },
        )
        return result
    except TabularError as e:
        raise _raise_tabular(e)


@router.get("/data-tables")
async def list_data_tables_endpoint(
    ks_id: Optional[str] = Query(None, description="Filtrar por knowledge_source"),
    user: dict = Depends(require_user),
):
    """Lista visibility-aware. Filtragem em SQL via list_for_user."""
    rows = await list_for_user(user, ks_id=ks_id)
    return {"data_tables": rows, "total": len(rows)}


@router.get("/data-tables/{table_id}")
async def get_data_table_endpoint(
    table_id: str,
    user: dict = Depends(require_user),
):
    """Detalhes + schema. 403 se visibility bloqueia, 404 se não existe."""
    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para acessar esta tabela.")
    return row


@router.post("/data-tables/{table_id}/query")
async def query_data_table_endpoint(
    table_id: str,
    payload: QueryRequest,
    user: dict = Depends(require_user),
):
    """Executa SELECT parametrizado com bind vars. Read-only. Audit obrigatório.

    Validação: colunas em select/filters/order_by devem existir no schema.
    Operadores validados contra SqlOperator enum. Limit hard-capped em
    MAX_ROWS_RETURNED.
    """
    # Visibility check ANTES de executar
    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para consultar esta tabela.")

    try:
        return await execute_query(
            table_id=table_id,
            inputs=payload.inputs,
            select=payload.select,
            filters=[f.model_dump() for f in payload.filters],
            order_by=payload.order_by,
            limit=payload.limit,
            executed_by=user.get("id"),
        )
    except TabularError as e:
        raise _raise_tabular(e)
