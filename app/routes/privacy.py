"""Direito ao esquecimento (LGPD Art.18) — arco LGPD-2 (35.9.0).

O titular (cliente-final) é identificado por `customer_ref` (CPF/id/email),
guardado só como HASH (customer_hash) na criação da interaction. Aqui o
operador (root/admin) apaga TODAS as conversas daquele titular: mesmo
delete+scrub da retenção, por titular em vez de idade. Auditado.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.database import audit_repo

router = APIRouter(prefix="/api/v1/privacy", tags=["privacy"])


class ForgetRequest(BaseModel):
    # O identificador CRU do cliente-final (não o hash) — hasheamos server-side
    # (mesma normalização da criação) p/ o operador não precisar calcular o hash.
    customer_ref: str


@router.post("/forget")
async def forget_customer_data(
    data: ForgetRequest,
    user: dict = Depends(require_role("root", "admin")),
):
    """Apaga todas as conversas do titular `customer_ref`. Root/admin apenas.

    Delete das interactions (cascade turns/tool_calls/binding) + scrub do texto
    do juiz (verifications) + varredura de api_call_logs/verifier_jobs. Os
    números de custo/qualidade (invocation_costs, scores) são preservados —
    interesse legítimo, sem conteúdo pessoal. Auditado (SEM o ref cru: só o
    hash + os contadores)."""
    from app.core.retention import hash_customer_ref, forget_customer
    chash = hash_customer_ref(data.customer_ref)
    if not chash:
        raise HTTPException(422, "customer_ref vazio — informe o identificador do cliente.")
    result = await forget_customer(chash)
    # Auditoria: NUNCA grava o ref cru (seria re-introduzir o dado que se apaga).
    await audit_repo.create({
        "entity_type": "privacy", "entity_id": chash[:16],
        "action": "customer_forgotten", "actor": user["id"],
        "details": json.dumps({
            "customer_hash_prefix": chash[:16],
            "deleted": result["deleted"],
            "scrubbed_verifications": result["scrubbed_verifications"],
            # 35.15.1 (achado da auditoria #4): um DSAR de sessão MISTA apaga
            # turns/jobs/arquivos sem deletar interaction inteira — sem estes
            # contadores o esquecimento parecia um no-op na auditoria.
            "turns_deleted": result.get("turns_deleted", 0),
            "invoke_jobs_deleted": result.get("invoke_jobs_deleted", 0),
            "files_deleted": result.get("files_deleted", 0),
        }),
    })
    return {
        "status": "ok",
        "customer_hash_prefix": chash[:16],
        "deleted_interactions": result["deleted"],
        "scrubbed_verifications": result["scrubbed_verifications"],
        "turns_deleted": result.get("turns_deleted", 0),
        "invoke_jobs_deleted": result.get("invoke_jobs_deleted", 0),
        "files_deleted": result.get("files_deleted", 0),
        "hint": ("Conversas apagadas e texto do juiz anonimizado. Os agregados "
                 "de custo/qualidade (sem conteúdo pessoal) foram preservados."),
    }
