"""Onda Tabular — ingestão de CSV/XLSX para tabela DuckDB consultável.

Três operações:

1. `analyze_tabular(data, filename, mime_type)` — dry-run em memória:
   carrega via DuckDB :memory:, infere schema, calcula score de qualidade,
   detecta PK candidata. NÃO persiste nada. Usado pelo modal
   "Promover para tabela?".

2. `promote_to_table(ks_id, data, filename, name, ...)` — cria arquivo
   .duckdb persistente em `data/tabular/<ks_id>/<table_id>.duckdb`,
   ingere os dados, grava metadata em Postgres (data_tables).
   Idempotente via versionamento por slug (re-promove gera v2, v3...).

3. `execute_query(table_id, inputs, select, filters, order_by, limit, ...)`
   — abre o .duckdb em modo READ-ONLY, valida select/filters contra o
   schema, monta SELECT parametrizado com bind vars (?), executa, audita
   em data_table_query_logs. Limit enforcement.

Decisões:
- Engine: DuckDB embarcado. read_csv_auto/read_xlsx nativos. Read-only
  por execução = safety técnica (não só prompt).
- Storage: 1 arquivo .duckdb por tabela (isolamento total, drop = rm).
  Caminho: `data/tabular/<ks_id>/<table_id>.duckdb`.
- Nome da tabela interna: sempre "data" (1 tabela por arquivo no MVP).
- Tipos: inferidos automaticamente pelo DuckDB (smart inference).
- Limites: ver `app/data_tables/types.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.core.config import get_settings
from app.core.database import (
    _get_pool,
    data_table_query_logs_repo,
    data_tables_repo,
    knowledge_repo,
)
from app.data_tables.queries import (
    build_urn,
    find_by_id_with_ks,
    next_version_for_slug,
    slugify,
)
from app.data_tables.types import (
    MAX_COLUMNS,
    MAX_ROWS_RETURNED,
    MAX_TABLE_SIZE_MB,
    SqlOperator,
    render_where_clause,
)

logger = logging.getLogger(__name__)


class TabularError(Exception):
    """Erro de ingestão/query tabular com HTTP status sugerido."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


# ─── Internals: DuckDB e arquivo ─────────────────────────────────


_DUCKDB_TABLE = "data"  # Nome da tabela interna no .duckdb (1 por arquivo)
_TABULAR_ROOT = Path("data") / "tabular"


def _import_duckdb():
    """Import lazy para não pagar custo de import quando feature não é usada."""
    try:
        import duckdb  # type: ignore
        return duckdb
    except ImportError as e:
        raise TabularError(
            "DuckDB não instalado. Execute `pip install duckdb>=1.0.0` "
            "ou `pip install -r requirements.txt`.",
            status_code=503,
        ) from e


def _ext_from_filename(filename: str) -> str:
    """Retorna 'csv' | 'xlsx' | 'unsupported' a partir da extensão."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return "xlsx"
    return "unsupported"


def _read_into_duckdb(con, file_path: str, ext: str) -> None:
    """Cria `data` table a partir do arquivo. Trata XLSX via extension `excel`."""
    if ext == "csv":
        # read_csv_auto: HEADER detect, type inference, delimiter sniffer.
        # IGNORE_ERRORS=false para falhar em CSV malformado (preferimos erro claro).
        # Path precisa ser escapado (replace '). Caminho vem de tempfile/Path,
        # não de input do usuário — risco baixo, mas bom hábito.
        safe = file_path.replace("'", "''")
        con.execute(
            f"CREATE TABLE {_DUCKDB_TABLE} AS "
            f"SELECT * FROM read_csv_auto('{safe}', HEADER=TRUE)"
        )
        return
    if ext == "xlsx":
        # Extension 'excel' (DuckDB 1.0+) traz read_xlsx. Auto-install + load.
        # Em ambiente offline pode falhar; mensagem clara.
        try:
            con.execute("INSTALL excel")
            con.execute("LOAD excel")
        except Exception as e:
            raise TabularError(
                "Extensão 'excel' do DuckDB indisponível "
                "(offline ou sem permissão de instalar). "
                "Solução: converter para CSV antes do upload.",
                status_code=503,
            ) from e
        safe = file_path.replace("'", "''")
        con.execute(
            f"CREATE TABLE {_DUCKDB_TABLE} AS "
            f"SELECT * FROM read_xlsx('{safe}')"
        )
        return
    raise TabularError(
        f"Formato não suportado: {ext}. Aceitos: csv, xlsx.",
        status_code=400,
    )


def _describe_schema(con) -> list[dict]:
    """DESCRIBE data → [{name, type, nullable}]."""
    rows = con.execute(f"DESCRIBE {_DUCKDB_TABLE}").fetchall()
    # DESCRIBE retorna: (column_name, column_type, null, key, default, extra)
    schema = []
    for r in rows:
        schema.append({
            "name": r[0],
            "type": r[1],
            "nullable": str(r[2]).upper() == "YES",
        })
    return schema


def _compute_quality_score(con, schema: list[dict], row_count: int) -> tuple[float, list[str], Optional[str]]:
    """Score (0.0-1.0) heurístico + warnings + PK sugerida.

    Penalidades:
    - % de colunas 100% nulas: -0.3
    - % de colunas com >50% nulos: -0.2
    - Coluna sem nome (DuckDB nomeia "column0", "column1" se sem header): -0.2
    - row_count < 2: -0.5 (provavelmente só header)

    PK candidata: primeira coluna com COUNT(DISTINCT col) == COUNT(*) AND
    COUNT(*) WHERE col IS NULL == 0.
    """
    score = 1.0
    warnings: list[str] = []

    if row_count < 2:
        score -= 0.5
        warnings.append(f"Apenas {row_count} linha(s) — planilha quase vazia.")

    if not schema:
        return 0.0, ["Schema vazio."], None

    # Colunas sem nome real (DuckDB usa "column0" etc quando não há header)
    generic_cols = [c for c in schema if str(c["name"]).startswith("column") and str(c["name"])[6:].isdigit()]
    if generic_cols:
        score -= 0.2
        warnings.append(f"{len(generic_cols)} coluna(s) sem cabeçalho (nomes genéricos column0, column1...).")

    # Análise de nulos por coluna
    null_heavy_cols = []
    fully_null_cols = []
    pk_candidate: Optional[str] = None

    for col in schema:
        col_name = col["name"]
        # Quote identifier para nomes com espaço/símbolo. DuckDB usa "".
        qcol = '"' + col_name.replace('"', '""') + '"'
        try:
            r = con.execute(
                f"SELECT COUNT(*) AS total, COUNT({qcol}) AS not_null, "
                f"COUNT(DISTINCT {qcol}) AS distinct_v FROM {_DUCKDB_TABLE}"
            ).fetchone()
            total, not_null, distinct_v = r[0], r[1], r[2]
        except Exception:
            continue

        if total == 0:
            continue
        null_pct = 1.0 - (not_null / total)
        if null_pct >= 0.99:
            fully_null_cols.append(col_name)
        elif null_pct >= 0.5:
            null_heavy_cols.append((col_name, null_pct))

        # PK: primeira coluna 100% única E 100% não-nula
        if pk_candidate is None and not_null == total and distinct_v == total and total > 1:
            pk_candidate = col_name

    if fully_null_cols:
        score -= 0.3
        warnings.append(f"{len(fully_null_cols)} coluna(s) 100% nulas: {', '.join(fully_null_cols[:3])}.")
    if null_heavy_cols:
        score -= 0.2
        sample = [f"{n} ({pct:.0%})" for n, pct in null_heavy_cols[:3]]
        warnings.append(f"{len(null_heavy_cols)} coluna(s) com mais de 50% nulos: {', '.join(sample)}.")

    score = max(0.0, min(1.0, score))
    return round(score, 3), warnings, pk_candidate


def _validate_size(data: bytes) -> None:
    """Rejeita arquivos acima do limite ANTES de carregar."""
    size_mb = len(data) / (1024 * 1024)
    if size_mb > MAX_TABLE_SIZE_MB:
        raise TabularError(
            f"Arquivo {size_mb:.1f}MB excede limite de {MAX_TABLE_SIZE_MB}MB. "
            f"Reduza o arquivo (filtre linhas/colunas) ou peça aumento do limite.",
            status_code=413,
        )


# ─── 1. ANALYZE ──────────────────────────────────────────────────


async def analyze_tabular(data: bytes, filename: str, mime_type: Optional[str] = None) -> dict:
    """Dry-run: carrega em DuckDB :memory:, infere schema, calcula score.

    NÃO persiste arquivo nem grava em Postgres. Usado pelo modal de promoção
    para o usuário decidir se vale a pena criar a tabela.

    Returns:
        {
          "tabular_ready": bool,    # score >= TABULAR_READY_THRESHOLD
          "score": float,           # 0.0-1.0
          "ext": "csv" | "xlsx",
          "rows": int,
          "columns": int,
          "schema": [{name, type, nullable}],
          "warnings": [str, ...],
          "suggested_pk": str | None,
          "size_bytes": int,
        }
    """
    _validate_size(data)
    ext = _ext_from_filename(filename)
    if ext == "unsupported":
        raise TabularError(
            f"Extensão não reconhecida em '{filename}'. Aceitos: .csv, .xlsx.",
            status_code=400,
        )

    duckdb = _import_duckdb()

    # CPU-bound: roda em thread pool para não bloquear event loop
    def _run() -> dict:
        with tempfile.NamedTemporaryFile(
            suffix=f".{ext}", delete=False
        ) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            con = duckdb.connect(":memory:")
            try:
                _read_into_duckdb(con, tmp_path, ext)
                schema = _describe_schema(con)
                if len(schema) > MAX_COLUMNS:
                    raise TabularError(
                        f"Planilha com {len(schema)} colunas excede limite de {MAX_COLUMNS}. "
                        f"Reduza/divida o arquivo.",
                        status_code=413,
                    )
                row_count = con.execute(f"SELECT COUNT(*) FROM {_DUCKDB_TABLE}").fetchone()[0]
                score, warnings, pk = _compute_quality_score(con, schema, row_count)
                from app.data_tables.types import TABULAR_READY_THRESHOLD
                return {
                    "tabular_ready": score >= TABULAR_READY_THRESHOLD,
                    "score": score,
                    "ext": ext,
                    "rows": row_count,
                    "columns": len(schema),
                    "schema": schema,
                    "warnings": warnings,
                    "suggested_pk": pk,
                    "size_bytes": len(data),
                }
            finally:
                con.close()
        except TabularError:
            raise
        except Exception as e:
            raise TabularError(
                f"Falha ao analisar arquivo: {e}. "
                f"Verifique se o CSV/XLSX está bem-formado.",
                status_code=400,
            ) from e
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return await asyncio.to_thread(_run)


# ─── 2. PROMOTE ──────────────────────────────────────────────────


async def promote_to_table(
    ks_id: str,
    data: bytes,
    filename: str,
    name: Optional[str] = None,
    description: str = "",
    created_by: Optional[str] = None,
) -> dict:
    """Cria arquivo .duckdb persistente e registra em data_tables.

    Args:
        ks_id: knowledge_source de origem. Deve existir.
        data: bytes do CSV/XLSX.
        filename: nome original (usado para slug + extensão).
        name: nome amigável da tabela. Default = filename sem extensão.
        description: descrição livre.
        created_by: user id (para audit).

    Returns:
        Dict da tabela criada (com `id`, `urn`, `schema_json`, etc.)

    Raises:
        TabularError: ks não existe (404), arquivo inválido (400),
                      tamanho excedido (413), DuckDB falha (500).
    """
    # Sanity checks
    ks = await knowledge_repo.find_by_id(ks_id)
    if not ks:
        raise TabularError(f"knowledge_source '{ks_id}' não encontrada.", status_code=404)

    # Reusa análise para validar + capturar schema/score
    analysis = await analyze_tabular(data, filename)
    ext = analysis["ext"]
    schema = analysis["schema"]

    # Slug + versão
    base_name = name or os.path.splitext(os.path.basename(filename))[0] or "tabela"
    slug = slugify(base_name)
    version = await next_version_for_slug(ks_id, slug)
    urn = build_urn(ks_id, slug, version)

    # Caminhos
    table_id = str(uuid.uuid4())
    ks_dir = _TABULAR_ROOT / ks_id
    duckdb_path = ks_dir / f"{table_id}.duckdb"
    relative_path = str(duckdb_path).replace("\\", "/")

    duckdb = _import_duckdb()

    def _write() -> int:
        """Cria o .duckdb e ingere os dados. Retorna size_bytes do arquivo final."""
        ks_dir.mkdir(parents=True, exist_ok=True)
        # tempfile para alimentar read_csv_auto/read_xlsx
        with tempfile.NamedTemporaryFile(
            suffix=f".{ext}", delete=False
        ) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            con = duckdb.connect(str(duckdb_path))
            try:
                _read_into_duckdb(con, tmp_path, ext)
            finally:
                con.close()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return duckdb_path.stat().st_size

    try:
        size_bytes = await asyncio.to_thread(_write)
    except TabularError:
        raise
    except Exception as e:
        # Cleanup parcial
        try:
            duckdb_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise TabularError(f"Falha ao gravar DuckDB: {e}", status_code=500) from e

    # Persiste metadata em Postgres
    row = {
        "id": table_id,
        "knowledge_source_id": ks_id,
        "urn": urn,
        "name": base_name,
        "description": description,
        "schema_json": schema,  # asyncpg/JSONB aceita list[dict] diretamente
        "row_count": analysis["rows"],
        "column_count": analysis["columns"],
        "size_bytes": size_bytes,
        "duckdb_path": relative_path,
        "duckdb_table_name": _DUCKDB_TABLE,
        "version": str(version),
        "status": "ready",
        "quality_score": analysis["score"],
        "suggested_pk": analysis.get("suggested_pk"),
        "created_by": created_by or "",
    }
    try:
        await data_tables_repo.create(row)
    except Exception as e:
        # Rollback: apaga arquivo se metadata falhar (consistência)
        try:
            duckdb_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise TabularError(f"Falha ao registrar tabela: {e}", status_code=500) from e

    # Retorna versão enriquecida (com flags da KS) para a UI
    enriched = await find_by_id_with_ks(table_id)
    return enriched or row


# ─── 3. EXECUTE QUERY ────────────────────────────────────────────


def _quote_ident(name: str) -> str:
    """Escapa identificador (col/table) para DuckDB usando double-quote."""
    return '"' + (name or "").replace('"', '""') + '"'


def _validate_columns(requested: list[str], schema: list[dict]) -> None:
    """Garante que toda coluna solicitada existe no schema. Erro 400 se não."""
    valid = {c["name"] for c in schema}
    unknown = [c for c in requested if c not in valid]
    if unknown:
        raise TabularError(
            f"Coluna(s) inexistente(s): {', '.join(unknown)}. "
            f"Disponíveis: {', '.join(sorted(valid))}.",
            status_code=400,
        )


async def execute_query(
    table_id: str,
    inputs: Optional[dict] = None,
    select: Optional[list[str]] = None,
    filters: Optional[list[dict]] = None,
    order_by: Optional[list[str]] = None,
    limit: int = 100,
    executed_by: Optional[str] = None,
    interaction_id: Optional[str] = None,
    agent_id: str = "",
) -> dict:
    """Executa SELECT parametrizado em uma data_table.

    Args:
        table_id: id da data_table.
        inputs: dict de inputs do usuário (paridade com declarative_engine).
                Filtros com `if_present: X` são pulados se X não estiver em inputs.
        select: colunas a retornar. None = todas.
        filters: list[{col, op, value, if_present?}]. `op` é string do SqlOperator.
                 `value` pode ser literal OU template "{{ inputs.X }}" (resolvido aqui).
        order_by: list de "col" ou "col DESC". Validado contra schema.
        limit: máximo de linhas. Hard-cap = MAX_ROWS_RETURNED.
        executed_by, interaction_id, agent_id: contexto pra auditoria.

    Returns:
        {
          "rows": [{col: val, ...}, ...],
          "row_count": N,
          "columns": [col_names],
          "duration_ms": int,
          "sql_rendered": str,        # SQL com ? (sem bind values)
          "table": {id, urn, name},
        }

    Raises:
        TabularError: tabela não existe (404), coluna inválida (400),
                      execução falha (500), limit > cap (400).
    """
    inputs = inputs or {}
    filters = filters or []
    select = select or []
    order_by = order_by or []

    table = await find_by_id_with_ks(table_id)
    if not table:
        raise TabularError(f"data_table '{table_id}' não encontrada.", status_code=404)
    if table.get("status") != "ready":
        raise TabularError(
            f"Tabela '{table_id}' não está pronta (status={table.get('status')}).",
            status_code=409,
        )

    if limit > MAX_ROWS_RETURNED:
        raise TabularError(
            f"limit={limit} excede máximo {MAX_ROWS_RETURNED}.",
            status_code=400,
        )
    if limit < 1:
        limit = 1

    schema = table.get("schema_json") or []
    schema_col_names = [c["name"] for c in schema]

    # SELECT clause
    if select:
        _validate_columns(select, schema)
        select_sql = ", ".join(_quote_ident(c) for c in select)
        result_columns = list(select)
    else:
        select_sql = "*"
        result_columns = list(schema_col_names)

    # WHERE clause
    where_parts: list[str] = []
    bind_values: list[Any] = []

    for f in filters:
        col = f.get("col") or f.get("column")
        op_raw = f.get("op") or f.get("operator")
        if not col or not op_raw:
            raise TabularError(f"Filtro inválido (col + op obrigatórios): {f}", status_code=400)

        # if_present: skip filter se input não fornecido
        if_present_key = f.get("if_present")
        if if_present_key and inputs.get(if_present_key) in (None, ""):
            continue

        # Validar operador (defense in depth)
        try:
            op = SqlOperator(op_raw if isinstance(op_raw, str) else op_raw.value)
        except ValueError:
            raise TabularError(
                f"Operador '{op_raw}' não suportado. Aceitos: {[o.value for o in SqlOperator]}.",
                status_code=400,
            )

        # Validar coluna
        _validate_columns([col], schema)

        # Resolver value (suporta template simples "{{ inputs.X }}")
        raw_value = f.get("value")
        value = _resolve_template_value(raw_value, inputs)

        clause, binds = render_where_clause(_quote_ident(col), op, value)
        where_parts.append(clause)
        bind_values.extend(binds)

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # ORDER BY
    order_sql = ""
    if order_by:
        ord_parts = []
        for ob in order_by:
            tokens = str(ob).strip().split()
            ob_col = tokens[0]
            direction = tokens[1].upper() if len(tokens) > 1 else "ASC"
            if direction not in ("ASC", "DESC"):
                raise TabularError(f"Direção inválida em ORDER BY: {ob}", status_code=400)
            _validate_columns([ob_col], schema)
            ord_parts.append(f"{_quote_ident(ob_col)} {direction}")
        order_sql = " ORDER BY " + ", ".join(ord_parts)

    sql = (
        f"SELECT {select_sql} FROM {_DUCKDB_TABLE}"
        f"{where_sql}{order_sql} LIMIT ?"
    )
    bind_values_final = list(bind_values) + [limit]

    duckdb = _import_duckdb()
    duckdb_path = table.get("duckdb_path")
    if not duckdb_path:
        raise TabularError("duckdb_path ausente na tabela.", status_code=500)

    def _run_query() -> tuple[list[dict], int, list[str]]:
        # READ-ONLY: safety técnica. INSERT/UPDATE/DELETE/DROP rejeitados pelo engine.
        con = duckdb.connect(duckdb_path, read_only=True)
        try:
            cur = con.execute(sql, bind_values_final)
            rows = cur.fetchall()
            col_descr = cur.description or []
            col_names = [d[0] for d in col_descr]
            row_dicts = [dict(zip(col_names, r)) for r in rows]
            return row_dicts, len(row_dicts), col_names
        finally:
            con.close()

    t0 = time.perf_counter()
    status = "ok"
    error_msg: Optional[str] = None
    rows: list[dict] = []
    row_count = 0
    columns_out: list[str] = result_columns

    try:
        rows, row_count, columns_out = await asyncio.to_thread(_run_query)
    except TabularError:
        status = "error"
        raise
    except Exception as e:
        status = "error"
        error_msg = str(e)
        raise TabularError(f"Falha ao executar query: {e}", status_code=500) from e
    finally:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        # Audit (best-effort: falha não derruba a request)
        try:
            await data_table_query_logs_repo.create({
                "id": str(uuid.uuid4()),
                "data_table_id": table_id,
                "interaction_id": interaction_id,
                "agent_id": agent_id,
                "executed_by": executed_by or "",
                "sql_rendered": sql,
                "inputs_json": inputs,  # JSONB
                "row_count": row_count,
                "duration_ms": duration_ms,
                "status": status,
                "error_message": error_msg,
            })
        except Exception:
            logger.exception("Falha ao gravar audit log de query tabular")

    return {
        "rows": rows,
        "row_count": row_count,
        "columns": columns_out,
        "duration_ms": duration_ms,
        "sql_rendered": sql,
        "table": {
            "id": table.get("id"),
            "urn": table.get("urn"),
            "name": table.get("name"),
        },
    }


_TEMPLATE_RE = None  # lazy compile


def _resolve_template_value(value: Any, inputs: dict) -> Any:
    """Resolve template MUITO simples: "{{ inputs.foo }}" → inputs["foo"].

    Não usa Jinja2 para evitar overhead e superfície de injeção. O engine
    declarativo (declarative_engine) já usa Jinja2 com SandboxedEnvironment
    no caminho de produção; este resolver é apenas para o caminho de teste
    direto via UI Query Builder.
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s.startswith("{{") and s.endswith("}}"):
        expr = s[2:-2].strip()
        if expr.startswith("inputs."):
            key = expr[len("inputs."):].strip()
            return inputs.get(key)
    return value
