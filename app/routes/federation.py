"""Rotas de federação A2A — provider/ingress (PR8b).

PR8b1: descoberta READ-ONLY via manifesto well-known. O endpoint de invoke
assinado (`POST /api/v1/federation/invoke`) é PR8b2.

Gate por `federation_enabled()` (default OFF → 404, instância invisível). Quando
ligada, o manifesto só expõe capabilities published+company (allowlist de kinds)
— ver `is_federation_exposable`. Sem auth no PR8b1 (descoberta de capabilities já
company-visíveis); peer-gating do manifesto é endurecimento de PR8c.
"""
import json
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.catalog import federation_peers as peers
from app.catalog.federation import build_manifest
from app.catalog.queries import is_root
from app.core.auth import require_user
from app.core.database import audit_repo
from app.core.federation_identity import federation_enabled

router = APIRouter(tags=["federation"])


@router.get("/.well-known/maestro-federation.json")
async def federation_manifest():
    """Manifesto de descoberta desta instância: capabilities published+company,
    com URN federada, resumo de disclosure e fingerprint (pipelines). 404 quando
    a federação está desligada (default — instância não anuncia nada)."""
    if not await federation_enabled():
        # 404 SEM detalhe custom: desligada deve ser indistinguível de inexistente
        # (instância "invisível"). Um detalhe próprio vazaria que a rota existe.
        raise HTTPException(404)
    return await build_manifest()


# ── Registro de peers (PR8b2) — ROOT-only; gere relações de confiança ────────
peers_router = APIRouter(prefix="/api/v1/federation/peers", tags=["federation"])


class PeerCreate(BaseModel):
    workspace: str
    base_url: Optional[str] = None


def _require_root(user: dict) -> None:
    if not is_root(user):
        raise HTTPException(403, "Apenas root pode gerir peers de federação")


def _peer_public(r: dict) -> dict:
    """View pública do peer — NUNCA expõe shared_secret/secret_prev."""
    return {
        "id": r["id"],
        "workspace": r["workspace"],
        "base_url": r.get("base_url"),
        "status": r.get("status"),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "rotated_at": r["rotated_at"].isoformat() if r.get("rotated_at") else None,
        "has_prev_secret": bool(r.get("secret_prev")),
    }


async def _audit_peer(action: str, peer_id: str, actor: str, workspace: str) -> None:
    await audit_repo.create({
        "entity_type": "federation_peer",
        "entity_id": peer_id,
        "action": action,
        "actor": actor,
        "details": json.dumps({"workspace": workspace}),
    })


@peers_router.post("", status_code=201)
async def create_peer(data: PeerCreate, user: dict = Depends(require_user)):
    """Registra um peer confiável. Devolve o shared_secret em plaintext UMA vez
    (compartilhe com o peer; o banco só guarda cifrado)."""
    _require_root(user)
    try:
        row, secret = await peers.register_peer(data.workspace, data.base_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"Peer para workspace '{data.workspace}' já existe (use rotate p/ trocar o segredo)")
    except Exception as e:
        # Fallback p/ ambientes/fakes que não sobem UniqueViolationError tipada
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"Peer para workspace '{data.workspace}' já existe (use rotate p/ trocar o segredo)")
        raise
    await _audit_peer("created", row["id"], user["id"], row["workspace"])
    return {
        **_peer_public(row),
        "shared_secret": secret,  # ⚠ ÚNICA vez que o plaintext aparece
        "warning": "Compartilhe o segredo com o peer agora — não será mostrado de novo.",
    }


@peers_router.get("")
async def list_peers_route(user: dict = Depends(require_user)):
    """Lista peers (sem segredos)."""
    _require_root(user)
    rows = await peers.list_peers()
    return {"peers": [_peer_public(r) for r in rows]}


@peers_router.post("/{peer_id}/rotate")
async def rotate_peer_route(peer_id: str, user: dict = Depends(require_user)):
    """Roda o segredo do peer (janela de sobreposição). Devolve o novo plaintext."""
    _require_root(user)
    res = await peers.rotate_peer_secret(peer_id)
    if not res:
        raise HTTPException(404, "Peer não encontrado")
    row, secret = res
    await _audit_peer("rotated", peer_id, user["id"], row["workspace"])
    return {
        **_peer_public(row),
        "shared_secret": secret,
        "warning": "Compartilhe o novo segredo — o anterior ainda vale até a próxima rotação.",
    }


@peers_router.delete("/{peer_id}")
async def revoke_peer_route(peer_id: str, user: dict = Depends(require_user)):
    """Revoga o peer (status='revoked'; não apaga). Idempotente p/ ausência → 404."""
    _require_root(user)
    row = await peers.revoke_peer(peer_id)
    if not row:
        raise HTTPException(404, "Peer não encontrado")
    await _audit_peer("revoked", peer_id, user["id"], row.get("workspace", ""))
    return {"message": "Peer revogado", "id": peer_id}
