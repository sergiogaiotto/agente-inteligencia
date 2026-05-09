# Deploy — VPS Hostinger (Docker)

Guia passo a passo para subir o **agente-inteligencia** numa VPS da Hostinger
(ou qualquer Linux com Docker). Cobre Onda 0 (PostgreSQL + Azure OpenAI +
Redis + Qdrant). As Ondas seguintes adicionam serviços ao mesmo
`docker-compose.yml` via *profile* `full`.

## 1. Pré-requisitos na VPS

Recomendado: **Ubuntu 22.04 / 24.04**, **4 GB RAM**, **2 vCPU**, **40 GB SSD**.
Se for usar a stack `full` na Onda 2 (Tempo+Loki+Grafana), preveja **8 GB RAM**.

```bash
# 1.1 — atualizações + Docker oficial
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# 1.2 — swap (recomendado para VPS pequenas)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 1.3 — firewall
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

## 2. Clonar e configurar

```bash
git clone <seu-fork>/agente-inteligencia.git
cd agente-inteligencia

cp .env.example .env

# Gere chaves seguras
openssl rand -hex 32                 # → SECRET_KEY
openssl rand -hex 24                 # → POSTGRES_PASSWORD

# Edite .env preenchendo:
#   SECRET_KEY                         (chave gerada acima)
#   POSTGRES_PASSWORD                  (senha gerada acima)
#   DATABASE_URL                       (use a mesma senha)
#   AZURE_OPENAI_API_KEY               (Azure portal → Keys)
#   AZURE_OPENAI_ENDPOINT              (Azure portal → Endpoint)
#   AZURE_OPENAI_CHAT_DEPLOYMENT       (nome do deployment no Azure)
nano .env
```

> ⚠️ **Importante**: nunca comite `.env`. O `.gitignore` já o ignora.
> Se compartilhar a chave Azure por engano, **rotacione** no portal.

## 3. Subir a stack mínima

```bash
docker compose pull          # imagens externas (postgres, redis, qdrant)
docker compose build app     # imagem do app
docker compose up -d
docker compose ps            # confirme que tudo está healthy
```

Acesse `http://<seu-ip-vps>:7000/api/health` — deve responder `{"status":"ok"}`.

## 4. Migrar dados de SQLite (opcional — só se já tinha o app rodando)

Se o app já rodou antes em SQLite, copie o arquivo `data/agente_inteligencia.db`
para a VPS e execute o script de migração:

```bash
# de dentro do container (recomendado — usa as mesmas envs)
docker compose exec app python -m app.core.db_migrate

# ou apontando para um caminho específico
docker compose exec app python -m app.core.db_migrate /app/data/legacy.db
```

A saída lista, por tabela, quantas linhas foram lidas e inseridas. O script
é idempotente — pode rodar várias vezes sem duplicar.

## 5. TLS público (Caddy reverse-proxy — recomendado)

Para HTTPS automático com Let's Encrypt, suba um Caddy ao lado:

```yaml
# salvar como infra/caddy/docker-compose.yml e rodar `docker compose up -d`
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    networks: [agente_aimesh]
networks:
  agente_aimesh:
    external: true
volumes:
  caddy_data:
  caddy_config:
```

`Caddyfile`:

```
seu-dominio.com {
    reverse_proxy app:7000
    encode zstd gzip
    log {
        output stdout
        format json
    }
}
```

## 6. Backup do PostgreSQL

```bash
# manual
docker compose exec postgres pg_dump -U agente agente_inteligencia | gzip > backup-$(date +%F).sql.gz

# crontab — diário às 03:00
0 3 * * * cd /home/usuario/agente-inteligencia && \
    docker compose exec -T postgres pg_dump -U agente agente_inteligencia | \
    gzip > backups/db-$(date +\%F).sql.gz
```

## 7. Comandos úteis

```bash
docker compose logs -f app             # logs do app
docker compose logs -f postgres        # logs do banco
docker compose exec postgres psql -U agente agente_inteligencia    # shell SQL
docker compose exec redis redis-cli    # shell redis
docker compose restart app             # reiniciar só o app
docker compose down                    # parar tudo (volumes preservados)
docker compose down -v                 # parar e APAGAR volumes — cuidado!
```

## 8. Observabilidade self-hosted (Onda 2)

A Onda 2 adiciona **Tempo** (traces), **Loki** (logs) e **Grafana** (UI) ao
mesmo `docker-compose.yml`, isolados no profile `full`. Sem o profile, nada
muda — `docker compose up -d` continua subindo as 4 imagens da Onda 0.

### 8.1. Habilitar a instrumentação no app

Edite `.env`:

```
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317
LOKI_ENDPOINT=http://loki:3100
```

Se `OTEL_ENABLED=false` (default), o app não tenta conectar e o stack pode
ficar parado sem afetar nada.

### 8.2. Subir o stack completo

```bash
docker compose --profile full up -d
docker compose --profile full ps
```

Recursos extras necessários: **+2.5 GB RAM, +1 GB disco**.

| Serviço   | Imagem                  | Função                            | Mem |
|-----------|-------------------------|-----------------------------------|-----|
| `tempo`   | `grafana/tempo:2.6.0`   | Recebe spans OTLP, indexa traces  | 1G  |
| `loki`    | `grafana/loki:3.2.0`    | Agrega logs                       | 1G  |
| `promtail`| `grafana/promtail:3.2.0`| Coleta logs do Docker → Loki      | 256M|
| `grafana` | `grafana/grafana:11.3.0`| UI de exploração e dashboards     | 512M|

### 8.3. Acessar Grafana

```
http://<seu-host>:3000
usuário: admin   senha: admin   (definido em GRAFANA_ADMIN_PASSWORD)
```

> ⚠️ Em produção, troque `GRAFANA_ADMIN_PASSWORD` no `.env` antes de subir.

### 8.4. Inspecionar uma interação

1. Mande uma mensagem no workspace (`/workspace`)
2. Em Grafana → **Explore** → datasource **Tempo**
3. **Search** → service: `agente-inteligencia` → escolha o trace mais recente
4. A árvore mostra: `POST /api/v1/workspace/...` → `fsm.transition:Intake->PolicyCheck` → `evidence.retrieve` → ...
5. Clique num span → **Logs for this span** pula direto pro Loki filtrado pelo `trace_id`

O dashboard inicial **AgenteInteligência → FSM & Logs** já vem provisionado.

### 8.5. Plano B: logs do Docker Desktop (Windows)

Em Docker Desktop no Windows, o bind mount de `/var/lib/docker/containers`
às vezes não funciona porque o socket é virtualizado. Sintoma: Promtail roda
mas nenhum log chega ao Loki.

**Workaround**: usar o logging driver `loki` direto no daemon Docker:

1. Instalar plugin: `docker plugin install grafana/loki-docker-driver:latest --alias loki --grant-all-permissions`
2. No `daemon.json` do Docker Desktop (Settings → Docker Engine):
   ```json
   {
     "log-driver": "loki",
     "log-opts": {
       "loki-url": "http://localhost:3100/loki/api/v1/push",
       "loki-batch-size": "400"
     }
   }
   ```
3. Restart Docker Desktop. Promtail pode ser desativado (`docker compose --profile full stop promtail`).

### 8.6. Desligar / limpar

```bash
docker compose --profile full down                    # mantém traces/logs
docker compose --profile full down -v                 # APAGA volumes (tempo_data, loki_data, grafana_data)
docker compose --profile full stop grafana tempo loki promtail   # só pausa
```

---

## 9. RAG real com Qdrant (Onda 3)

Substitui a busca textual ingênua original (match em metadados) por busca
**híbrida BM25 + vetorial** com fusão por Reciprocal Rank Fusion (RRF) e
reranker LLM opcional.

### 9.1. Componentes

| Camada | Implementação |
|---|---|
| Embeddings | Azure OpenAI `text-embedding-3-small` (1536 dims) |
| Chunker | `tiktoken cl100k_base`, 500 tokens/50 overlap |
| BM25 | Postgres `tsvector` + GIN index, `plainto_tsquery('simple', ...)` |
| Vetorial | Qdrant collection `agente_evidence`, distância cosine |
| Fusão | RRF com k=60 |
| Reranker | LLM (Azure GPT-4o) com fallback heurístico |

### 9.2. Toggles principais (`.env`)

```bash
RAG_V2_ENABLED=true               # false: cai no retriever legacy (metadados)
RAG_CHUNK_SIZE_TOKENS=500
RAG_CHUNK_OVERLAP_TOKENS=50
RAG_TOP_N_VECTOR=20               # candidatos da perna vetorial
RAG_TOP_N_BM25=20                 # candidatos da perna BM25
RAG_RRF_K=60
RAG_RERANK_WITH_LLM=true          # false: usa heurística (sem custo, menos preciso)
```

### 9.3. Fluxo de ingestão

```bash
# 1. Cria a knowledge_source (se ainda não existe)
SRC=$(curl -s -X POST http://localhost:7000/api/v1/knowledge-sources \
  -H "Content-Type: application/json" \
  -d '{"name":"FAQ Produtos","description":"FAQ oficial","source_type":"text","authorized":1}' \
  | jq -r .id)

# 2. Ingere texto (chunca + embeda + grava)
curl -X POST "http://localhost:7000/api/v1/knowledge-sources/$SRC/ingest" \
  -H "Content-Type: application/json" \
  -d '{"text": "Aqui vai o conteúdo cru do documento. Pode ter parágrafos. ..."}'
# → {"chunks_created": N, "tokens_total": N, "qdrant_upserted": N, "duration_ms": N, "partial": false}

# 3. Verifica
curl "http://localhost:7000/api/v1/knowledge-sources/$SRC/chunks?limit=10"
curl http://localhost:7000/api/v1/rag/health
# → {"qdrant_collection":{"points_count":N,"status":"green"},"rag_available":true}

# 4. Re-ingerir (apaga chunks/pontos anteriores e refaz)
curl -X POST "http://localhost:7000/api/v1/knowledge-sources/$SRC/ingest" \
  -d '{"text":"...novo conteúdo...","replace":true}'

# 5. Limpar só os chunks (mantém a knowledge_source)
curl -X DELETE "http://localhost:7000/api/v1/knowledge-sources/$SRC/chunks"
```

### 9.4. Custo aproximado de embedding

Com `text-embedding-3-small` ($0.02/M tokens):
- Documento de **10 páginas** (~5000 tokens, ~10 chunks): **$0.0001**
- Documento de **100 páginas** (~50000 tokens, ~100 chunks): **$0.001**
- Re-ingestão de 100 docs grandes: ~**$0.10**

### 9.5. Graceful degradation

| Cenário | Comportamento |
|---|---|
| `RAG_V2_ENABLED=false` | Retriever cai no legacy (busca em metadados de knowledge_sources) |
| Nenhuma source com chunks ainda | Retriever cai no legacy automaticamente |
| Qdrant offline durante ingestão | Postgres recebe chunks; resposta marca `partial=true`; usuário re-roda ingest quando Qdrant voltar |
| Qdrant offline durante search | Cai em BM25-only no Postgres (vetorial vazio na fusão) |
| Azure embeddings offline | Ingest falha com 503 explícito; search vetorial cai vazia, BM25 segue |
| LLM-reranker falha | Fallback automático para heurística de overlap |

### 9.6. Observabilidade

Spans emitidos durante busca (visíveis em Grafana → Tempo):

```
evidence.retrieve  (parent)
├── evidence.retrieve.bm25       (atributo bm25.hits)
├── evidence.retrieve.vector     (atributo vector.hits)
└── evidence.rerank              (atributo rerank.path: llm | heuristic | llm_failed_fallback)
```

Durante ingestão:

```
ingest.text  (atributos: source.id, chunks.count, chunks.tokens_total)
├── ingest.embed
├── ingest.delete_old            (só quando replace=true)
└── ingest.qdrant_upsert         (atributo qdrant.upserted)
```

### 9.7. Escopo escudado nesta Onda (limites conhecidos)

- **Só texto puro**: `application/json` com campo `text`. PDF, URL, docx ficam para Onda 3.5.
- **1 collection global** (`agente_evidence`) com filtro por `knowledge_source_id`. Multi-tenant via collections separadas é evolução.
- **Sem UI**: API only. Upload via `curl`/Postman/script.
- **Re-ingestão é total**: `replace=true` apaga tudo da source e refaz. Sem diff incremental.

---

## 10. AI Gateway com LiteLLM (Onda 4b)

Centraliza **todas** as chamadas LLM da aplicação num proxy único:
- Rate-limit nativo por modelo
- Fallback automático Azure→OpenAI
- Logging+custo unificado (LangFuse callback)
- Adicionar novo provider sem redeploy do app (só edita yaml + restart container)

### 10.1. Componentes

| | Imagem / Localização |
|---|---|
| Gateway | `ghcr.io/berriai/litellm:main-stable` |
| Configuração | `infra/litellm/config.yaml` (versionado) |
| Master key | env var `LLM_GATEWAY_MASTER_KEY` no `.env` (NUNCA commitada) |
| Porta | `127.0.0.1:4000` (loopback, debug local) |
| Provider routing | prefixo no model_name: `azure/gpt-4o`, `openai/gpt-4o`, `maritaca/sabia-3`, `ollama/llama3.1` |

### 10.2. Ativar

```bash
# 1. Gerar master key (uma vez)
echo "LLM_GATEWAY_MASTER_KEY=sk-litellm-$(openssl rand -hex 24)" >> .env

# 2. Subir o gateway (sempre, mesmo desligado)
docker compose up -d litellm

# 3. Ligar no app via .env
echo "LLM_GATEWAY_ENABLED=true" >> .env

# 4. Restart do app para pegar a flag
docker compose up -d --force-recreate app

# 5. Verificar
curl -H "Authorization: Bearer $LLM_GATEWAY_MASTER_KEY" http://localhost:4000/v1/models | jq
```

### 10.3. Smoke test direto no gateway

```bash
MK=$(grep ^LLM_GATEWAY_MASTER_KEY .env | cut -d= -f2)
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $MK" -H "Content-Type: application/json" \
  -d '{"model":"azure/gpt-4o","messages":[{"role":"user","content":"diga ok"}],"max_tokens":5}'
```

### 10.4. Adicionar novo provider/modelo

Edite `infra/litellm/config.yaml`, adicione um item em `model_list`:

```yaml
- model_name: anthropic/claude-3-5-sonnet
  litellm_params:
    model: anthropic/claude-3-5-sonnet-20241022
    api_key: os.environ/ANTHROPIC_API_KEY
```

Adicione a env var no compose (passthrough), restart do container:

```bash
docker compose restart litellm
```

App não precisa redeploy — basta o agent ter `llm_provider="anthropic"` no banco
(e existir uma classe `AnthropicProvider` que monta `model="anthropic/<name>"`).

### 10.5. Defesa em profundidade

- `LLM_GATEWAY_FALLBACK_TO_DIRECT=true` (default): se gateway 5xx ou unreachable,
  o provider tenta upstream direto antes de propagar erro. Mais resilência, +1
  retry de latência no failure path.
- `LLM_GATEWAY_FALLBACK_TO_DIRECT=false`: falha rápida, mais determinístico.
  Útil em produção depois que o gateway está provado estável.

### 10.6. Observabilidade

Quando ligado, cada chamada LLM aparece em **dois** lugares:
- **LangFuse**: callback nativo do gateway (basta `LANGFUSE_PUBLIC_KEY` no .env)
- **LangChain wraps** continuam emitindo trace pro nível superior (chain inteira)

São níveis complementares: LangFuse via gateway = visão por LLM call; LangChain
wrap = visão por chain/agent. Ambos úteis.

### 10.7. Reverter

```bash
# Desligar uso pelo app (mantém gateway rodando)
sed -i 's/^LLM_GATEWAY_ENABLED=true/LLM_GATEWAY_ENABLED=false/' .env
docker compose up -d --force-recreate app

# Ou desligar gateway por completo
docker compose stop litellm
```

App volta a chamar upstream direto sem perda de funcionalidade.

---

## 11. Próximas ondas

- **Onda 1** ✅ segurança (rate-limit, PII redaction, secrets cifrados, prompt guard)
- **Onda 2** ✅ observabilidade (OTel + Tempo + Loki + Grafana) — seção 8
- **Onda 3** ✅ RAG real (Qdrant + embeddings + híbrido BM25+vetorial) — seção 9
- **Onda 4b** ✅ AI Gateway (LiteLLM) — seção 10
- **Onda 4a** ⏳ Policy as Code (OPA + Rego policies + PEP em Python)
- **Onda 4c** ⏳ mTLS + Helm chart (deploy k8s production-grade)
