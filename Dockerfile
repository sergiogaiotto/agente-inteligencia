# syntax=docker/dockerfile:1.6
# ══════════════════════════════════════════════════════════════
# Maestro — Dockerfile multi-stage
# Stage 1: builder (compila wheels nativos: asyncpg, hiredis, bcrypt)
# Stage 2: runtime (slim, sem toolchain, com usuário não-root)
# ══════════════════════════════════════════════════════════════

# ─── Stage 1: builder ─────────────────────────────────────────
# Base FIXA em 3.11: o wheelhouse (pip wheel) não resolve em 3.14 — magika/
# onnxruntime (via markitdown[all]) ainda não publicam cp314, e o resolver
# falha com conflito em youtube-transcript-api. Bump de base SÓ com build
# local verde (`docker compose build app`) + suíte no container.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .

# Compila tudo num wheelhouse local — copiado depois para o runtime
RUN pip wheel --wheel-dir=/wheels -r requirements.txt


# ─── Stage 2: runtime ─────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app \
    APP_PORT=7000

# Apenas as libs nativas necessárias em runtime — sem toolchain
# - libpq5: asyncpg
# - ffmpeg: markitdown[all] audio-transcription (Onda 6 RAG)
# - libmagic1: detecção MIME por conteúdo (markitdown usa)
# - perl + libimage-exiftool-perl: exiftool, lido por markitdown pra metadados de imagem
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        ca-certificates \
        ffmpeg \
        libmagic1 \
        libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app -u 1000 -d ${APP_HOME} -s /bin/bash app

WORKDIR ${APP_HOME}

# Instala wheels pré-compiladas
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

# Copia código do app
COPY --chown=app:app app ./app

# Scripts operacionais (ex.: seed do usuário E2E, rodado via `docker exec`).
COPY --chown=app:app scripts ./scripts

# Diretório de uploads / dados
RUN mkdir -p ${APP_HOME}/data/uploads && chown -R app:app ${APP_HOME}/data

# Diretório de logs estruturados (logging_setup grava aqui via LOG_DIR=logs).
# Sem mkdir+chown explícitos o user `app` (uid 1000) não conseguia criar a
# pasta dentro de /app/ (que é root) e setup_logging() falhava silenciosamente
# — file handlers viravam no-op e a UI de Manutenção de Logs mostrava 0 B.
RUN mkdir -p ${APP_HOME}/logs && chown -R app:app ${APP_HOME}/logs

USER app

EXPOSE 7000

# Healthcheck consome o /api/health do FastAPI
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${APP_PORT}/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7000", "--proxy-headers", "--forwarded-allow-ips=*"]
