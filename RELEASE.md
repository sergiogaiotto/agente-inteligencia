# Release v2.1.0 — Ondas 2 / 3 / 4 (Observabilidade, RAG real, Governança, TLS)

**Tag:** `v2.1.0` · **Base:** `v2.0.0` · **Compatibilidade:** 100% backward compat

---

## 🎯 Resumo executivo

7 commits, ~3.000 linhas adicionadas, ~600 removidas, 7 novos containers
disponíveis (5 default + 4 no profile `full`). Esta release fecha o
roadmap original do projeto:

| | Onda | Estado |
|---|---|---|
| 0–1 | Base + Segurança | ✅ (releases anteriores) |
| **2** | **Observabilidade self-hosted** (OTel + Tempo + Loki + Grafana) | ✅ |
| **3** | **RAG real** (Qdrant + embeddings + híbrido BM25+vetorial) | ✅ |
| **4a** | **Policy as Code** (OPA + 3 policies Rego + PEP) | ✅ |
| **4b** | **AI Gateway** (LiteLLM + 7 modelos + fallback) | ✅ |
| **4c.1** | **TLS público** (Caddy reverse proxy + Let's Encrypt) | ✅ |
| **4c.4** | **Secrets management** (script de leak scan + doc) | ✅ |
| 4c.2 | Postgres TLS | ⏳ futuro (gatilho: cloud-managed) |
| 4c.3 | Helm chart | ⏳ futuro (gatilho: migração k8s) |

**Plus:** purga 100% do SQLite legacy. PostgreSQL 16 + asyncpg é o backend
único e fonte de verdade.

---

## 🌊 Onda 2 — Observabilidade self-hosted

Tracing distribuído via OpenTelemetry → Tempo (traces) + Loki (logs) +
Grafana (UI), em paralelo ao LangFuse já existente.

**Como ativar:**
```bash
# .env: OTEL_ENABLED=true
docker compose --profile full up -d
# Grafana: http://localhost:3000 (admin/admin)
```

**Auto-instrumented:** FastAPI, asyncpg, httpx, redis, logging
(com `trace_id`/`span_id` injetados em todo log record).

**Spans manuais nos pontos críticos:**
- `fsm.transition:<from>-><to>` em cada transição da máquina de estados
- `evidence.retrieve.bm25` + `evidence.retrieve.vector` no Retriever
- `evidence.rerank` no Reranker
- `ingest.text` + `ingest.embed` + `ingest.qdrant_upsert` na ingestão
- `policy.evaluate` em cada decisão OPA

**Dashboard provisionado:** "AgenteInteligência → FSM & Logs" com correlação
trace ↔ log via derived fields (clica num log com `trace_id=abc...` e abre
o trace no Tempo).

**Graceful degradation:** se OTel exporter offline, app continua rodando
(BatchSpanProcessor descarta spans em buffer overflow, sem propagar erro).

📂 Commit: `7cc4f67` · 🔗 Detalhes: `infra/README.md §8`

---

## 🌊 Onda 3 — RAG real (Qdrant + embeddings)

Substitui o retriever textual ingênuo (match em metadados) por busca
**híbrida real**: BM25 nativo do Postgres + vetorial via Qdrant, fundidos
por Reciprocal Rank Fusion.

**Pipeline de ingestão:**
- Chunker token-based (`tiktoken cl100k_base`, 500 tokens / 50 overlap)
- Embedder via Azure OpenAI `text-embedding-3-small` (1536 dims, cosine)
- Persistência: chunks no Postgres (com `tsvector` gerado para BM25) +
  pontos no Qdrant collection `agente_evidence`

**Endpoints REST:**
- `POST /api/v1/knowledge-sources/{id}/ingest` — ingere texto cru
- `GET  /api/v1/knowledge-sources/{id}/chunks` — lista chunks
- `DELETE /api/v1/knowledge-sources/{id}/chunks` — limpa source
- `GET  /api/v1/rag/health` — diagnóstico Qdrant

**Reranker LLM** (opcional, toggle `RAG_RERANK_WITH_LLM`): pós-RRF, top
candidatos passam por GPT-4o que reordena com justificativa. +500ms,
+~$0.0005/query, qualidade superior.

**Graceful degradation:** Qdrant offline → BM25-only. Source sem chunks
ingeridos → cai no retriever legacy (busca textual em metadados). App
nunca quebra por causa do RAG.

📂 Commit: `c0797d7` · 🔗 Detalhes: `infra/README.md §9`

---

## 🌊 Onda 4a — Policy as Code (OPA)

Open Policy Agent integrado como PEP/PDP. Substitui o stub legacy de
PolicyCheck (que decidia apenas via prompt_guard) por motor de políticas
Rego versionadas.

**3 políticas piloto** (`infra/opa/policies/`):
- `interaction.rego` — gate do PolicyCheck (prompt_injection, rate_limit,
  user status)
- `tool_invocation.rego` — gate de tools por sensitivity × user.role ×
  trusted_context
- `evidence.rego` — clearance vs confidentiality (definida; PEP wirado em
  iteração futura quando `users.clearance` existir)

**Failsafe configurável:**
- `OPA_FAILSAFE_OPEN=true` (dev): OPA offline → allow=true + warning + audit
- `OPA_FAILSAFE_OPEN=false` (prod com dados sensíveis): OPA offline → deny

**Audit trail:** cada decisão (allow ou deny) → linha em `audit_log` com
`entity_type='policy_decision'`, `details=json{package, rule, input,
decision}`. Crítico para compliance.

**Toggle:** `OPA_ENABLED=false` por default (opt-in).

📂 Commit: `061120f` · 🔗 Detalhes: `infra/README.md §11`

---

## 🌊 Onda 4b — AI Gateway (LiteLLM)

Proxy OpenAI-compatible único entre app e providers. Centraliza
rate-limit, fallback automático, logging unificado e cost tracking.

**7 modelos roteados** (`infra/litellm/config.yaml`):
- Azure: `azure/gpt-4o`, `azure/text-embedding-3-small`
- OpenAI: `openai/gpt-4o`, `openai/gpt-4.1`
- Maritaca: `maritaca/sabia-3`, `maritaca/sabia-4`
- Ollama: `ollama/llama3.1`

**Fallback automático:** Azure GPT-4o cai → tenta OpenAI GPT-4o (configurado
em `litellm_settings.fallbacks` no yaml).

**Observabilidade nativa:** LangFuse callback ativo no gateway — cada call
LLM vira span observável (sem código adicional).

**Defesa em profundidade Python:** se gateway 5xx ou unreachable e
`LLM_GATEWAY_FALLBACK_TO_DIRECT=true`, providers caem em upstream direto
antes de propagar erro. Validado E2E parando o container do `litellm` —
app continuou respondendo via Azure direto.

**Toggle:** `LLM_GATEWAY_ENABLED=false` por default (opt-in).

📂 Commit: `ee07918` · 🔗 Detalhes: `infra/README.md §10`

---

## 🌊 Onda 4c — TLS público + Secrets management

### 4c.1 — Caddy reverse proxy

Em paralelo à porta `:7000` do app (mantida para back-compat). Caddy
adiciona HTTPS automático em produção (Let's Encrypt nativo), headers
de segurança baseline, compressão e logs JSON estruturados.

**Modo dev (default):** HTTP only em `:80`.
**Modo prod:** `TLS_SITE=meudominio.com` + `CADDY_GLOBAL=email admin@...` →
Caddy obtém cert Let's Encrypt automaticamente.

**Headers de segurança:** X-Content-Type-Options, X-Frame-Options,
Referrer-Policy, Permissions-Policy. HSTS deixado comentado (sticky no
browser — operador ativa após validar HTTPS em prod).

**Portas parametrizáveis:** `TLS_HTTP_PORT_HOST` / `TLS_HTTPS_PORT_HOST`
para contornar conflito com IIS/Skype no Windows.

### 4c.4 — Secrets management

Script `infra/scripts/check-secrets-leak.sh` (bash puro, sem deps) que
escaneia padrões high-confidence:
- `sk-proj-...` (OpenAI), `sk-ant-...` (Anthropic), `sk-litellm-...`
- `pk-lf-...` / `sk-lf-...` (LangFuse)
- `ghp_/gho_/ghu_...` (GitHub), `xoxb-...` (Slack), `AKIA[A-Z0-9]{16}` (AWS)

**Modos:** `tracked` (arquivos versionados) e `--staged` (uso em pre-commit).

**Doc completa em `infra/README.md §13`:** rotação de chaves por provedor,
caminhos de evolução (Docker Secrets → Sealed Secrets → External Secrets
Operator).

📂 Commit: `92d0dcc`

---

## 🧹 Bonus: Purga 100% do SQLite

Limpeza completa de toda infraestrutura legacy de SQLite:

- Removido `app/core/db_migrate.py` (216 linhas — script one-shot)
- Removido `data/agente_inteligencia.db` (untracked + apagado do disco)
- Removido `aiosqlite` do `requirements.txt`
- Removida camada de compat (`_qmark_to_dollar`, `_ConnCompat`,
  `_CursorCompat`, `get_db()`) em `app/core/database.py` — código morto
- Atualizadas docs (`README.md`, `infra/README.md`) — zero menções a SQLite

**Resultado:** `+50 / −372 linhas` (líquido **−322 linhas**), Postgres é
backend único confirmado por 8 validações E2E.

📂 Commit: `5cc6969`

---

## 🛡️ Pendência crítica de segurança

Durante a Onda 4c, o script de leak scan detectou que o banco SQLite
legacy commitado historicamente continha **chaves LangFuse** (`pk-lf-...`,
`sk-lf-...`). O arquivo foi **untracked e removido do disco** nesta release,
mas o **blob continua acessível no histórico do git** (`git log -- data/agente_inteligencia.db`).

**Ação obrigatória do operador (depois desta release):**

1. **Rotacionar chaves LangFuse imediatamente** em `cloud.langfuse.com →
   Settings → API Keys`. Atualizar `.env` e `docker compose up -d
   --force-recreate app litellm`.

2. **(Opcional, destrutivo) limpar histórico** com:
   ```bash
   pip install git-filter-repo
   git filter-repo --path data/agente_inteligencia.db --invert-paths --force
   git push --force origin main
   ```
   Reescreve commits passados — coordene com co-autores. **Mesmo após
   isso, a rotação de chaves continua obrigatória** (clones antigos têm
   o blob).

📚 Detalhes em `infra/README.md §13.5`.

---

## 🔄 Como atualizar

```bash
cd /caminho/para/agente-inteligencia
git pull origin main

# Rebuild da imagem do app (deps novas: opentelemetry-*, qdrant-client,
# tiktoken; aiosqlite removido)
docker compose build app

# Stack default (5 containers: app, postgres, redis, qdrant, opa, litellm,
# caddy — mais que antes!)
docker compose up -d

# Stack completa (default + 4 de observabilidade)
docker compose --profile full up -d
```

**Toggles para ativar gradualmente** (todos default `false`):
- `OTEL_ENABLED=true` — exporta traces para Tempo (Onda 2)
- `LLM_GATEWAY_ENABLED=true` — providers via LiteLLM (Onda 4b)
- `OPA_ENABLED=true` — PolicyCheck e tool gate via OPA (Onda 4a)

Cada toggle é reversível em 2 comandos: trocar valor no `.env` +
`docker compose up -d --force-recreate app`.

---

## 📊 Estado do dashboard (`/`)

Seção "Módulos Implementados" agora mostra **14 entries** (10 da
especificação canônica + 4 ondas):

| Seção | Label |
|---|---|
| §4 | Topologia AOBD→AR→SA |
| §5 | Parser SKILL.md Canônico |
| §6 | CAR — Catálogo Roteadores |
| §7 | Protocolo A2A / Envelope |
| §9.5 | Harness Avaliação |
| §14 | Evidence Runtime + **RAG real** |
| §15 | FSM Interação (9 estados) |
| §16 | Modelo de Dados (**27 tabelas PostgreSQL**) |
| §17 | **Observabilidade self-hosted** |
| §18 | Version Registry / Drift |
| **Onda 1** | Segurança fundacional (OWASP LLM Top 10) |
| **Onda 4a** | Policy as Code (OPA) |
| **Onda 4b** | AI Gateway (LiteLLM) |
| **Onda 4c** | TLS + Secrets management |

Cada entry tem tooltip técnico (não promo) com a implementação real.

---

## 🗂️ Stack final

11 containers operacionais:

| Container | Porta | Onda |
|---|---|---|
| `agente_app` | 7000 | Base |
| `agente_postgres` | 5432 (interno) | Base |
| `agente_redis` | 6379 (interno) | Base |
| `agente_qdrant` | 127.0.0.1:6333 | Base + Onda 3 |
| `agente_litellm` | 127.0.0.1:4000 | Onda 4b |
| `agente_opa` | 127.0.0.1:8181 | Onda 4a |
| `agente_caddy` | 80, 443 (parametrizável) | Onda 4c.1 |
| `agente_tempo` | interno (OTLP 4317) | Onda 2 (profile=full) |
| `agente_loki` | interno (3100) | Onda 2 (profile=full) |
| `agente_promtail` | — | Onda 2 (profile=full) |
| `agente_grafana` | 3000 | Onda 2 (profile=full) |

---

## 📝 Commits incluídos

```
28e96b4  docs(dashboard): atualiza Módulos Implementados com as 4 Ondas entregues
5cc6969  chore: purga 100% do SQLite — Postgres como backend único
92d0dcc  Onda 4c (combo B): Caddy reverse proxy + secrets management
061120f  Onda 4a: Policy as Code com OPA (3 policies + PEP em PolicyCheck e tools)
ee07918  Onda 4b: AI Gateway com LiteLLM (proxy + routing + fallback automático)
c0797d7  Onda 3: RAG real (Qdrant + embeddings + busca híbrida BM25+vetorial)
7cc4f67  Onda 2: observabilidade self-hosted (OTel + Tempo + Loki + Grafana)
```

---

## 🚧 Não entregue nesta release (escopo escudado)

Decisões conscientes de "não fazer" — documentadas como "futuro com gatilho":

- **mTLS interno** entre containers — teatro em single-host Docker. Valor
  real aparece com service mesh (Istio/Linkerd) em k8s.
- **Helm chart validado** — sem cluster k8s real (Windows + Docker Desktop)
  o chart vira YAML não-testado. Quando migrar para k8s, criar como Onda 4c.3.
- **Postgres TLS** — sem cloud-managed Postgres, ganho marginal. Onda 4c.2
  quando o gatilho aparecer.
- **`users.clearance` + filtro de evidence por confidentiality** — política
  Rego `evidence.rego` está pronta; aguarda coluna no schema.

---

🤖 Co-authored with [Claude](https://claude.com/claude-code) (Opus 4.7, 1M context).
