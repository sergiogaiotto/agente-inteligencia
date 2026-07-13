"""API key authentication — geração, hash, validação.

Padrão `X-API-Key: ag_live_<32_chars_random>` ou `Authorization: Bearer <same>`.
A plaintext só existe no momento da criação (mostrada UMA vez na UI/response);
o banco só guarda SHA-256.

Uso comum:
- `generate_api_key()` cria nova key + retorna (plaintext, prefix, hash) pra
  caller persistir o hash/prefix e devolver a plaintext ao operador.
- `verify_api_key(plaintext)` busca em api_keys, valida que não está revogada
  nem expirada, retorna o `user_id` dono ou None.

Race conditions: `last_used_at` é atualizado em background (fire-and-forget),
não bloqueia a request. Concorrência aceita perder updates ocasionais — campo
é só informativo pra rotação/diag.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Identifica visualmente que é uma key do nosso app (não OpenAI/Anthropic).
# "live" deixa espaço pro futuro `ag_test_` em sandbox sem mudar o parser.
_KEY_PREFIX = "ag_live_"
_KEY_RANDOM_BYTES = 24  # 24 bytes → 32 chars urlsafe-b64. Total: 8 + 32 = 40 chars.


def generate_api_key() -> tuple[str, str, str]:
    """Gera nova key + retorna (plaintext, prefix_para_UI, hash_para_banco).

    plaintext: o que o operador copia (única chance de ver).
    prefix_para_UI: primeiros ~12 chars visíveis pra reconhecer a key na lista
                    (ex: "ag_live_a1b2"). Não basta pra autenticar.
    hash_para_banco: SHA-256 hex (64 chars) — único índice de busca.
    """
    random_part = secrets.token_urlsafe(_KEY_RANDOM_BYTES)
    plaintext = f"{_KEY_PREFIX}{random_part}"
    prefix_for_ui = plaintext[:12]  # "ag_live_aBc1"
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, prefix_for_ui, key_hash


def hash_api_key(plaintext: str) -> str:
    """SHA-256 de uma key plaintext. Usado pra validação."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def verify_api_key(plaintext: str) -> Optional[dict]:
    """Valida uma key plaintext contra o banco.

    Retorna o registro de api_keys (incluindo user_id) se válida e ativa.
    Caso contrário retorna None — sem distinguir entre "não existe", "revogada"
    e "expirada" (evita info leak; logs internos guardam a razão).

    Side-effect: atualiza last_used_at de forma fire-and-forget (não bloqueia
    a request).
    """
    if not plaintext or not plaintext.startswith(_KEY_PREFIX):
        return None
    from app.core.database import _get_pool
    key_hash = hash_api_key(plaintext)
    try:
        pool = _get_pool()
    except Exception:
        return None
    async with pool.acquire() as con:
        # SELECT * (não colunas explícitas): resiliente se o escopo por-key
        # (allowed_pipeline_ids/read_only) ainda não migrou num DB — o auth por
        # key NÃO pode quebrar por uma coluna nova ausente (Alembic é fail-open).
        row = await con.fetchrow(
            "SELECT * FROM api_keys WHERE key_hash = $1",
            key_hash,
        )
        if not row:
            return None
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.utcnow():
            return None
        # fire-and-forget update do last_used_at — não bloqueia auth
        import asyncio
        async def _bump():
            try:
                async with pool.acquire() as c:
                    await c.execute(
                        "UPDATE api_keys SET last_used_at = now() WHERE id = $1",
                        row["id"],
                    )
            except Exception as e:
                logger.debug(f"api_keys last_used_at update falhou: {e}")
        asyncio.create_task(_bump())
        return dict(row)
