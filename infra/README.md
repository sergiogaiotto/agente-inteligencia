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

## 9. Próximas ondas

- **Onda 1** ✅ segurança (rate-limit, PII redaction, secrets cifrados, prompt guard)
- **Onda 2** ✅ observabilidade (OTel + Tempo + Loki + Grafana) — *este documento*
- **Onda 3** ⏳ Vector DB com embeddings reais (Qdrant já está no compose)
- **Onda 4** ⏳ Policy as Code (OPA), AI Gateway, mTLS, Helm chart
