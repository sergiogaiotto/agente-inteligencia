"""api_keys.allowed_pipeline_ids + read_only — escopo por-key (Onda 6, 33.17.0).

Uma API key invocava QUALQUER pipeline. Estas colunas dão escopo: allowed_
pipeline_ids (JSON array; NULL/[] = todos) restringe quais pipelines a key pode
invocar; read_only marca a key como só-leitura (invoke → 403). Colunas na
tabela EXISTENTE api_keys → padrão do keystone: coluna no SCHEMA base (DB fresco/
CI) + esta revisão (DBs existentes). Sem índice (filtro é por-key no auth, não
em massa). Idempotente por IF NOT EXISTS.
"""
from __future__ import annotations

from alembic import op

revision = "0004_api_keys_scope"
down_revision = "0003_interactions_owner_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_pipeline_ids TEXT")
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS read_only BOOLEAN DEFAULT FALSE")


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS read_only")
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS allowed_pipeline_ids")
