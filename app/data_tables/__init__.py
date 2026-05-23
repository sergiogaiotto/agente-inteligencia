"""Onda Tabular — promoção de CSV/XLSX a tabela consultável.

Estrutura:
- queries.py: helpers especializados (visibility, URN, list_for_user)
- types.py: enums e tipos compartilhados (operadores SQL, status)

Service de ingestão/query fica em `app/evidence/tabular.py` (perto do
restante do pipeline de evidence/RAG, com o qual coexiste).

Tabelas Postgres: `data_tables` (metadata) + `data_table_query_logs` (audit).
Dados ficam em DuckDB embarcado: `data/tabular/<ks_id>/<table_id>.duckdb`.
"""
