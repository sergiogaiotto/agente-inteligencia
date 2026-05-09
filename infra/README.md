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

## 5. TLS público (Caddy)

Caddy agora é parte do `docker-compose.yml` (Onda 4c.1) — basta `docker compose
up -d caddy` e configurar `.env`. Documentação completa de uso (modos dev/prod,
Let's Encrypt, parametrização de portas) está em **§12** abaixo.

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

## 11. Policy as Code com OPA (Onda 4a)

Versionar políticas de autorização em arquivos Rego. PolicyCheck do FSM e
gate de invocação de tools consultam o **Open Policy Agent** antes de
permitir. Cada decisão é auditada em `audit_log`.

### 11.1. Componentes

| | Imagem / Localização |
|---|---|
| Engine | `openpolicyagent/opa:1.0.0` em modo `--server` |
| Políticas | `infra/opa/policies/*.rego` (versionadas) |
| Endpoint | `http://opa:8181` interno; `http://localhost:8181` para curl direto |
| Cliente Python | `app/core/opa_client.py` (httpx async + audit + spans OTel) |

### 11.2. Políticas piloto

| Pacote | O que decide | Schema input |
|---|---|---|
| `interaction` | Allow/deny do PolicyCheck do FSM. Bloqueia em prompt_injection ≥ 0.7, rate_limit, user inativo | `{prompt_injection, rate_limit, user}` |
| `tool_invocation` | Allow/deny por tool baseado em sensitivity × user.role × trusted_context | `{tool, user, context}` |
| `evidence` | Allow/deny acesso a evidência por confidentiality vs user.clearance | `{user, evidence}` (NÃO wirado nesta Onda — depende de `users.clearance`) |

### 11.3. Ativar

```bash
# 1. OPA já sobe junto com a stack minimal
docker compose up -d opa
curl http://localhost:8181/v1/policies | jq '.result | length'   # deve ser 3

# 2. Ligar no app via .env
echo "OPA_ENABLED=true" >> .env
docker compose up -d --force-recreate app

# 3. Verificar que decisões estão saindo (após algumas interações):
docker exec agente_postgres psql -U agente agente_inteligencia -c \
  "SELECT entity_id, action, count(*) FROM audit_log WHERE entity_type='policy_decision' GROUP BY 1,2"
```

### 11.4. Smoke test direto no OPA

```bash
# Allow caminho-claro
curl -X POST http://localhost:8181/v1/data/interaction/allow \
  -H "Content-Type: application/json" \
  -d '{"input":{"prompt_injection":{"score":0.1},"rate_limit":{"exceeded":false},"user":{"status":"active"}}}'
# → {"result":true}

# Deny por prompt_injection alto
curl -X POST http://localhost:8181/v1/data/interaction/allow \
  -H "Content-Type: application/json" \
  -d '{"input":{"prompt_injection":{"score":0.9},"rate_limit":{"exceeded":false},"user":{"status":"active"}}}'
# → {"result":false}

# Listar reasons
curl -X POST http://localhost:8181/v1/data/interaction/reasons \
  -H "Content-Type: application/json" \
  -d '{"input":{"prompt_injection":{"score":0.9},"user":{"status":"inactive"}}}'
# → {"result":["prompt_injection_blocked","user_inactive"]}
```

### 11.5. Adicionar nova política

1. Criar `infra/opa/policies/nome.rego` (pacote `nome`, regra `allow`)
2. Restart container: `docker compose restart opa`
3. Verificar carga: `curl http://localhost:8181/v1/policies | jq '.result[].id'`
4. PEP em Python: `await opa_client.evaluate("nome", "allow", {...})`

App não precisa redeploy — políticas são dados, não código do app.

### 11.6. Failsafe (decisão crítica de operação)

| `OPA_FAILSAFE_OPEN` | OPA up | OPA down |
|---|---|---|
| `true` (default dev) | decisão real | **allow=true** + warning + audit |
| `false` (prod c/ dados sensíveis) | decisão real | **allow=false** (negação por padrão) |

Trade-off explícito: disponibilidade × segurança. Em dev = open. Em prod com
PII/financeiro/saúde = closed (nega quando OPA não responder, evita bypass).

### 11.7. Observabilidade

- **Spans OTel**: `policy.evaluate` com `policy.package`, `policy.rule`, `policy.allow`, `policy.duration_ms`, `policy.source` (opa/disabled/failsafe_open/failsafe_closed). Visíveis no Grafana → Tempo.
- **Audit trail**: cada decisão (toggle ON, audit=true) → linha em `audit_log` com `entity_type='policy_decision'`, `action='allow'|'deny'`, `details=json{package,rule,input,decision}`.

### 11.8. Reverter

```bash
sed -i 's/^OPA_ENABLED=true/OPA_ENABLED=false/' .env
docker compose up -d --force-recreate app
# OPA continua rodando ocioso. `docker compose stop opa` para parar.
```

---

## 12. TLS público com Caddy (Onda 4c.1)

Reverse proxy entre o app e o mundo externo. Em paralelo à porta `:7000`
do app (que continua aberta para back-compat). Caddy adiciona:
- HTTPS automático em produção (Let's Encrypt nativo, sem cert-manager separado)
- Headers de segurança baseline (X-Content-Type-Options, X-Frame-Options, etc.)
- Compressão gzip/zstd
- Logs JSON estruturados

### 12.1. Modo dev (default)

```bash
# Porta 80 frequentemente reservada no Windows (IIS/Skype). Use 8080/8443:
TLS_HTTP_PORT_HOST=8080 TLS_HTTPS_PORT_HOST=8443 docker compose up -d caddy

# Ou no .env:
echo "TLS_HTTP_PORT_HOST=8080" >> .env
echo "TLS_HTTPS_PORT_HOST=8443" >> .env
docker compose up -d caddy

# Smoke test:
curl http://localhost:8080/api/health
```

Modo dev usa `CADDY_GLOBAL=auto_https off` — HTTP only, sem cert.

### 12.2. Modo produção (HTTPS automático)

Pré-requisitos:
1. Domain real (ex: `agente.minhaempresa.com`) com DNS A/AAAA apontando para o IP do host
2. Portas 80 e 443 acessíveis da internet (firewall, NAT)
3. Email válido para Let's Encrypt (ele envia avisos de expiração)

`.env`:
```
TLS_HTTP_PORT_HOST=80
TLS_HTTPS_PORT_HOST=443
TLS_SITE=agente.minhaempresa.com
CADDY_GLOBAL=email admin@minhaempresa.com
```

```bash
docker compose up -d caddy
docker compose logs -f caddy   # acompanhar Let's Encrypt obtendo cert
```

Renovação automática: Caddy renova ~30 dias antes da expiração. Volume nomeado
`caddy_data` persiste o cert entre `docker compose down` (sem `-v`).

### 12.3. Reverter / parar

```bash
docker compose stop caddy   # mantém volumes
docker compose rm -f caddy  # remove container, mantém volumes
docker compose down -v      # apaga volumes (inclui certs! cuidado em prod)
```

App continua funcionando direto em `:7000` mesmo com Caddy parado.

---

## 13. Secrets management (Onda 4c.4)

### 13.1. Estado atual

Secrets ficam em `.env` na raiz, em texto puro. `.gitignore` impede commit
do `.env` real (mas não de `.env.example`, que é template sem chaves).

**Riscos reais (em ordem de probabilidade):**
1. Vazamento via `docker logs` ou `docker inspect` (env vars aparecem)
2. Backup do host inclui `.env` em cleartext
3. Screenshot/screen-share durante demo
4. Histórico do shell (`history | grep KEY`)
5. **Já-leakado**: chaves anteriormente commitadas em arquivos rastreados (script `check-secrets-leak.sh` detecta)

### 13.2. Scan de leak (script pronto)

```bash
# Escaneia arquivos rastreados pelo git
./infra/scripts/check-secrets-leak.sh

# Apenas arquivos staged (use no pre-commit):
./infra/scripts/check-secrets-leak.sh --staged
```

Detecta padrões com prefixo distintivo: OpenAI (`sk-proj-`, `sk-ant-`),
LiteLLM (`sk-litellm-`), LangFuse (`pk-lf-`, `sk-lf-`), GitHub, Slack, AWS.

**Não detecta** (intencionalmente — falsos positivos demais): senhas Postgres
genéricas, chaves Azure (string aleatória sem prefixo). Para essas, faça
`grep -i "azure_openai_api_key" $(git ls-files)` manualmente.

### 13.3. Rotação de chaves (quando vazar)

Procedimento padrão por provedor:

| Provedor | Onde rotacionar |
|---|---|
| Azure OpenAI | Portal Azure → recurso OpenAI → Keys and Endpoint → "Regenerate Key 1/2" |
| OpenAI público | platform.openai.com → API Keys → revogar antiga + criar nova |
| Maritaca | console Maritaca → Settings → API Keys |
| LangFuse | cloud.langfuse.com → Settings → API Keys → revoke |
| LiteLLM master | gerar nova: `openssl rand -hex 24`, atualizar `.env`, restart `litellm` |
| Postgres | `ALTER USER agente PASSWORD '...'` + atualizar `DATABASE_URL` + recreate |

Após rotacionar, atualize `.env` e:
```bash
docker compose up -d --force-recreate app litellm
```

### 13.4. Caminhos de evolução

Ranqueados por esforço × ganho:

1. **Pre-commit hook** com `check-secrets-leak.sh --staged` (~5min, instalar com `pre-commit` ou husky)
2. **Docker Secrets** — secrets viram arquivos read-only em `/run/secrets/`, não env vars. Sobrevivem a `docker inspect`. Esforço: ~1h, requer reescrever lugares que leem env.
3. **Sealed Secrets** (k8s, Bitnami) — encripta secret antes de commitar; cluster decrypta. Versionável no git. Requer k8s.
4. **External Secrets Operator** (k8s) — sincroniza secrets do Vault/AWS Secrets Manager/Azure Key Vault. Padrão de produção em empresas. Requer k8s + vault.

Para single-host produção (VPS), a combinação prática hoje é:
- `.env` fora do repo, montado read-only no container
- Backup do `.env` em password manager corporativo (1Password, Bitwarden)
- Rotação a cada 90 dias (calendário no time)
- `check-secrets-leak.sh --staged` no pre-commit

### 13.5. ⚠️ Achado de leak histórico

O script detecta `pk-lf-...` e `sk-lf-...` em `data/agente_inteligencia.db`
(banco SQLite legacy commitado no início do projeto). Ações requeridas:

1. **Rotacionar chaves LangFuse** já — assumir que vazaram para qualquer um que clonou o repo
2. Remover o `.db` do tracking: `git rm --cached data/agente_inteligencia.db`
3. **Limpar histórico** com `git filter-repo` se quiser apagar do passado (operação destrutiva — coordene com co-autores)
4. Garantir que `data/*.db` continua no `.gitignore` (já está, só pegar arquivos novos)

---

## 14. Próximas ondas

- **Onda 1** ✅ segurança (rate-limit, PII redaction, secrets cifrados, prompt guard)
- **Onda 2** ✅ observabilidade (OTel + Tempo + Loki + Grafana) — seção 8
- **Onda 3** ✅ RAG real (Qdrant + embeddings + híbrido BM25+vetorial) — seção 9
- **Onda 4b** ✅ AI Gateway (LiteLLM) — seção 10
- **Onda 4a** ✅ Policy as Code (OPA + 3 policies + PEP) — seção 11
- **Onda 4c.1** ✅ TLS público com Caddy — seção 12
- **Onda 4c.4** ✅ Secrets management (script + doc) — seção 13
- **Onda 4c.2** ⏳ Postgres TLS (futuro — quando cloud-managed)
- **Onda 4c.3** ⏳ Helm chart (futuro — quando migrar para k8s)
