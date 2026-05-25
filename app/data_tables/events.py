"""Catálogo de eventos canônicos da Onda Tabular.

Cada evento tem um nome estável + schema documentado, usado em logs JSON
estruturados (`logger.info(msg, extra={"event": EVENT_NAME, ...})`).

Por que um catálogo formal:
- Loki/Grafana queries ficam estáveis (`| json | event="tabular.promote.completed"`)
- Dashboards não quebram quando refatoramos código
- Troubleshooting fica padronizado (você pesquisa pelo evento, não pelo grep)
- Documentação serve de contrato com o time de Observabilidade

Padrão do nome: `tabular.<acao>.<outcome>` ou `tabular.<recurso>.<verbo>`.
"""
from __future__ import annotations

# ─── Catálogo (nomes canônicos) ──────────────────────────────────

# Upload + análise
EVT_UPLOAD_RECEIVED = "tabular.upload.received"
"""Upload de arquivo aceito pelo endpoint. Campos:
  filename, ext, size_bytes, kb_mode, ks_id, ks_name
"""

EVT_KB_MODE_REJECTED = "tabular.kb_mode.rejected_upload"
"""Upload bloqueado por incompatibilidade com kb_mode. Campos:
  ks_id, kb_mode, filename, ext, reason
"""

EVT_ANALYZE_STARTED = "tabular.analyze.started"
"""Início de analyze_tabular. Campos:
  ks_id, filename, ext, size_bytes
"""

EVT_ANALYZE_COMPLETED = "tabular.analyze.completed"
"""analyze_tabular retorna sucesso. Campos:
  ks_id, filename, ext, sheet_count, primary_sheet, top_score,
  any_ready, duration_ms, has_auto_detect
"""

EVT_ANALYZE_FAILED = "tabular.analyze.failed"
"""analyze_tabular falhou (TabularError ou Exception). Campos:
  ks_id, filename, error_class, error_msg, status_code, duration_ms
"""

# Promote (criação de tabela)
EVT_PROMOTE_STARTED = "tabular.promote.started"
"""Início de promote_to_table. Campos:
  ks_id, filename, sheet_name, header_row
"""

EVT_PROMOTE_COMPLETED = "tabular.promote.completed"
"""Tabela criada com sucesso. Campos:
  ks_id, table_id, urn, name, sheet_name, rows, columns,
  size_bytes, quality_score, suggested_pk, duration_ms
"""

EVT_PROMOTE_FAILED = "tabular.promote.failed"
"""promote_to_table falhou. Campos:
  ks_id, filename, sheet_name, error_class, error_msg, status_code, duration_ms
"""

# Append (incremento)
EVT_APPEND_STARTED = "tabular.append.started"
"""Início de append_to_table. Campos:
  table_id, filename, sheet_name
"""

EVT_APPEND_COMPLETED = "tabular.append.completed"
"""Append OK. Campos:
  table_id, rows_added, row_count_before, row_count_after, duration_ms
"""

EVT_APPEND_FAILED = "tabular.append.failed"
"""append_to_table falhou. Campos:
  table_id, filename, error_class, error_msg, status_code, duration_ms
"""

# Query
EVT_QUERY_EXECUTED = "tabular.query.executed"
"""execute_query retornou. Campos:
  table_id, table_urn, operators_used (list), select_count, has_template,
  row_count, duration_ms, status (ok|error)
"""

# DuckDB engine (low-level)
EVT_DUCKDB_ERROR = "tabular.duckdb.error"
"""Qualquer falha vinda do DuckDB (read_csv_auto, INSERT, etc). Campos:
  operation (read|write|describe|query), sql_snippet (max 200 chars),
  error_class, error_msg
"""

# Lista completa para validação / docs
ALL_EVENTS = [
    EVT_UPLOAD_RECEIVED,
    EVT_KB_MODE_REJECTED,
    EVT_ANALYZE_STARTED,
    EVT_ANALYZE_COMPLETED,
    EVT_ANALYZE_FAILED,
    EVT_PROMOTE_STARTED,
    EVT_PROMOTE_COMPLETED,
    EVT_PROMOTE_FAILED,
    EVT_APPEND_STARTED,
    EVT_APPEND_COMPLETED,
    EVT_APPEND_FAILED,
    EVT_QUERY_EXECUTED,
    EVT_DUCKDB_ERROR,
]
