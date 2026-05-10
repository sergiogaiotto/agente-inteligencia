# Deploy em Hostinger VPS

Guia condensado para subir a plataforma `agente-inteligencia` num VPS
da Hostinger (ou qualquer Ubuntu 22.04 com Docker). Para a revisão
arquitetural completa, ver discussão em `.planning/` ou histórico de
PRs.

## 1. Provisão do VPS (Hostinger Panel)

- **Plano**: KVM 2 (mín 4GB RAM, 2 vCPU, 80GB SSD) para `minimal` profile.
  Para `full` (Tempo+Loki+Grafana), KVM 4 (8-16GB RAM).
- **SO**: Ubuntu 22.04 LTS.
- **Auth**: SSH key (não senha) — cole a chave pública na criação.
- **DNS**: aponte um A record `seu-dominio.com.br` → IP do VPS.
  Aguarde propagação (5-30min). Confirme: `dig seu-dominio.com.br`.

## 2. Setup do servidor (uma vez)

```bash
ssh root@<IP-VPS>

# Update + ferramentas básicas
apt update && apt upgrade -y
apt install -y ca-certificates curl gnupg ufw fail2ban git

# Docker (script oficial)
curl -fsSL https://get.docker.com | sh
systemctl enable docker

# Usuário não-root pra rodar a stack
adduser --disabled-password --gecos '' deploy
usermod -aG docker deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys

# Firewall (Docker bypassa, mas ainda vale ter regra explícita pra SSH)
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# Swap (KVM 2GB precisa pra build de markitdown[all] não morrer)
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# ⚠ CRÍTICO: paths persistentes pra dados sobreviverem a redeploy.
# Hostinger Deploy apaga volumes Docker no Redeploy automático — sem
# isso, você perde TUDO (banco, evidências, contas) toda vez.
mkdir -p /var/lib/agente-persist/{postgres,redis,qdrant,app}
chown -R 999:999 /var/lib/agente-persist/postgres   # uid postgres
chown -R 999:999 /var/lib/agente-persist/redis      # uid redis
chown -R 1000:1000 /var/lib/agente-persist/qdrant   # uid qdrant
chown -R 1000:1000 /var/lib/agente-persist/app      # uid app
```

Depois, no Hostinger Panel → Environment Variables (ou no `.env` montado),
adicione **as 4 linhas críticas** abaixo. Sem elas, o compose usa volumes
Docker nomeados (que serão apagados no próximo Deploy):

```
PG_DATA_DIR=/var/lib/agente-persist/postgres
REDIS_DATA_DIR=/var/lib/agente-persist/redis
QDRANT_DATA_DIR=/var/lib/agente-persist/qdrant
APP_DATA_DIR=/var/lib/agente-persist/app
```

## 3. Clone + .env de produção

```bash
su - deploy
git clone https://github.com/sergiogaiotto/agente-inteligencia.git
cd agente-inteligencia

cp .env.example .env
chmod 600 .env
nano .env
```

Ajustes mínimos no `.env` (gere senhas com `openssl rand -hex 32`):

```env
APP_ENV=production
APP_DEBUG=false
SECRET_KEY=<openssl rand -hex 32>

POSTGRES_PASSWORD=<openssl rand -hex 24>
DATABASE_URL=postgresql://agente:<MESMA_SENHA>@postgres:5432/agente_inteligencia

# TLS produção (Caddy + Let's Encrypt)
TLS_HTTP_PORT_HOST=80
TLS_HTTPS_PORT_HOST=443
TLS_SITE=seu-dominio.com.br
CADDY_GLOBAL=email seu-email@dominio.com   # remover 'auto_https off'

# Verifier em produção (sample async, sem bloquear resposta)
VERIFIER_V2_ENABLED=true
VERIFIER_PRODUCTION_ASYNC=true
VERIFIER_PRODUCTION_SAMPLE_RATE=0.10

# Policy engine failsafe-closed em prod
OPA_ENABLED=true
OPA_FAILSAFE_OPEN=false

# Suas API keys reais
AZURE_OPENAI_API_KEY=<sua-key>
AZURE_OPENAI_ENDPOINT=https://seu-recurso.openai.azure.com/
MARITACA_API_KEY=<sua-key>
```

## 4. Subir

```bash
docker compose up -d --build           # minimal (recomendado pra começar)
# OU
docker compose --profile full up -d --build   # com observabilidade

docker compose ps
docker compose logs -f app             # acompanhe primeiro boot
```

Caddy obtém cert Let's Encrypt automaticamente em ~30s. Confirme:

```bash
curl -fsS http://localhost:7000/api/health    # via host loopback
curl -fsS https://seu-dominio.com.br/api/health    # via Caddy/TLS público
```

## 5. Day-2 ops

### Update da aplicação

```bash
cd ~/agente-inteligencia
git pull
docker compose up -d --build app
```

### Backup (cron diário)

```bash
crontab -e
# adicionar:
0 3 * * * cd /home/deploy/agente-inteligencia && bash infra/scripts/backup.sh >> /var/log/agente-backup.log 2>&1
```

Backups vão pra `./backups/agente-backup-YYYY-MM-DD-HHMM.tar.gz`.
Retenção 30 dias por default. Inclui pg_dump + Qdrant + uploads + Caddy
certs. Para mover off-site: rclone/aws-cli pra S3/Wasabi.

### Logs

```bash
docker compose logs --tail=200 -f app
docker compose logs caddy | grep -i "certificate\|acme"   # debug TLS
```

Profile=full: logs centralizados via Loki em Grafana (porta 3000 em
loopback — `ssh -L 3000:127.0.0.1:3000 deploy@<VPS>` pra acessar).

## 6. Hardening checklist (já aplicado no compose)

| Item | Status |
|------|--------|
| App bind em loopback (`127.0.0.1:7000`) | ✓ Caddy é a única porta pública |
| Postgres não exposto na host | ✓ |
| Redis não exposto na host | ✓ |
| Qdrant em loopback (debug only) | ✓ |
| OPA em loopback | ✓ |
| Grafana em loopback (profile full) | ✓ |
| Healthchecks em todos serviços | ✓ |
| Logs com rotação (10MB×3) | ✓ |
| Memory limits (caddy 128m, opa 256m) | ✓ |
| Volumes nomeados (sobrevivem `down` sem `-v`) | ✓ |
| Caddy auto-HTTPS (Let's Encrypt) | ✓ quando `TLS_SITE` for domínio |
| HSTS sticky | ⚠ comentado — descomente em `infra/caddy/Caddyfile` após validar 1-2 semanas |
| Backup automatizado | ✓ via `infra/scripts/backup.sh` + cron |

## 7. Troubleshooting comum

### Caddy não obtém certificado

```bash
docker compose logs caddy | grep -i "error\|acme"
```

Causas frequentes:
- DNS ainda não propagou (`dig seu-dominio.com.br` deve retornar IP do VPS).
- Porta 80 não acessível da internet (Hostinger Panel → Firewall, e UFW).
- `auto_https off` ainda no `CADDY_GLOBAL` (modo dev — não emite cert).

### Build do Docker falha (markitdown)

`markitdown[all]` puxa libs pesadas. Se VPS tem só 2GB RAM e sem swap,
`pip wheel` pode morrer. Soluções:
- Adicionar swap (passo 2 do setup).
- OU trocar `markitdown[all]` por `markitdown[pdf,docx,pptx,xlsx]` no
  `requirements.txt` (cobre 90% dos casos sem ffmpeg/audio).

### App reinicia em loop

```bash
docker compose logs app
```

Causas comuns: `DATABASE_URL` apontando pra senha errada (compare com
`POSTGRES_PASSWORD`), API keys placeholder no `.env`, OPA offline com
`OPA_FAILSAFE_OPEN=false`.

### "Ports are not available" ao subir Caddy

Algum serviço do host (nginx/Apache nativo) usando 80/443. Pare:
```bash
systemctl stop nginx apache2 2>/dev/null || true
systemctl disable nginx apache2 2>/dev/null || true
```

## 8. Acesso a interfaces internas (debug)

Tudo loopback. Pra acessar da sua máquina local:

```bash
# Grafana (profile=full)
ssh -L 3000:127.0.0.1:3000 deploy@<VPS>
# depois: http://localhost:3000 no browser local

# App direto (sem Caddy)
ssh -L 7000:127.0.0.1:7000 deploy@<VPS>
# depois: http://localhost:7000

# Qdrant dashboard
ssh -L 6333:127.0.0.1:6333 deploy@<VPS>
# http://localhost:6333/dashboard

# OPA
ssh -L 8181:127.0.0.1:8181 deploy@<VPS>
```
