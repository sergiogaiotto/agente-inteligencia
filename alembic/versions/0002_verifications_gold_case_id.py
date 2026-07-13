"""verifications.gold_case_id — elo harness ↔ produção (keystone 33.10.0).

Liga cada linha de ``verifications`` ao caso do Golden Dataset que a originou
(``gold_cases.id``) quando a verificação vem de um run do harness; NULL na
produção normal. Destrava o drift (Q1) e o RAGAS-com-gabarito (Q3): passa a
dar para join-ar o veredito de produção ao baseline do gold.

Esta é a PRIMEIRA revisão REAL após o baseline no-op (0001) — inaugura o
caminho Alembic para mudanças de schema (antes as adições iam em
``_IDEMPOTENT_MIGRATIONS``). O DDL base (``SCHEMA`` em app/core/database.py)
já cria a coluna em DB FRESCO/CI; esta revisão a adiciona nos DBs EXISTENTES
(prod) e cria o índice (que NÃO pode viver no SCHEMA — em DB existente a
coluna ainda não existe quando o SCHEMA roda → CREATE INDEX = boot crash).

Idempotente por ``IF [NOT] EXISTS``: o ``alembic_version`` já garante que o
upgrade roda uma vez por DB, mas a guarda protege contra a coluna já existir
por qualquer via (ex.: DB fresco que veio do SCHEMA antes do upgrade).
"""
from __future__ import annotations

from alembic import op

revision = "0002_verifications_gold_case_id"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE verifications ADD COLUMN IF NOT EXISTS gold_case_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_verifications_gold_case "
        "ON verifications (gold_case_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_verifications_gold_case")
    op.execute("ALTER TABLE verifications DROP COLUMN IF EXISTS gold_case_id")
