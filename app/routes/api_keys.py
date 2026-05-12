"""API keys CRUD — gera/lista/revoga chaves de integração externa.

Endpoints (todos exigem auth via cookie OU X-API-Key existente):
- POST /api/v1/api-keys           cria nova (mostra plaintext UMA vez)
- GET  /api/v1/api-keys           lista do user atual (sem plaintext)
- DELETE /api/v1/api-keys/{id}    revoga (marca revoked_at, não apaga)

Plaintext só é gerado no POST e devolvido na response. Banco só tem hash.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import require_user
from app.core.auth_apikey import generate_api_key
from app.core.database import api_keys_repo, audit_repo

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])


class APIKeyCreate(BaseModel):
    name: str
    expires_at: Optional[str] = None  # ISO 8601 string OR None pra sem expiração


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
            # Aceita ISO com ou sem timezone
            expires_at = datetime.fromisoformat(data.expires_at.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(400, "expires_at inválido (use ISO 8601: 2026-12-31T23:59:59Z)")

    plaintext, prefix, key_hash = generate_api_key()
    key_id = str(uuid.uuid4())

    await api_keys_repo.create({
        "id": key_id,
        "user_id": user["id"],
        "name": name,
        "key_hash": key_hash,
        "key_prefix": prefix,
        "expires_at": expires_at,
    })

    await audit_repo.create({
        "entity_type": "api_key",
        "entity_id": key_id,
        "action": "created",
        "actor": user["id"],
        "details": json.dumps({"name": name, "prefix": prefix}),
    })

    return {
        "id": key_id,
        "name": name,
        "key": plaintext,  # ⚠ ÚNICA VEZ que o plaintext aparece
        "prefix": prefix,
        "expires_at": data.expires_at,
        "warning": "Copie a key agora — não será mostrada de novo.",
    }


@router.get("")
async def list_api_keys(user: dict = Depends(require_user)):
    """Lista API keys do user atual. Não expõe plaintext nem hash."""
    rows = await api_keys_repo.find_all(user_id=user["id"], limit=200)
    # Ordena: ativas primeiro, depois revogadas; dentro do grupo por created_at desc
    rows.sort(key=lambda r: (r.get("revoked_at") is not None, r.get("created_at") or ""), reverse=False)
    return {
        "keys": [
            {
                "id": r["id"],
                "name": r["name"],
                "prefix": r["key_prefix"],
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
                "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
                "revoked_at": r["revoked_at"].isoformat() if r.get("revoked_at") else None,
                "active": r.get("revoked_at") is None,
            }
            for r in rows
        ]
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
