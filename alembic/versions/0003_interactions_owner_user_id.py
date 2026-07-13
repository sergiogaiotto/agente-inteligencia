"""interactions.owner_user_id — fecha o IDOR do interaction_id (Onda 6, 33.13.0).

O `interaction_id`/`session_id` era um handle PORTADOR: qualquer autenticado que
soubesse o id lia/reinjetava a conversa alheia (vazamento cross-tenant). Esta
coluna dá o DONO a cada interaction; o gate de acesso (app/core/interaction_access.py)
barra reusar/ler o id de outro dono. NULLABLE — interactions legadas não têm
dono (sem atribuição retroativa); o 1º acesso do dono a carimba.

Segue o padrão do 0002 (keystone): a coluna vive no SCHEMA base (DB fresco/CI) E
nesta revisão (DBs existentes/prod). O índice vive SÓ aqui (nunca no SCHEMA: em
DB existente a coluna ainda não existe quando o init_db DDL roda → CREATE INDEX
= boot crash). Idempotente por IF [NOT] EXISTS.
"""
from __future__ import annotations

from alembic import op

revision = "0003_interactions_owner_user_id"
down_revision = "0002_verifications_gold_case_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE interactions ADD COLUMN IF NOT EXISTS owner_user_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_interactions_owner "
        "ON interactions (owner_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_interactions_owner")
    op.execute("ALTER TABLE interactions DROP COLUMN IF EXISTS owner_user_id")
