"""Rotas de federação A2A — provider/ingress (PR8b).

PR8b1: descoberta READ-ONLY via manifesto well-known. O endpoint de invoke
assinado (`POST /api/v1/federation/invoke`) é PR8b2.

Gate por `federation_enabled()` (default OFF → 404, instância invisível). Quando
ligada, o manifesto só expõe capabilities published+company (allowlist de kinds)
— ver `is_federation_exposable`. Sem auth no PR8b1 (descoberta de capabilities já
company-visíveis); peer-gating do manifesto é endurecimento de PR8c.
"""
from fastapi import APIRouter, HTTPException

from app.catalog.federation import build_manifest
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
