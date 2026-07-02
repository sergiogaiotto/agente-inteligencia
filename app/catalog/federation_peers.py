"""Registro de PEERS de federação A2A (PR8b2).

Peers = outras instâncias confiáveis. Infra COMPARTILHADA entre as duas pontas:
- ingress (PR8b3) verifica o HMAC do envelope com o(s) shared_secret(s) do peer;
- egress (PR8c) assina + chama o `base_url` do peer.

Segredos compartilhados são CIFRADOS at-rest (crypto.py, prefixo `enc::`) — nunca
em plaintext no banco/dump/audit. Rotação com JANELA DE SOBREPOSIÇÃO: ao rodar,
o segredo atual vira `secret_prev`; o ingress aceita os DOIS durante a troca, então
envelopes em trânsito assinados com o antigo ainda verificam até a próxima rotação.

`verify_api_key` (auth de USUÁRIO) deliberadamente NÃO é reusado — peer é outro
domínio de confiança (instância, não pessoa).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime

from app.core.datetime_utils import naive_utc_now
from typing import Optional

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.database import federation_peers_repo
from app.core.federation_identity import is_valid_workspace, local_workspace

logger = logging.getLogger(__name__)

_PEER_SECRET_BYTES = 32  # 32 bytes urlsafe → ~43 chars de entropia


def generate_peer_secret() -> str:
    """Segredo compartilhado novo (plaintext — exibido UMA vez ao operador)."""
    return secrets.token_urlsafe(_PEER_SECRET_BYTES)


def validate_base_url(base_url: Optional[str]) -> Optional[str]:
    """Normaliza/valida o base_url (sanidade — a guarda SSRF profunda é egress/PR8c).
    None/'' → None. Sem esquema http(s) → ValueError."""
    if not base_url:
        return None
    u = base_url.strip()
    if not (u.startswith("https://") or u.startswith("http://")):
        raise ValueError("base_url deve começar com http:// ou https://")
    return u.rstrip("/")


async def register_peer(workspace: str, base_url: Optional[str] = None) -> tuple[dict, str]:
    """Registra um peer novo: gera + cifra o segredo. Devolve (row, plaintext).

    ValueError em workspace/base_url inválidos. Duplicidade de workspace sobe como
    exceção do banco (UNIQUE) — o caller mapeia para 409."""
    ws = (workspace or "").strip()
    if not is_valid_workspace(ws):
        raise ValueError(f"workspace inválido: {ws!r} (esperado [a-z0-9-]+)")
    if ws == await local_workspace():
        raise ValueError("não é possível registrar a própria instância como peer")
    url = validate_base_url(base_url)
    plaintext = generate_peer_secret()
    row = {
        "id": str(uuid.uuid4()),
        "workspace": ws,
        "base_url": url,
        "shared_secret": encrypt_secret(plaintext),
        "secret_prev": None,
        "status": "active",
    }
    await federation_peers_repo.create(row)
    return row, plaintext


async def get_active_peer_by_workspace(workspace: str) -> Optional[dict]:
    """Peer ATIVO pelo workspace, ou None (revogados não autenticam)."""
    if not workspace:
        return None
    rows = await federation_peers_repo.find_all(workspace=workspace, status="active", limit=1)
    return rows[0] if rows else None


def peer_secrets(peer: dict) -> list[str]:
    """Segredos plaintext válidos do peer (atual + anterior, janela de rotação).
    Decifra e filtra vazios. O ingress (PR8b3) verifica o HMAC contra qualquer um."""
    out: list[str] = []
    for col in ("shared_secret", "secret_prev"):
        enc = peer.get(col)
        if enc:
            dec = decrypt_secret(enc)
            if dec:
                out.append(dec)
    return out


async def list_peers() -> list[dict]:
    """Todos os peers (rows crus — o caller NUNCA serializa o segredo)."""
    return await federation_peers_repo.find_all(limit=500)


async def revoke_peer(peer_id: str) -> Optional[dict]:
    """Revoga (status='revoked'); não apaga (preserva audit). Devolve a row
    (p/ o caller auditar com o workspace) ou None se inexistente."""
    row = await federation_peers_repo.find_by_id(peer_id)
    if not row:
        return None
    await federation_peers_repo.update(
        peer_id, {"status": "revoked", "updated_at": naive_utc_now()}
    )
    return row


async def rotate_peer_secret(peer_id: str) -> Optional[tuple[dict, str]]:
    """Roda o segredo: atual → secret_prev, gera novo. Janela de sobreposição
    mantém envelopes em trânsito (assinados com o antigo) verificáveis até a
    próxima rotação. Devolve (row_atualizada, novo_plaintext) ou None se inexistente.

    Read-then-write (sem lock): aceitável — rotação é op de root, rara; duas
    rotações simultâneas do MESMO peer é cenário desprezível. (Atomicidade via
    UPDATE..RETURNING seria o upgrade, ao custo de testabilidade sem DB.)"""
    row = await federation_peers_repo.find_by_id(peer_id)
    if not row:
        return None
    plaintext = generate_peer_secret()
    await federation_peers_repo.update(peer_id, {
        "secret_prev": row.get("shared_secret"),
        "shared_secret": encrypt_secret(plaintext),
        "rotated_at": naive_utc_now(),
        "updated_at": naive_utc_now(),
    })
    updated = await federation_peers_repo.find_by_id(peer_id)
    return updated, plaintext
