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
from app.data_tables.governance import allowed_cols_from_catalog
from app.data_tables.queries import (
    can_user_see,
    find_by_id_with_ks,
    list_for_user,
)
from app.data_tables.runtime import text_to_sql_enabled
from app.evidence.tabular import (
    TabularError,
    analyze_tabular,
    append_to_table,
    delete_all_tables_for_ks,
    delete_table,
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


class CatalogColumnSpec(BaseModel):
    """Entrada curada de UMA coluna no Catálogo de Dados."""
    name: str
    description: str = ""
    pii_category: str = "none"


class CatalogPutRequest(BaseModel):
    """Curadoria humana do catálogo: descrição da tabela + colunas."""
    description: str = ""
    columns: list[CatalogColumnSpec] = Field(default_factory=list)


class CatalogSuggestRequest(BaseModel):
    """Parâmetros da sugestão por IA (não persiste)."""
    sample_size: int = 10


class CompileQueryRequest(BaseModel):
    """Tier 2 — pergunta em PT-BR p/ a bancada compilar em consulta estruturada."""
    question: str = ""
    sample_size: int = 10


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


@router.put("/data-tables/{table_id}/catalog")
async def put_data_table_catalog_endpoint(
    table_id: str,
    body: CatalogPutRequest,
    user: dict = Depends(require_user),
):
    """Curadoria HUMANA do Catálogo de Dados (única escrita do catálogo).

    DNA anti-alucinação: a IA só SUGERE (PR3, sem persistir); este PUT é o único
    caminho que grava. 404 se não existe; 403 se visibility bloqueia; 400 se uma
    coluna não existe no schema ou pii_category está fora do enum.
    """
    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para editar esta tabela.")
    from app.data_tables.catalog import apply_catalog
    try:
        updated = await apply_catalog(
            row, body.description, [c.model_dump() for c in body.columns], user
        )
    except TabularError as e:
        raise _raise_tabular(e)
    await _audit(
        "data_table.catalog.update",
        table_id,
        user.get("id", ""),
        {"columns": len(body.columns)},
    )
    return {"ok": True, "table": updated}


@router.post("/data-tables/{table_id}/catalog/suggest")
async def suggest_data_table_catalog_endpoint(
    table_id: str,
    body: CatalogSuggestRequest = CatalogSuggestRequest(),
    user: dict = Depends(require_user),
):
    """Sugestão do catálogo via IA — NÃO persiste (volátil; só o PUT grava).

    DNA anti-alucinação: lê schema + amostra read-only (OMITIDA p/ bases
    restricted/confidential — PII não vai ao provedor) e o LLM NUNCA define a
    lista de colunas (reconciliação por nome contra o schema vivo).
    """
    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para esta tabela.")

    # Amostra read-only só p/ public/internal (PII fora do prompt em sensíveis).
    sample_rows: list = []
    label = str(row.get("ks_confidentiality_label") or "internal").lower()
    if label in ("public", "internal"):
        try:
            n = max(1, min(int(body.sample_size or 10), 20))
            res = await execute_query(table_id, limit=n, executed_by=user.get("id"))
            sample_rows = res.get("rows", []) or []
        except Exception:
            sample_rows = []  # amostra é best-effort; segue só com o schema

    from app.llm_routing import resolve_llm_for_task
    from app.routes.wizard import _wizard_llm_complete
    from app.data_tables.catalog import generate_catalog_suggestion

    provider, model = await resolve_llm_for_task("instruct")

    async def _complete(messages: list) -> str:
        content, _, _ = await _wizard_llm_complete(
            messages, provider, model, route="catalog_suggest"
        )
        return content

    try:
        suggestion = await generate_catalog_suggestion(row, sample_rows, _complete)
    except HTTPException:
        raise  # 503 acionável do _wizard_llm_complete (LLM inacessível)
    except Exception as e:
        logger.error("catalog suggest falhou", exc_info=True)
        raise HTTPException(500, f"Erro ao gerar sugestão: {e}")

    await _audit(
        "data_table.catalog.suggest",
        table_id,
        user.get("id", ""),
        {"columns": len(suggestion.get("columns", [])), "sampled": len(sample_rows)},
    )
    return {"ok": True, "suggestion": suggestion}


@router.post("/data-tables/{table_id}/compile-query")
async def compile_query_endpoint(
    table_id: str,
    body: CompileQueryRequest,
    user: dict = Depends(require_user),
):
    """Tier 2 — bancada "Perguntar à Tabela": compila uma pergunta em PT-BR numa
    consulta ESTRUTURADA, governada pelo Catálogo de Dados.

    DRY-RUN: NÃO executa e NÃO persiste — só devolve o struct + preview de SQL +
    o que foi bloqueado, p/ o humano revisar/curar. Gated por TEXT_TO_SQL_ENABLED
    (flag OFF → recurso não existe). Pipeline de gates:
    visibility → anti-injeção (prompt_guard) → allow-list/masking do Catálogo
    (o LLM só vê colunas liberadas) → parse defensivo → validação determinística.
    """
    if not text_to_sql_enabled():
        # Mecanismo desligado → o recurso simplesmente não existe.
        raise HTTPException(404, "Recurso indisponível (Tier 2 desativado).")

    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para esta tabela.")

    question = (body.question or "").strip()
    if not question:
        raise HTTPException(422, "Pergunta vazia.")

    # Gate anti-injeção de prompt (OWASP LLM01) ANTES de tocar o LLM.
    from app.core.prompt_guard import detect as pg_detect

    guard = pg_detect(question)
    if guard.blocked:
        logger.warning(
            "text_to_sql.prompt_blocked",
            extra={
                "event": "text_to_sql.prompt_blocked",
                "table_id": table_id,
                "score": round(guard.score, 3),
            },
        )
        raise HTTPException(422, "Pergunta bloqueada por suspeita de injeção de prompt.")

    catalog = row.get("catalog") or {}
    allowed = allowed_cols_from_catalog(catalog)

    # Amostra read-only só p/ public/internal e SÓ colunas liberadas (sem PII no
    # prompt). Best-effort: falha → segue só com o schema.
    sample_rows: list = []
    label = str(row.get("ks_confidentiality_label") or "internal").lower()
    if allowed and label in ("public", "internal"):
        try:
            n = max(1, min(int(body.sample_size or 10), 20))
            res = await execute_query(table_id, select=allowed, limit=n, executed_by=user.get("id"))
            sample_rows = res.get("rows", []) or []
        except Exception:
            sample_rows = []

    from app.llm_routing import resolve_llm_for_task
    from app.routes.wizard import _wizard_llm_complete
    from app.data_tables.text_to_sql import compile_question

    provider, model = await resolve_llm_for_task("instruct")

    async def _complete(messages: list) -> str:
        # Determinismo: temperature=0 + JSON-mode (struct executável, não prosa).
        content, _, _ = await _wizard_llm_complete(
            messages, provider, model, route="text_to_sql_compile",
            temperature=0.0, response_format={"type": "json_object"},
        )
        return content

    try:
        result = await compile_question(row, catalog, sample_rows, question, _complete)
    except HTTPException:
        raise  # 503 acionável do _wizard_llm_complete (LLM inacessível)
    except Exception as e:
        logger.error("text_to_sql compile falhou", exc_info=True)
        raise HTTPException(500, f"Erro ao compilar pergunta: {e}")

    # Audit best-effort: SÓ metadata — a pergunta crua pode conter PII, não loga.
    await _audit(
        "data_table.text_to_sql.compile",
        table_id,
        user.get("id", ""),
        {
            "allowed_cols": len(allowed),
            "select": len(result["compiled"]["select"]),
            "filters": len(result["compiled"]["filters"]),
            "blocked": len(result.get("blocked") or []),
            "sampled": len(sample_rows),
        },
    )
    return {"ok": True, **result}


@router.delete("/data-tables/{table_id}")
async def delete_data_table_endpoint(
    table_id: str,
    user: dict = Depends(require_user),
):
    """Apaga uma tabela individual (arquivo .duckdb + linha em data_tables).

    Visibility: 403 se o user não puder VER a tabela. Idempotente: 404 se
    a tabela já não existir. Audit registra ação + usuário + KS.

    Não há "soft delete" — o registro é removido para sempre (re-upload do
    mesmo arquivo gera nova versão via slug+version).
    """
    row = await find_by_id_with_ks(table_id)
    if not row:
        raise HTTPException(404, f"data_table '{table_id}' não encontrada.")
    if not can_user_see(user, row):
        raise HTTPException(403, "Sem permissão para excluir esta tabela.")

    try:
        result = await delete_table(table_id, deleted_by=user.get("id"))
    except Exception as e:
        logger.error("data_table delete falhou", exc_info=True)
        raise HTTPException(500, f"Erro ao deletar tabela: {e}")

    await _audit(
        "data_table.delete",
        table_id,
        user.get("id", ""),
        {
            "name": result.get("name", ""),
            "ks_id": result.get("ks_id", ""),
            "size_freed_bytes": result.get("size_freed_bytes", 0),
        },
    )
    return result


@router.delete("/knowledge-sources/{ks_id}/tables")
async def delete_all_tables_for_ks_endpoint(
    ks_id: str,
    user: dict = Depends(require_user),
):
    """Apaga TODAS as tabelas visíveis ao user nesta KB.

    Equivalente tabular de `DELETE /knowledge-sources/{ks_id}/chunks`.
    Mantém a fonte registrada — só limpa as tabelas. Útil quando o operador
    quer "começar do zero" sem desfazer o cadastro da KB.

    Visibility: respeita `list_for_user` — root apaga tudo, user comum só
    apaga o que pode ver. Idempotente: KB sem tabelas retorna
    `{deleted: 0, freed_bytes: 0}`.
    """
    try:
        result = await delete_all_tables_for_ks(
            ks_id, user, deleted_by=user.get("id")
        )
    except Exception as e:
        logger.error("data_tables bulk delete falhou", exc_info=True)
        raise HTTPException(500, f"Erro ao deletar tabelas: {e}")

    await _audit(
        "data_table.delete_all",
        ks_id,
        user.get("id", ""),
        {
            "ks_id": ks_id,
            "deleted_count": result.get("deleted", 0),
            "freed_bytes": result.get("freed_bytes", 0),
        },
    )
    return result


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
