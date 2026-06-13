"""Rotas de federação A2A — provider/ingress (PR8b).

PR8b1: descoberta READ-ONLY via manifesto well-known. O endpoint de invoke
assinado (`POST /api/v1/federation/invoke`) é PR8b2.

Gate por `federation_enabled()` (default OFF → 404, instância invisível). Quando
ligada, o manifesto só expõe capabilities published+company (allowlist de kinds)
— ver `is_federation_exposable`. Sem auth no PR8b1 (descoberta de capabilities já
company-visíveis); peer-gating do manifesto é endurecimento de PR8c.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.a2a.protocol import Envelope
from app.catalog import federation as fed
from app.catalog import federation_peers as peers
from app.catalog.federation import build_manifest
from app.catalog.queries import create_execution, get_execution, is_root
from app.core.auth import require_user
from app.core.database import audit_repo
from app.core.federation_identity import federation_enabled, secret_key_present

logger = logging.getLogger(__name__)

router = APIRouter(tags=["federation"])

# Defesas DoS aplicáveis no ingress. NOTA: o budget (tokens/usd) do envelope NÃO é
# enforçado pelo engine hoje (execute_pipeline não recebe budget) — é ADVISORY até
# o engine ganhar gate de budget. Aqui limitamos corpo, input e tempo de execução.
_MAX_BODY_BYTES = 262_144   # 256 KB — cap pré-parse (proxy/uvicorn é o limite duro)
_MAX_INPUT_CHARS = 100_000
_EXEC_TIMEOUT_S = 120


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


# ── Ingress assinado (PR8b3) — a ponta de execução de rede ───────────────────


@router.post("/api/v1/federation/invoke")
async def federation_invoke(request: Request):
    """Invoke ASSINADO inbound. Body JSON: `{"envelope": {...}, "signature": "<hex>"}`.
    Verifica o HMAC do peer + janela de replay, resolve o alvo (só published+company),
    executa SELADO ao snapshot e devolve o resultado. Custo/trust sob a identidade do
    peer (consumer 'federation:<ws>'). Sem auth de USUÁRIO — a autenticação é o HMAC."""
    if not await federation_enabled():
        raise HTTPException(404)  # invisível quando desligada
    if not secret_key_present():
        # Fail-closed: sem master key, crypto cai no fallback inseguro → HMAC forjável.
        raise HTTPException(503, "Federação indisponível (MAESTRO_SECRET_KEY ausente)")

    # 0) cap de corpo ANTES do parse (DoS pré-auth — json.loads de corpo gigante é o custo)
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(413, "Corpo excede o limite")
    try:
        payload = json.loads(raw)
        envelope_dict = payload["envelope"]
        signature = payload["signature"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Body inválido (esperado {envelope, signature})")

    # 1) reconstrói o envelope (from_dict é TOTAL → ValueError vira 400, nunca 500)
    try:
        env = Envelope.from_dict(envelope_dict)
    except (ValueError, TypeError):
        raise HTTPException(400, "Envelope malformado")
    if not env.origin_workspace or not env.target_skill_urn or not isinstance(signature, str) or not signature:
        raise HTTPException(400, "Envelope incompleto (origin_workspace/target_skill_urn/signature)")

    # 2) AUTENTICA (HMAC do peer) ANTES de revelar qualquer coisa sobre alvos.
    #    Defensivo: qualquer erro de verificação vira 403 (nunca 500 pré-auth).
    try:
        peer = await fed.verify_inbound_envelope(env, signature)
    except Exception:
        logger.warning("federation_invoke: verify_inbound_envelope erro", exc_info=True)
        peer = None
    if not peer:
        raise HTTPException(403, "Peer desconhecido ou assinatura inválida")
    ws = env.origin_workspace

    # 3) anti-replay (só após autenticar): janela de tempo + nonce single-use
    if not fed.within_replay_window(env.created_at, datetime.utcnow()):
        raise HTTPException(401, "Fora da janela de replay (created_at)")
    if not await fed.check_and_record_nonce(env.envelope_id, ws):
        raise HTTPException(409, "Replay detectado")

    # 4) resolve o alvo — só capabilities EXPONÍVEIS (re-check TOCTOU); 404 indistinto
    entry = await fed.get_entry_by_urn(env.target_skill_urn)
    if not entry or not fed.is_federation_exposable(entry):
        raise HTTPException(404, "Capability não encontrada")

    # 5) resolver SELADO (snapshot obrigatório; root∈membros; sem fallback de mesh vivo)
    root, members = await fed.resolve_federated_exec(entry)
    if not root or not members:
        raise HTTPException(422, "Capability sem snapshot selável — não executável")

    # 6) input vem ASSINADO em context.user_input
    user_input = (env.context or {}).get("user_input")
    if not isinstance(user_input, str) or not user_input.strip():
        raise HTTPException(400, "context.user_input ausente/vazio")
    if len(user_input) > _MAX_INPUT_CHARS:
        raise HTTPException(413, "user_input excede o limite")

    # 7) executa SELADO sob a identidade do peer (reusa execução+custo+trust)
    consumer_id = f"federation:{ws}"
    execution = await create_execution(
        recipe_entry_id=entry["id"], consumer_user_id=consumer_id, input_text=user_input,
    )
    from app.catalog.executor import execute_pipeline_entry
    try:
        result = await asyncio.wait_for(
            execute_pipeline_entry(
                execution_id=execution["id"],
                pipeline_entry_id=entry["id"],
                root_agent_id=root,
                consumer_user={"id": consumer_id},
                user_input=user_input,
                allowed_agent_ids=members,  # SELA ao subgrafo do snapshot
            ),
            timeout=_EXEC_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Execução excedeu o tempo limite")

    rec = await get_execution(execution["id"]) or {}
    # Audit guardado (N2): a execução já produziu efeitos (row+custo+trust); uma
    # falha de audit NÃO pode virar 500 (o peer reexecutaria → custo dobrado).
    try:
        await audit_repo.create({
            "entity_type": "federation_invoke",
            "entity_id": entry["id"],
            "action": "invoked",
            "actor": consumer_id,
            "details": json.dumps({
                "peer_workspace": ws,
                "target_urn": env.target_skill_urn,
                "execution_id": execution["id"],
                "status": rec.get("status"),
                "envelope_id": env.envelope_id,
            }),
        })
    except Exception:
        logger.warning("federation_invoke: audit falhou (execução já efetivada)", exc_info=True)
    return {
        "execution_id": execution["id"],
        "status": rec.get("status"),
        "output": (result or {}).get("output", ""),
        "total_cost_usd": rec.get("total_cost_usd"),
        "total_latency_ms": rec.get("total_latency_ms"),
        "workspace": ws,
    }


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
