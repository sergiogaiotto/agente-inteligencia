# Logs estruturados — guia operacional

## Visão geral

A app grava logs JSON estruturados (1 evento por linha = JSONL) em `logs/`
e no stdout. Loki coleta via Promtail e indexa; Grafana consome em tempo
real via dashboards provisionados.

## Pasta `logs/`

| Arquivo | Conteúdo | Retenção |
|---|---|---|
| `app.log` | Geral da aplicação (info+, exclui handlers específicos) | 14 dias |
| `tabular.log` | Eventos da Onda Tabular (analyze/promote/append/query) | 30 dias |
| `api.log` | Request/response HTTP com latency | 14 dias |
| `audit.log` | Writes em DB (auditoria de mudanças) | 90 dias |
| `errors.log` | Apenas `ERROR` e `CRITICAL` (escalation rápida) | 30 dias |

Rotação: `TimedRotatingFileHandler` diário (00:00 UTC); arquivos antigos
viram `app.log.2026-05-23`. UTC ajuda quando vários servidores escrevem.

## Schema do JSON

Cada linha tem:

```json
{
  "ts": "2026-05-25T10:30:00.123Z",
  "level": "INFO",
  "logger": "tabular.promote",
  "msg": "promote_completed",
  "request_id": "req_a1b2c3",
  "trace_id": "cli_xyz",
  "user_id": "u-root",
  "event": "tabular.promote.completed",
  "table_id": "t-abc",
  "rows": 1234,
  "duration_ms": 542
}
```

Campos sempre presentes: `ts`, `level`, `logger`, `msg`.
Campos opcionais (contexto): `request_id`, `trace_id`, `user_id`.
Campos do evento (extras do `logger.X(msg, extra={...})`): qualquer key
do extra vira top-level no JSON.

**PII redaction**: dicts aninhados têm chaves sensíveis (password,
token, api_key, secret, authorization, etc) substituídas por
`***REDACTED***` antes da serialização.

## Catálogo de eventos canônicos (Onda Tabular)

Definidos em [`app/data_tables/events.py`](../../app/data_tables/events.py).
Use o nome do evento em queries LogQL para filtrar de forma estável.

| Evento | Logger | Quando dispara |
|---|---|---|
| `tabular.upload.received` | tabular | POST /ingest-file ou /promote-to-table |
| `tabular.kb_mode.rejected_upload` | tabular | upload bloqueado por kb_mode incompatível |
| `tabular.analyze.started` | tabular | início de analyze_tabular |
| `tabular.analyze.completed` | tabular | analyze ok |
| `tabular.analyze.failed` | tabular | analyze deu erro |
| `tabular.promote.started` | tabular | início de promote_to_table |
| `tabular.promote.completed` | tabular | tabela criada |
| `tabular.promote.failed` | tabular | promote deu erro |
| `tabular.append.started` | tabular | início de append |
| `tabular.append.completed` | tabular | append ok |
| `tabular.append.failed` | tabular | append deu erro |
| `tabular.query.executed` | tabular | query SELECT executou (ok ou error) |
| `tabular.duckdb.error` | tabular | falha low-level do DuckDB |

Eventos HTTP genéricos (qualquer rota):

| Evento | Logger | Quando |
|---|---|---|
| `http.request` | app.api | DEBUG, request entrou |
| `http.response` | app.api | INFO/WARN/ERROR (por status), request terminou |
| `http.exception` | app.api | exceção não tratada |

## Labels indexados pelo Promtail (Onda Observabilidade)

O Promtail (`infra/promtail/promtail-config.yaml`) parseia o JSON e extrai como **labels indexados** os seguintes campos (baixa cardinality, performáticos pra filtrar):

| Label | Fonte | Exemplo |
|---|---|---|
| `level` | `level` do JSON | `INFO`, `ERROR` |
| `logger` | `logger` do JSON | `tabular.promote`, `app.api` |
| `event` | `event` do JSON | `tabular.promote.completed` |
| `trace_id` | `trace_id` do JSON | `cli_abc12345` |
| `request_id` | `request_id` do JSON | `req_a1b2c3d4e5f6` |
| `container_name` | Docker (sempre) | `agente_app` |
| `compose_service` | Docker (sempre) | `app` |

Campos NÃO-label (user_id, table_id, duration_ms, rows, etc) ficam acessíveis via `| json | campo="x"` no LogQL. Use labels pra filtros frequentes; use `| json` pra campos high-cardinality ou agregações com `unwrap`.

## Queries LogQL prontas (Grafana)

### Eventos da Onda Tabular por minuto, por tipo

```logql
sum by (event) (
  count_over_time({container_name=~".*agente.*"} | json | logger=~"tabular.*" [1m])
)
```

### Latência p95 das queries (5min)

```logql
quantile_over_time(0.95,
  ({container_name=~".*agente.*"} | json | event="tabular.query.executed"
   | unwrap duration_ms)[5m]
)
```

### Tabelas criadas nas últimas 24h

```logql
sum(count_over_time(
  {container_name=~".*agente.*"} | json | event="tabular.promote.completed" [24h]
))
```

### Falhas por tipo nos últimos 5min

```logql
sum by (error_class) (
  count_over_time({container_name=~".*agente.*"} | json
                   | event=~"tabular.*\\.failed" [5m])
)
```

### Eventos de um request específico (label-indexed, RÁPIDO)

```logql
{container_name=~".*agente.*", request_id="req_abc123"}
```

### Eventos de um usuário (no client) — label trace_id

```logql
{container_name=~".*agente.*", trace_id="cli_xyz789"}
```

### Todos os ERRORs (label level)

```logql
{container_name=~".*agente.*", level="ERROR"}
```

### Eventos de um logger específico (label logger)

```logql
{container_name=~".*agente.*", logger=~"tabular\\..*"}
```

### Logs de uma KS específica

```logql
{container_name=~".*agente.*"} | json | ks_id="ks-1"
```

### Top usuários por queries hoje

```logql
topk(10,
  sum by (user_id) (
    count_over_time({container_name=~".*agente.*"} | json
                    | event="tabular.query.executed" [24h])
  )
)
```

## Correlação end-to-end

| ID | Origem | Propagação |
|---|---|---|
| `request_id` (req_xxx) | Servidor (gerado ou aceito de header) | Em TODOS os logs do request; echo no header X-Request-Id da response |
| `trace_id` (cli_xxx) | Frontend JS (`_genClientTraceId`) | Header X-Client-Trace-Id; logs do request; pode cobrir N requests (1 ação do user) |
| `user_id` | Cookie/header durante middleware | Em todos os logs do request |

**Fluxo**: user clica → JS gera `cli_X`, envia em todos os fetches da ação
→ backend recebe, gera `req_Y` por request, propaga em logs → modal de
erro mostra `cli_X` + `req_Y` copiáveis → suporte busca esses IDs no
Grafana e vê toda a trilha em segundos.

## ENV vars

| Var | Default | Descrição |
|---|---|---|
| `LOG_DIR` | `logs` | Pasta dos arquivos `.log` |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `LOG_FORMAT` | `json` (prod) / `text` (test) | JSON estruturado ou texto legível |
| `LOG_FILE_ENABLED` | `1` (prod) / `0` (test) | Liga handlers de arquivo |
| `LOG_CONSOLE_ENABLED` | `1` | Liga handler de stdout |

## Como instrumentar nova feature

```python
import logging
logger = logging.getLogger("minha_feature")

logger.info(
    "operacao_completada",  # mensagem humana
    extra={
        "event": "minha_feature.operacao.completed",  # nome canônico
        "entity_id": "x-123",
        "duration_ms": 42,
        # qualquer key vira top-level no JSON
    },
)
```

Use prefixo padrão `<feature>.<acao>.<outcome>` no `event`. Mantém
queries LogQL estáveis quando refatorar o código.
