"""baseline — ponto de STAMP do schema existente (Onda 4, 33.6.0).

O schema (tabelas/índices/FKs) É criado pelo ``init_db`` DDL + ``_IDEMPOTENT_MIGRATIONS``
(app/core/database.py), que já rodou em TODOS os DBs (dev/VPS). Esta revisão é um
NO-OP proposital: serve só como versão-0 para o Alembic marcar o banco como
"gerenciado" — ``alembic upgrade head`` num DB existente OU fresco apenas cria a
tabela ``alembic_version`` e registra esta revisão, SEM tocar o schema.

Migrações FUTURAS entram como revisões APÓS esta (com upgrade/downgrade reais);
o fold das ~94 migrações idempotentes existentes no Alembic é um follow-up
deliberado (evita o risco de um baseline que não bate 1:1 com o schema em prod).
"""
from __future__ import annotations

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NO-OP: o schema já existe (criado pelo init_db DDL). Ver docstring.
    pass


def downgrade() -> None:
    # Baseline não tem downgrade (não recriamos/removemos o schema por aqui).
    pass
