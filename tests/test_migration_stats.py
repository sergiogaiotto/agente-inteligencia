"""Migration stats + gate strict (33.5.0) — parte unit (sem DB real).

O comportamento de CASCADE/limpeza-de-órfãos vive em
tests/integration/test_fk_cascade_real_postgres.py (Postgres real). Aqui só o
contrato barato: default do setting e o shape/isolamento do snapshot.
"""
from __future__ import annotations


def test_database_migrations_strict_default_false():
    # Default OFF = preserva o fail-open atual (retrocompat).
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert get_settings().database_migrations_strict is False


def test_get_migration_stats_shape_e_e_copia():
    from app.core import database

    s = database.get_migration_stats()
    for k in ("applied", "failed", "total", "failures", "ran_at", "strict"):
        assert k in s, k
    # get_migration_stats devolve CÓPIA — mutar não afeta o estado interno.
    s["applied"] = 999999
    assert database._MIGRATION_STATS["applied"] != 999999
