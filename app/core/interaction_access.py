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
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# ── Dono na CRIAÇÃO (35.4.0, review do deadline por job) ──
# O stamp pós-execução deixava uma janela: um aborto server-side DEPOIS da
# criação (o timeout do invoke-job foi o 1º caminho DETERMINÍSTICO) órfãva a
# interaction SEM dono — listável por todo autenticado (`OR owner IS NULL`) e
# sequestrável no reuso do session_id. O caller (rota/worker) seta o dono no
# CONTEXTO da execução e os pontos de criação (FSM run_intake + cadeia
# declarativa) o incluem no INSERT — nasce com dono; o stamp pós vira rede de
# segurança. ContextVar: flui por await/task-filho sem threading de assinatura.
_creation_owner: ContextVar[Optional[str]] = ContextVar(
    "interaction_creation_owner", default=None
)


def set_interaction_owner_for_creation(user_id: Optional[str]) -> None:
    _creation_owner.set((user_id or "").strip() or None)


def interaction_owner_for_creation() -> Optional[str]:
    return _creation_owner.get()


# ── customer_hash na CRIAÇÃO (35.9.0, arco LGPD-2) ──
# Pivô do direito ao esquecimento: quando o request informa `customer_ref` (o
# identificador do cliente-final), guardamos só o HASH na interaction — dá o
# alvo do DELETE por titular. Mesmo padrão ContextVar do owner-na-criação.
_creation_customer: ContextVar[Optional[str]] = ContextVar(
    "interaction_creation_customer_hash", default=None
)


def set_interaction_customer_for_creation(customer_ref: Optional[str]) -> None:
    from app.core.retention import hash_customer_ref
    _creation_customer.set(hash_customer_ref(customer_ref))


def set_interaction_customer_hash_for_creation(customer_hash: Optional[str]) -> None:
    """35.14.2: seta o HASH já pronto (o worker do 202 não tem o ref cru — ele
    NÃO é mais persistido no job; só o hash trafega)."""
    _creation_customer.set((customer_hash or "").strip() or None)


def interaction_customer_hash_for_creation() -> Optional[str]:
    return _creation_customer.get()


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
    """Barra IDOR: interaction de OUTRO dono → 404; LEGADA sem dono → só root.

    404 (não 403) de propósito: não confirma a EXISTÊNCIA da conversa a um
    não-dono (evita enumeração de ids). No-op quando: sem `interaction_id`,
    id INEXISTENTE (sessão nova que o caller acabou de cunhar), ou root.

    ENDURECIMENTO 35.7.0 (decisão do dono, FF7): legada-sem-dono deixou de ser
    liberada a todos — era o buraco residual do IDOR (qualquer autenticado lia
    E, ao reusar o session_id, SEQUESTRAVA a conversa via stamp do 1º acesso).
    Com o dono-na-criação (#595) toda linha nova nasce carimbada; as NULL são
    só o legado — root as vê e pode atribuí-las cirurgicamente (claim)."""
    if not interaction_id:
        return
    if _can_bypass(user):
        return
    # Distinguir INEXISTENTE (permite — id novo) de LEGADA sem dono (root-only)
    # exige olhar a linha, não só o owner (ambos davam None no lookup antigo).
    try:
        from app.core.database import interactions_repo
        row = await interactions_repo.find_by_id(interaction_id)
    except Exception as e:
        logger.warning("interaction_access.lookup_failed id=%s: %s",
                       interaction_id, str(e)[:150])
        return  # fail-open (paridade com owner_of_interaction)
    if not row:
        return  # sessão nova — o invoke a criará (com dono, #595)
    owner = row.get("owner_user_id")
    if owner == (user or {}).get("id"):
        return
    logger.warning(
        "interaction_access.idor_blocked",
        extra={
            "event": "security.idor_blocked",
            "interaction_id": interaction_id,
            "owner_user_id": owner,
            "caller_user_id": (user or {}).get("id"),
            "legacy_unowned": owner is None,
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
