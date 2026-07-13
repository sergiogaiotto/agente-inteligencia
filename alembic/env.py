"""Ambiente de migração Alembic (Onda 4, 33.6.0).

A URL vem das settings do app (não do alembic.ini) e o app usa asyncpg cru em
runtime — aqui, porém, alembic roda SÍNCRONO via psycopg2 (`postgresql+psycopg2://`),
o caminho estoque/robusto. ``target_metadata=None``: as migrações são SQL escrito
à mão (o app não tem modelos SQLAlchemy), então NÃO há autogenerate.
"""
from __future__ import annotations

import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings

config = context.config

# URL do banco: ALEMBIC_DATABASE_URL (override — ops/CI miram um DB específico)
# senão settings.database_url do app (asyncpg → psycopg2 sync p/ o alembic).
_url = (os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url or "").strip()
if _url.startswith("postgresql+asyncpg://"):
    _url = _url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
elif _url.startswith("postgresql://"):
    _url = _url.replace("postgresql://", "postgresql+psycopg2://", 1)
config.set_main_option("sqlalchemy.url", _url)

# Logging do alembic — SÓ no CLI standalone (root ainda sem handlers).
# In-process (migrations no boot do app, database._alembic_upgrade_sync), o
# fileConfig do alembic.ini DERRUBAVA o logging do app: substituía os handlers
# do root (app.log/api.log/console JSON sumiam) e, com o default
# disable_existing_loggers=True, DESABILITAVA todos os loggers já criados
# (app.*, uvicorn.* — nem access log sobrava). Nenhum log de runtime chegava a
# sink algum desde a 33.17.0 (#585, primeira release com alembic no boot).
# O try/except antigo só protegia contra EXCEÇÃO, não contra esse efeito
# colateral. Diagnóstico: revisão E2E Pulsar, 2026-07-13.
if config.config_file_name is not None and not logging.getLogger().handlers:
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:
        pass

# Sem modelos SQLAlchemy no app → sem autogenerate; migrações são SQL manual.
target_metadata = None


def run_migrations_offline() -> None:
    """Modo offline — gera SQL sem conectar (alembic upgrade --sql)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Modo online — conecta (psycopg2) e aplica as revisões pendentes."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
