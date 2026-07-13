"""Controle de acesso a `interactions` por dono — fecha o IDOR (Onda 6, 33.13.0).

O `interaction_id`/`session_id` era um handle PORTADOR: até aqui QUALQUER
autenticado que soubesse o id conseguia ler a conversa alheia (`GET
/sessions/{id}`) ou REINJETÁ-LA no LLM (reusando o `session_id` no invoke
multi-turno) — vazamento cross-tenant (IDOR).

Este módulo dá o gate:
- `assert_can_access_interaction` — barra o acesso quando a interaction TEM dono
  e não é o do chamador (nem root). Aplicado ON-PATH (síncrono, ANTES de reusar/
  ler), nunca no caminho off-path de analytics.
- `stamp_interaction_owner` — carimba o dono no 1º acesso (best-effort).

`owner_user_id` NULL = interaction LEGADA (pré-fix): sem dono conhecido, o gate
NÃO bloqueia (não dá pra atribuir retroativamente); o 1º acesso a carimba, e daí
em diante fica protegida. Dono canônico = `user['id']` (cookie OU dono da
API-key — `require_user` resolve os dois para o usuário DONO).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _can_bypass(user: Optional[dict]) -> bool:
    """Só `root` acessa interaction alheia (suporte/operação). `admin` NÃO lê
    conversa de terceiros por padrão — privacidade > conveniência."""
    return bool(user) and (user.get("role") or "").strip().lower() == "root"


async def owner_of_interaction(interaction_id: Optional[str]) -> Optional[str]:
    """`owner_user_id` da interaction, ou None (inexistente OU legada sem dono).
    Fail-open a None em erro de DB (o caller trata None como 'não bloqueia')."""
    if not interaction_id:
        return None
    try:
        from app.core.database import _get_pool
        async with _get_pool().acquire() as con:
            return await con.fetchval(
                "SELECT owner_user_id FROM interactions WHERE id = $1", interaction_id
            )
    except Exception as e:
        logger.warning("interaction_access.owner_lookup_failed id=%s: %s",
                       interaction_id, str(e)[:150])
        return None


async def assert_can_access_interaction(interaction_id: Optional[str], user: Optional[dict]) -> None:
    """Barra IDOR: se a interaction TEM dono e NÃO é o do chamador (nem root) → 404.

    404 (não 403) de propósito: não confirma a EXISTÊNCIA da conversa a um
    não-dono (evita enumeração de ids). No-op quando: sem `interaction_id`, ou a
    interaction é inexistente/legada-sem-dono (owner None), ou o chamador é root.
    """
    if not interaction_id:
        return
    owner = await owner_of_interaction(interaction_id)
    if owner is None:            # inexistente OU legada sem dono → nada a barrar aqui
        return
    if _can_bypass(user):
        return
    if owner != (user or {}).get("id"):
        logger.warning(
            "interaction_access.idor_blocked",
            extra={
                "event": "security.idor_blocked",
                "interaction_id": interaction_id,
                "owner_user_id": owner,
                "caller_user_id": (user or {}).get("id"),
            },
        )
        raise HTTPException(404, "Sessão não encontrada")


async def stamp_interaction_owner(interaction_id: Optional[str], user_id: Optional[str]) -> None:
    """Carimba `owner_user_id` na interaction se ainda SEM dono (1º acesso).

    `WHERE owner_user_id IS NULL` não sobrescreve um dono já gravado (idempotente
    + à prova de corrida benigna). Best-effort — nunca derruba a request (o
    invoke/analytics é off-path; a persistência do dono também não pode bloquear).
    """
    if not interaction_id or not user_id:
        return
    try:
        from app.core.database import _get_pool
        async with _get_pool().acquire() as con:
            await con.execute(
                "UPDATE interactions SET owner_user_id = $1 "
                "WHERE id = $2 AND owner_user_id IS NULL",
                user_id, interaction_id,
            )
    except Exception as e:
        logger.warning("interaction_access.stamp_failed id=%s: %s",
                       interaction_id, str(e)[:150])
