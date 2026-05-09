#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# Backup da plataforma agente-inteligencia
#
# Captura num único timestamped tarball:
#   - pg_dump completo (Postgres: agentes, skills, interações, evidências,
#     verifications, eval_runs, knowledge_sources, evidence_chunks, audit,
#     platform_settings — TUDO).
#   - Volumes binários: Qdrant (vetores), app_data (uploads do usuário),
#     caddy_data (certificados Let's Encrypt — re-emissão é rate-limited).
#
# Uso:
#   bash infra/scripts/backup.sh                    # default: ./backups/
#   BACKUP_DIR=/var/backups/agente bash …           # custom destino
#   RETENTION_DAYS=14 bash …                        # limpa antigos
#
# Cron exemplo (diário 03:00):
#   0 3 * * * cd /home/deploy/agente-inteligencia && bash infra/scripts/backup.sh >> /var/log/agente-backup.log 2>&1
#
# Restore (manual, ordem matters):
#   1. tar xzf agente-backup-YYYY-MM-DD.tar.gz
#   2. docker compose down
#   3. docker volume rm agente_postgres_data agente_qdrant_data \
#                       agente_app_data agente_caddy_data
#   4. docker volume create agente_postgres_data; idem outros
#   5. docker run --rm -v agente_qdrant_data:/data -v $(pwd)/qdrant:/restore alpine \
#         sh -c "cd /data && tar xzf /restore/qdrant.tar.gz"
#      (idem app_data e caddy_data)
#   6. docker compose up -d postgres
#   7. cat agente-postgres.sql | docker exec -i agente_postgres psql -U agente -d agente_inteligencia
#   8. docker compose up -d
# ════════════════════════════════════════════════════════════════════

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DATE="$(date +%F-%H%M)"
WORK="${BACKUP_DIR}/work-${DATE}"
ARCHIVE="${BACKUP_DIR}/agente-backup-${DATE}.tar.gz"

# Carrega .env pra ter POSTGRES_USER / POSTGRES_DB com defaults seguros
if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a
    source .env
    set +a
fi
PG_USER="${POSTGRES_USER:-agente}"
PG_DB="${POSTGRES_DB:-agente_inteligencia}"

mkdir -p "${WORK}"
echo "[$(date -Is)] backup iniciado → ${ARCHIVE}"

# ─── 1. Postgres dump ─────────────────────────────────────────────
echo "[$(date -Is)] pg_dump ${PG_DB}…"
docker exec agente_postgres pg_dump -U "${PG_USER}" -d "${PG_DB}" --no-owner --no-acl \
    > "${WORK}/agente-postgres.sql"
echo "  OK: $(wc -l < "${WORK}/agente-postgres.sql") linhas"

# ─── 2. Volumes binários ──────────────────────────────────────────
backup_volume() {
    local vol="$1"
    local out="$2"
    echo "[$(date -Is)] volume ${vol} → ${out}…"
    if ! docker volume inspect "${vol}" >/dev/null 2>&1; then
        echo "  SKIP: volume ${vol} não existe"
        return
    fi
    docker run --rm -v "${vol}:/data:ro" -v "${WORK}:/backup" alpine \
        tar czf "/backup/${out}" -C /data . 2>/dev/null || {
        echo "  WARN: tar falhou pra ${vol} (volume vazio?)"
    }
    if [[ -f "${WORK}/${out}" ]]; then
        echo "  OK: $(du -h "${WORK}/${out}" | cut -f1)"
    fi
}
backup_volume agente_qdrant_data qdrant.tar.gz
backup_volume agente_app_data    app_data.tar.gz
backup_volume agente_caddy_data  caddy_data.tar.gz

# ─── 3. Empacota tudo num único tarball ───────────────────────────
echo "[$(date -Is)] empacotando final…"
tar czf "${ARCHIVE}" -C "${BACKUP_DIR}" "work-${DATE}"
rm -rf "${WORK}"
echo "[$(date -Is)] backup concluído: ${ARCHIVE} ($(du -h "${ARCHIVE}" | cut -f1))"

# ─── 4. Retenção: remove backups mais antigos que RETENTION_DAYS ──
if [[ "${RETENTION_DAYS}" -gt 0 ]]; then
    echo "[$(date -Is)] retenção: removendo > ${RETENTION_DAYS}d em ${BACKUP_DIR}…"
    find "${BACKUP_DIR}" -maxdepth 1 -name 'agente-backup-*.tar.gz' \
        -mtime "+${RETENTION_DAYS}" -print -delete | wc -l \
        | xargs -I{} echo "  removidos: {} arquivos"
fi

echo "[$(date -Is)] FIM."
