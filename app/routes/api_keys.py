"""API keys CRUD — gera/lista/revoga chaves de integração externa.

Endpoints (todos exigem auth via cookie OU X-API-Key existente):
- POST /api/v1/api-keys           cria nova (mostra plaintext UMA vez)
- GET  /api/v1/api-keys           lista do user atual (sem plaintext)
- DELETE /api/v1/api-keys/{id}    revoga (marca revoked_at, não apaga)

Plaintext só é gerado no POST e devolvido na response. Banco só tem hash.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import require_user
from app.core.auth_apikey import generate_api_key
from app.core.database import api_keys_repo, audit_repo

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])

# F6 — janelas válidas de orçamento de custo por key (dia | mês | acumulado).
_VALID_BUDGET_WINDOWS = ("day", "month", "total")


def _validate_budget(cost_budget_usd, cost_budget_window) -> str:
    """Valida os campos de orçamento (F6). Retorna a janela normalizada.

    Regras: ``None`` de orçamento é válido (= remover / sem teto). Caso contrário
    o valor precisa ser um número FINITO e POSITIVO — rejeita 0, negativo e
    não-finito (NaN/Infinity). Sem o guard de finitude, o parser JSON aceitaria
    ``Infinity``/``NaN`` e o pydantic os coagiria para float('inf')/float('nan');
    como ``nan/inf <= 0`` é False e ``gasto >= inf/nan`` é sempre False, a key
    NUNCA seria bloqueada — furando a governança de custo (achado de segurança)."""
    if cost_budget_usd is not None:
        try:
            v = float(cost_budget_usd)
        except (TypeError, ValueError):
            raise HTTPException(400, "cost_budget_usd inválido (número em USD, ex: 5.00)")
        if not math.isfinite(v):
            raise HTTPException(400, "cost_budget_usd inválido (informe um número finito em USD, ex: 5.00)")
        if v <= 0:
            raise HTTPException(400, "cost_budget_usd deve ser positivo (omita ou envie null para remover o teto)")
    window = (cost_budget_window or "month").lower().strip()
    if window not in _VALID_BUDGET_WINDOWS:
        raise HTTPException(400, f"cost_budget_window inválido (use: {', '.join(_VALID_BUDGET_WINDOWS)})")
    return window


class APIKeyCreate(BaseModel):
    name: str
    expires_at: Optional[str] = None  # ISO 8601 string OR None pra sem expiração
    # F6 — orçamento de custo opcional. None = sem teto (comportamento atual).
    # Só é APLICADO quando api_key_cost_budget_enabled está ON (toggle global).
    cost_budget_usd: Optional[float] = None
    cost_budget_window: Optional[str] = "month"  # 'day' | 'month' | 'total'
    # Escopo por-key (Onda 6): allowed_pipeline_ids = lista de pipeline ids que a
    # key pode invocar (None/[] = todos); read_only = key só lê/descobre (invoke→403).
    allowed_pipeline_ids: Optional[list[str]] = None
    read_only: Optional[bool] = False


class APIKeyScopeUpdate(BaseModel):
    # Substitui o escopo: allowed_pipeline_ids (None/[] = liberar todos) + read_only.
    allowed_pipeline_ids: Optional[list[str]] = None
    read_only: Optional[bool] = False


class APIKeyBudgetUpdate(BaseModel):
    # None em cost_budget_usd = REMOVER o teto (key volta a ser ilimitada).
    cost_budget_usd: Optional[float] = None
    cost_budget_window: Optional[str] = "month"


@router.post("", status_code=201)
async def create_api_key(data: APIKeyCreate, request: Request, user: dict = Depends(require_user)):
    """Gera nova API key. A plaintext é retornada UMA ÚNICA VEZ — depois só hash."""
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "Name obrigatório (ex: 'zapier-prod', 'n8n-cobranças')")
    if len(name) > 100:
        raise HTTPException(400, "Name muito longo (máx 100 chars)")

    expires_at = None
    if data.expires_at:
        try:
            # Aceita ISO com ou sem timezone. A coluna expires_at é TIMESTAMP SEM tz
            # (naive) — asyncpg recusa um datetime aware ("can't subtract offset-naive
            # and offset-aware datetimes"). O modal manda '...Z' (aware), então
            # convertemos pra UTC e tiramos o tzinfo. Armazenamento é UTC naive (igual
            # ao resto do app); verify_api_key compara com datetime.utcnow() (naive).
            dt = datetime.fromisoformat(data.expires_at.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            expires_at = dt
        except Exception:
            raise HTTPException(400, "expires_at inválido (use ISO 8601: 2026-12-31T23:59:59Z)")

    # F6 — orçamento de custo (opcional). Validado aqui; aplicado em runtime só
    # quando o toggle global api_key_cost_budget_enabled está ON.
    window = _validate_budget(data.cost_budget_usd, data.cost_budget_window)

    plaintext, prefix, key_hash = generate_api_key()
    key_id = str(uuid.uuid4())

    await api_keys_repo.create({
        "id": key_id,
        "user_id": user["id"],
        "name": name,
        "key_hash": key_hash,
        "key_prefix": prefix,
        "expires_at": expires_at,
        "cost_budget_usd": data.cost_budget_usd,
        "cost_budget_window": window,
        # Escopo por-key (Onda 6): allowed_pipeline_ids é TEXT (JSON) — serializo
        # à mão (a Repository só coage dict/list em colunas JSON/JSONB, não TEXT).
        "allowed_pipeline_ids": json.dumps(data.allowed_pipeline_ids) if data.allowed_pipeline_ids else None,
        "read_only": bool(data.read_only),
    })

    await audit_repo.create({
        "entity_type": "api_key",
        "entity_id": key_id,
        "action": "created",
        "actor": user["id"],
        "details": json.dumps({
            "name": name, "prefix": prefix,
            "cost_budget_usd": data.cost_budget_usd, "cost_budget_window": window,
        }),
    })

    return {
        "id": key_id,
        "name": name,
        "key": plaintext,  # ⚠ ÚNICA VEZ que o plaintext aparece
        "prefix": prefix,
        "expires_at": data.expires_at,
        "cost_budget_usd": data.cost_budget_usd,
        "cost_budget_window": window,
        "warning": "Copie a key agora — não será mostrada de novo.",
    }


@router.patch("/{key_id}/scope")
async def update_api_key_scope(key_id: str, data: APIKeyScopeUpdate, user: dict = Depends(require_user)):
    """Define/substitui o escopo de uma key (Onda 6): allowed_pipeline_ids +
    read_only. Só o DONO da key (ou root). Vazio/None em allowed = liberar todos."""
    row = await api_keys_repo.find_by_id(key_id)
    is_root = (user.get("role") or "").strip().lower() == "root"
    if not row or (row.get("user_id") != user.get("id") and not is_root):
        raise HTTPException(404, "API key não encontrada")
    allowed = json.dumps(data.allowed_pipeline_ids) if data.allowed_pipeline_ids else None
    await api_keys_repo.update(key_id, {
        "allowed_pipeline_ids": allowed,
        "read_only": bool(data.read_only),
    })
    await audit_repo.create({
        "entity_type": "api_key", "entity_id": key_id, "action": "scope_updated",
        "actor": user["id"],
        "details": json.dumps({
            "allowed_pipeline_ids": data.allowed_pipeline_ids, "read_only": bool(data.read_only),
        }),
    })
    return {"id": key_id, "allowed_pipeline_ids": data.allowed_pipeline_ids or [],
            "read_only": bool(data.read_only)}


@router.get("")
async def list_api_keys(user: dict = Depends(require_user)):
    """Lista API keys do user atual. Não expõe plaintext nem hash.

    F6: inclui o orçamento de custo (``cost_budget_usd``/``cost_budget_window``) e
    o gasto acumulado da janela corrente (``spent_usd``) por key, mais o estado do
    toggle global (``budget_enabled``) — pra UI mostrar "gasto / teto".
    """
    from app.core.config import get_settings
    from app.core.api_key_budget import normalize_window, current_spend_usd

    rows = await api_keys_repo.find_all(user_id=user["id"], limit=200)
    # Ordena: ativas primeiro, depois revogadas; dentro do grupo por created_at desc
    rows.sort(key=lambda r: (r.get("revoked_at") is not None, r.get("created_at") or ""), reverse=False)
    budget_enabled = bool(get_settings().api_key_cost_budget_enabled)
    keys = []
    for r in rows:
        window = normalize_window(r.get("cost_budget_window"))
        budget = r.get("cost_budget_usd")
        # Gasto só é computado pra keys ATIVAS COM orçamento — é o único caso em que
        # o gasto é exibido (progresso rumo ao teto). Keys sem teto não pagam a
        # query de SUM (evita N+1 na lista quando F6 não está em uso). Best-effort:
        # falha de agregação não derruba a lista.
        spent = None
        if r.get("revoked_at") is None and budget is not None:
            try:
                spent = round(await current_spend_usd(r["id"], window), 6)
            except Exception:
                spent = None
        keys.append({
            "id": r["id"],
            "name": r["name"],
            "prefix": r["key_prefix"],
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
            "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
            "revoked_at": r["revoked_at"].isoformat() if r.get("revoked_at") else None,
            "active": r.get("revoked_at") is None,
            "cost_budget_usd": float(budget) if budget is not None else None,
            "cost_budget_window": window,
            "spent_usd": spent,
        })
    return {"keys": keys, "budget_enabled": budget_enabled}


@router.put("/{key_id}/budget")
async def set_api_key_budget(
    key_id: str, data: APIKeyBudgetUpdate, user: dict = Depends(require_user),
):
    """F6: define/atualiza/remove o orçamento de custo de uma key existente.

    ``cost_budget_usd=null`` REMOVE o teto (key volta a ilimitada). Só o dono (ou
    admin) altera. O orçamento só é EXECUTADO quando o toggle global
    api_key_cost_budget_enabled está ON — mas pode ser configurado a qualquer hora.
    """
    row = await api_keys_repo.find_by_id(key_id)
    if not row:
        raise HTTPException(404, "API key não encontrada")
    if row["user_id"] != user["id"] and user.get("role") not in ("admin", "root"):
        raise HTTPException(403, "Sem permissão pra alterar key de outro usuário")
    if row.get("revoked_at") is not None:
        raise HTTPException(409, "Key revogada — não é configurável")
    window = _validate_budget(data.cost_budget_usd, data.cost_budget_window)
    await api_keys_repo.update(key_id, {
        "cost_budget_usd": data.cost_budget_usd,
        "cost_budget_window": window,
    })
    await audit_repo.create({
        "entity_type": "api_key",
        "entity_id": key_id,
        "action": "budget_set",
        "actor": user["id"],
        "details": json.dumps({
            "name": row["name"],
            "cost_budget_usd": data.cost_budget_usd,
            "cost_budget_window": window,
        }),
    })
    return {
        "id": key_id,
        "cost_budget_usd": data.cost_budget_usd,
        "cost_budget_window": window,
        "message": "Orçamento removido" if data.cost_budget_usd is None else "Orçamento atualizado",
    }


@router.delete("/{key_id}")
async def revoke_api_key(key_id: str, user: dict = Depends(require_user)):
    """Revoga (não apaga). Preserva audit. Idempotente."""
    row = await api_keys_repo.find_by_id(key_id)
    if not row:
        raise HTTPException(404, "API key não encontrada")
    if row["user_id"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(403, "Sem permissão pra revogar key de outro usuário")
    if row.get("revoked_at") is not None:
        return {"message": "Já estava revogada", "id": key_id}
    await api_keys_repo.update(key_id, {"revoked_at": datetime.utcnow()})
    await audit_repo.create({
        "entity_type": "api_key",
        "entity_id": key_id,
        "action": "revoked",
        "actor": user["id"],
        "details": json.dumps({"name": row["name"], "prefix": row["key_prefix"]}),
    })
    return {"message": "API key revogada", "id": key_id}
