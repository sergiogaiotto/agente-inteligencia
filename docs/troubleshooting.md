# Troubleshooting — catálogo `sintoma → query`

Guia operacional pra investigar problemas usando os **logs estruturados JSON**
da plataforma. Cada evento tem campo `event=...` consultável via LogQL no
Grafana/Loki **ou** via `jq` filtrando o `docker compose logs`.

Quando reportar um problema, **prefira mandar saída de uma das queries abaixo**
em vez de grep cego — o filtro por `event=` é determinístico e captura
metadata estruturada (qdrant_url, source_id, error_type, traceback completo).

---

## Como rodar as queries

### Via Docker logs + jq (dev / VPS sem Grafana)

```bash
# Filtrar últimas N linhas por evento exato
docker compose logs app --tail=2000 | jq 'select(.event == "qdrant.upsert.failed")'

# Filtrar família de eventos (regex)
docker compose logs app --tail=2000 | jq 'select(.event | startswith("qdrant."))'

# Só erros (level >= WARNING)
docker compose logs app --tail=5000 | jq 'select(.level == "WARNING" or .level == "ERROR")'

# Combinar: erros qdrant das últimas 10 min
docker compose logs app --since=10m | jq 'select(.event | startswith("qdrant.") and .level == "ERROR")'
```

### Via Grafana/Loki (prod com observabilidade ativa)

```logql
# Todos eventos qdrant em erro
{job="app"} | json | event=~"qdrant\\..*failed"

# Retries do Verifier que falharam
{job="app"} | json | event=~"verifier\\.contract\\.retry.*"

# Drift de dim detectado (qualquer backend)
{job="app"} | json | event=~".*dim_mismatch"

# Correlação por request_id (todos logs de uma request HTTP específica)
{job="app"} | json | request_id="req_c28d236df1c6"
```

---

## Catálogo `sintoma → query`

### Vector store (Qdrant / pgvector)

| Sintoma | Evento(s) | Causa típica |
|---|---|---|
| Banner UI *"Vetores divergentes"* após ingestão | `qdrant.upsert.failed`, `qdrant.upsert.aborted_no_collection`, `pgvector.upsert.failed`, `evidence.ingest.partial` | Qdrant offline, dim de embedding mudou, collection com dim errada |
| Skill nova ingerida não aparece na busca | `evidence.ingest.completed` (confere `vector_upserted == chunks_created`), `qdrant.search.failed`, `pgvector.search.failed` | Vetor não foi gravado, ou busca falhou silencioso |
| Drift de dim (trocou embedder no Settings) | `qdrant.collection.dim_mismatch`, `pgvector.column.dim_mismatch` | Provider trocou sem rodar `/api/v1/evidence/reindex` |
| Qdrant offline / rede caiu | `qdrant.client.init_failed`, `qdrant.collection.ensure_failed` | Container down, network split, timeout |
| Coluna pgvector não pôde ser criada | `pgvector.column.create_failed`, `pgvector.codec.register_failed` | Postgres sem extensão `vector` (imagem errada — precisa `pgvector/pgvector:pg16`) |
| Reindex global travou no meio | `evidence.reindex.batch_embed_failed`, `evidence.reindex.batch_embed_short`, `evidence.reindex.aborted` | Embedder offline, dim retornada ≠ esperada |
| Re-ingest pra source não limpa antigos | `qdrant.delete.failed`, `pgvector.delete.failed` | Backend down — chunks novos coexistem com antigos |

**Query operacional:**

```bash
# "Qual é o erro real do meu Qdrant?"
docker compose logs app --since=15m | jq 'select(.event | startswith("qdrant.") and .level == "WARNING" or .level == "ERROR") | {ts, event, error_type, qdrant_url, source_ids}'
```

---

### Verifier (retry on contract failure)

| Sintoma | Evento(s) | Causa típica |
|---|---|---|
| Resposta do agent vem com formato errado | `verifier.contract.retry_initiated`, `verifier.contract.retry_failed_final` | LLM ignorou Output Contract; retry também falhou |
| Latência de skill subiu | `verifier.contract.retry_initiated` (frequente) | Skills violando contrato em alta freq — investigar prompt ou contract |
| Retry está custando muito $$ | Contar `verifier.contract.retry_initiated` por hora | Ver % de retries; se > 20% considerar desligar `VERIFIER_CONTRACT_RETRY_ENABLED` ou melhorar prompt |

**Query operacional:**

```bash
# "Quantos retries de contract estão acontecendo?"
docker compose logs app --since=1h | jq -r 'select(.event == "verifier.contract.retry_initiated") | .first_attempt_errors[0]' | sort | uniq -c | sort -rn

# "Retry corrigiu ou continuou falhando?"
docker compose logs app --since=1h | jq 'select(.event | test("verifier\\.contract\\.retry_(succeeded|failed_final)")) | {event, original_errors, retry_errors}'
```

---

### Wizard de Skills

| Sintoma | Evento(s) | Causa típica |
|---|---|---|
| Wizard demora muito ou time out | `wizard.llm.resolved` (ver provider/model usado) | Resolver caiu em provider lento, ou modelo errado pra task |
| Wizard gera skill sem RAG/Tools mesmo selecionado | `wizard.lookup_*_failed` | Postgres deu erro no lookup dos IDs (raro); LLM ignorou prompt enriquecido |
| Operador trocou Roteamento LLM e wizard usa modelo antigo | `wizard.llm.resolved.source` | Cache de settings; restart do app |

**Query operacional:**

```bash
# "Qual modelo o Wizard usou hoje em cada chamada?"
docker compose logs app --since=24h | jq 'select(.event == "wizard.llm.resolved") | {ts, wizard_route, task_type, provider, model, source}'
```

---

### Ingestão de evidências

| Sintoma | Evento(s) | Causa típica |
|---|---|---|
| Ingestão "parcial" persistente | `evidence.ingest.partial` (com `rag_vector_backend`, `vector_upserted`, `chunks_expected`) | Backend de vetor offline / dim mismatch — ver `hint` no log |
| Performance da ingestão piorou | `evidence.ingest.completed` (campo `duration_ms`) | Comparar p95 ao longo do tempo |

**Query operacional:**

```bash
# "Distribuição de duration_ms das ingestões hoje"
docker compose logs app --since=24h | jq -r 'select(.event == "evidence.ingest.completed") | .duration_ms' | sort -n | awk 'BEGIN{c=0}{a[c]=$1;c++}END{print "p50:", a[int(c*0.5)], "p95:", a[int(c*0.95)], "max:", a[c-1]}'
```

---

### HTTP requests

| Sintoma | Evento(s) | Causa típica |
|---|---|---|
| 500 em alguma rota — qual? | `http.exception` (com path, status, exception block) | Stack trace completo no log |
| Latência alta em endpoint X | `http.response` (campo `duration_ms`) | Filtrar por path + comparar p95 |
| Burst de requests duvidoso | `http.request` por user_id | Possível scraping ou bot |

**Query operacional:**

```bash
# "Mostrar TODOS os logs de uma request específica" (correlação por request_id)
docker compose logs app --since=1h | jq 'select(.request_id == "req_c28d236df1c6")'
```

---

## Convenção pra adicionar novos eventos

Quando você (ou eu) for adicionar `logger.warning(..., extra={"event": "x.y.z", ...})` num PR:

### 1. Nome do evento

Formato: `<dominio>.<componente>.<acao_ou_estado>`

- `dominio`: módulo lógico (`qdrant`, `pgvector`, `wizard`, `verifier`, `evidence`, `http`, `engine`)
- `componente`: sub-componente (`upsert`, `collection`, `contract`, `ingest`, `reindex`)
- `acao_ou_estado`: o que aconteceu (`failed`, `succeeded`, `aborted_no_collection`, `dim_mismatch`)

Exemplos bons:
- `qdrant.collection.dim_mismatch` ✓
- `verifier.contract.retry_succeeded` ✓
- `evidence.ingest.partial` ✓

Exemplos ruins:
- `error` ✗ (sem domínio)
- `qdrant_failed` ✗ (sem componente; underscore no lugar errado)
- `Upsert.Failed` ✗ (case inconsistente)

### 2. Extras estruturados

Toda metadata útil pro troubleshooter (URL do serviço, IDs, contagens, tipos de erro) entra em `extra={...}`, não no `msg` cru. Exemplo:

```python
logger.warning(
    "qdrant upsert falhou — chunks ficaram só no Postgres",
    extra={
        "event": "qdrant.upsert.failed",
        "qdrant_url": settings.qdrant_url,
        "collection": settings.qdrant_collection,
        "chunk_count": len(chunks),
        "source_ids": list({c["source_id"] for c in chunks}),
        "error_type": type(e).__name__,
    },
    exc_info=True,  # ← stack trace vai pro JSON como exception.traceback
)
```

`JsonFormatter` (`app/core/logging_setup.py`) promove cada chave de `extra` pra top-level no JSON — `jq '.qdrant_url'` funciona direto. **PII (api_key, password, token) é redactado automaticamente** se a chave bater com `_SENSITIVE_KEYS`.

### 3. Teste com `caplog`

**Convenção pós este PR**: cada PR que adiciona/modifica `event=...` vem com pelo menos 1 teste asserindo o evento + extras críticos. Garante que se alguém quebrar o `event=` por typo (`qdrant.upsert.failed` → `qdrant.upsert.fail`), o teste pega.

Template:

```python
def test_emits_qdrant_upsert_failed_event(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="app.evidence.qdrant_store"):
        # ... rodar código que dispara o log ...
        await upsert_chunks_with_broken_provider()

    rec = next(
        (r for r in caplog.records if getattr(r, "event", "") == "qdrant.upsert.failed"),
        None,
    )
    assert rec is not None, "evento qdrant.upsert.failed não foi emitido"
    assert rec.qdrant_url == "http://expected"
    assert rec.error_type == "ConnectTimeout"
    assert rec.chunk_count == 3
```

### 4. Adicionar ao catálogo aqui

Atualizar a tabela `sintoma → query` apropriada deste arquivo com o novo evento, pra que o próximo plantonista ache rápido.

---

## Anti-padrões

- **NÃO** usar `print()` ou `logger.info(f"x={x}")` pra debug. Sai do JSON formatter, perde estrutura, não dá pra filtrar.
- **NÃO** repetir o `event` no `msg` (`logger.warning("qdrant.upsert.failed: ...", extra={"event": "qdrant.upsert.failed"})`). Mensagem livre é pro humano lendo direto; `event` é pro filtro.
- **NÃO** colocar PII em `msg` ou `extra` (api_key, senha, token literal). Redactor pega chaves óbvias mas não previne se você logar `f"key was {api_key}"` no msg.
- **NÃO** logar dentro de loops apertados sem rate-limit. 1 erro = 1 log; não 1 erro = 10000 logs.

---

## Onde a infraestrutura vive

- **JsonFormatter + handlers**: `app/core/logging_setup.py`
- **Context vars (request_id, trace_id, user_id)**: injetados pelo `RequestContextMiddleware`
- **PII redaction**: `_SENSITIVE_KEYS` em `app/core/logging_setup.py`
- **Loki + Grafana (prod)**: configurado em `infra/promtail/` + `infra/grafana/` (ver `docs/observability/LOGS.md`)
