"""Histórico de revisões de conteúdo autoral (46.0.0, PR1 do arco Otimização).

Snapshots INSERT-ONLY de `skills.raw_content` e `agents.system_prompt` a cada
save — antes, o PUT sobrescrevia in-place e o texto anterior era destruído
(nenhum rollback possível; achado da varredura do arco). Este módulo é o
pré-requisito da PROMOÇÃO de variantes (PR5) e da linhagem do loop GEPA (PR4:
`parent_revision_id` + `note` = a árvore genética com o rationale).

Decisões:
- BEST-EFFORT nos hooks: falha ao gravar revisão NUNCA quebra o save (o
  conteúdo novo é o que importa; o histórico é acessório) — callers embrulham.
- DEDUP por hash: salvar sem mudar o conteúdo não gera revisão nova.
- BACKFILL na primeira edição: entidades pré-feature ganham snapshot do
  conteúdo ANTIGO antes do novo (senão a 1ª edição pós-deploy perderia o
  estado anterior — exatamente o que a feature existe para impedir).
- PODA: mantém as últimas KEEP_LAST por entidade (crescimento limitado).
- Rollback NÃO rebobina: restaurar gera um SAVE NOVO (versão bumpada,
  revisão nova com source='rollback' e parent apontando a restaurada) —
  histórico nunca é reescrito.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Tipos de entidade suportados (allowlist — rota valida contra isto).
ENTITY_SKILL = "skill"
ENTITY_AGENT_PROMPT = "agent_system_prompt"
ENTITY_TYPES = (ENTITY_SKILL, ENTITY_AGENT_PROMPT)

KEEP_LAST = 50


def _pool():
    from app.core.database import _get_pool
    return _get_pool()


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


async def record_revision(
    *,
    entity_type: str,
    entity_id: str,
    content: str,
    version: Optional[str] = None,
    source: str = "update",
    author_user_id: Optional[str] = None,
    note: Optional[str] = None,
    parent_revision_id: Optional[str] = None,
) -> Optional[str]:
    """Insere um snapshot. Dedup: se a última revisão da entidade tem o mesmo
    hash, é no-op (retorna o id existente). Poda além de KEEP_LAST. Retorna o
    id da revisão (ou None em conteúdo vazio)."""
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"entity_type inválido: {entity_type!r}")
    if not (content or "").strip():
        return None
    h = content_hash(content)
    async with _pool().acquire() as con:
        last = await con.fetchrow(
            "SELECT id, content_hash FROM content_revisions "
            "WHERE entity_type=$1 AND entity_id=$2 "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            entity_type, entity_id,
        )
        if last and last["content_hash"] == h:
            return last["id"]
        rid = f"rev_{uuid.uuid4().hex[:16]}"
        await con.execute(
            "INSERT INTO content_revisions (id, entity_type, entity_id, content, "
            "content_hash, version, source, author_user_id, note, parent_revision_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            rid, entity_type, entity_id, content, h, version, source,
            author_user_id, note, parent_revision_id,
        )
        # Poda: além das KEEP_LAST mais recentes. OFFSET exige ORDER BY no
        # subselect; DELETE por ids (Postgres não tem DELETE ... LIMIT).
        await con.execute(
            "DELETE FROM content_revisions WHERE id IN ("
            "  SELECT id FROM content_revisions "
            "  WHERE entity_type=$1 AND entity_id=$2 "
            "  ORDER BY created_at DESC, id DESC OFFSET $3)",
            entity_type, entity_id, KEEP_LAST,
        )
    return rid


async def backfill_if_first(
    *, entity_type: str, entity_id: str, old_content: str,
    version: Optional[str] = None,
) -> None:
    """1ª edição pós-deploy: snapshot do conteúdo ANTIGO antes do novo —
    sem isto a primeira edição destruiria o estado pré-feature."""
    if not (old_content or "").strip():
        return
    async with _pool().acquire() as con:
        n = await con.fetchval(
            "SELECT count(*) FROM content_revisions "
            "WHERE entity_type=$1 AND entity_id=$2",
            entity_type, entity_id,
        )
    if int(n or 0) == 0:
        await record_revision(
            entity_type=entity_type, entity_id=entity_id, content=old_content,
            version=version, source="backfill",
            note="snapshot do conteúdo anterior à 1ª edição pós-feature",
        )


async def safe_record(**kwargs) -> Optional[str]:
    """record_revision embrulhado: histórico é acessório — falha loga e segue
    (o save do conteúdo real nunca pode quebrar por causa do snapshot)."""
    try:
        return await record_revision(**kwargs)
    except Exception:
        logger.warning("event=revision_record_failed entity=%s id=%s",
                       kwargs.get("entity_type"), kwargs.get("entity_id"),
                       exc_info=True)
        return None


async def safe_backfill(**kwargs) -> None:
    try:
        await backfill_if_first(**kwargs)
    except Exception:
        logger.warning("event=revision_backfill_failed entity=%s id=%s",
                       kwargs.get("entity_type"), kwargs.get("entity_id"),
                       exc_info=True)


async def list_revisions(entity_type: str, entity_id: str,
                         limit: int = KEEP_LAST) -> list[dict]:
    """Lista SEM o conteúdo (leve p/ a UI) — content via get_revision."""
    async with _pool().acquire() as con:
        rows = await con.fetch(
            "SELECT id, entity_type, entity_id, content_hash, version, source, "
            "author_user_id, note, parent_revision_id, created_at, "
            "length(content) AS content_chars "
            "FROM content_revisions WHERE entity_type=$1 AND entity_id=$2 "
            "ORDER BY created_at DESC, id DESC LIMIT $3",
            entity_type, entity_id, limit,
        )
    return [dict(r) for r in rows]


async def get_revision(revision_id: str) -> Optional[dict]:
    async with _pool().acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM content_revisions WHERE id=$1", revision_id)
    return dict(row) if row else None
