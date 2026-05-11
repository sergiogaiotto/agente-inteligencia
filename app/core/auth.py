"""Auth hardening — bcrypt para senhas + CSRF token (opt-in).

bcrypt: substituição de SHA256 com migração transparente.
- Hashes legados em SHA256 continuam VALIDANDO (compat).
- Em login bem-sucedido, se o hash era SHA256, é regravado em bcrypt.

CSRF: token aleatório em cookie + header. Validação opt-in via
`settings.csrf_required` para não quebrar frontend antes de adaptado.

Cookies: marcadores HttpOnly + SameSite + (Secure em prod) já aplicados.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from passlib.context import CryptContext

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Aceita bcrypt (preferido) e SHA256 hex (legado, deprecado).
# `deprecated="auto"` faz needs_update() retornar True para SHA256 → migra
# transparentemente no próximo login.
pwd_context = CryptContext(
    schemes=["bcrypt", "hex_sha256"],
    deprecated="auto",
    bcrypt__rounds=12,
)


def hash_password(password: str) -> str:
    """Hash seguro com bcrypt."""
    return pwd_context.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Valida senha contra hash (bcrypt OU sha256 legado)."""
    if not password or not stored_hash:
        return False
    try:
        return pwd_context.verify(password, stored_hash)
    except Exception as e:
        # Hash malformado / scheme desconhecido — registra e nega
        logger.warning(f"verify_password: falha ({type(e).__name__}: {e})")
        # Fallback puro para o exato formato hex SHA256 antigo (caso
        # CryptContext não reconheça por algum motivo)
        legacy = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(legacy, stored_hash)


def needs_rehash(stored_hash: str) -> bool:
    """True se o hash deve ser regravado (ex: SHA256 → bcrypt)."""
    try:
        return pwd_context.needs_update(stored_hash)
    except Exception:
        # Se não for um scheme conhecido, força migração
        return True


# ═══════════════════════════════════════════════════════════════
# CSRF token
# ═══════════════════════════════════════════════════════════════


def make_csrf_token() -> str:
    """Token aleatório de 32 bytes (b64url, ~43 chars)."""
    return secrets.token_urlsafe(32)


def verify_csrf(cookie_token: str, header_token: str) -> bool:
    """Compare-time-safe equality. Vazio em qualquer lado falha."""
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


# ═══════════════════════════════════════════════════════════════
# Helper para set_cookie consistente
# ═══════════════════════════════════════════════════════════════


def cookie_kwargs() -> dict:
    """Defaults seguros para `response.set_cookie(...)`."""
    s = get_settings()
    return {
        "httponly": True,
        "samesite": s.cookie_samesite,
        "secure": s.cookie_secure,
        "max_age": s.session_max_age_seconds,
    }


# ═══════════════════════════════════════════════════════════════
# Auth unificada — cookie OU X-API-Key (Depends pra endpoints)
# ═══════════════════════════════════════════════════════════════

from typing import Optional
from fastapi import HTTPException, Request


def _extract_api_key_from_headers(request: Request) -> Optional[str]:
    """X-API-Key direto, ou Authorization: Bearer ag_live_... (convenção)."""
    key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if key:
        return key.strip()
    authz = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        candidate = authz.split(None, 1)[1].strip()
        if candidate.startswith("ag_live_"):
            return candidate
    return None


async def require_user(request: Request) -> dict:
    """Auth obrigatória. 401 se nem cookie nem X-API-Key validarem.

    Convenção pra usar:
        @router.post("/algo")
        async def handler(user: dict = Depends(require_user)):
            uid = user["id"]
            ...

    Aceita 2 caminhos:
    1. Cookie `user_id` (UI/browser — comportamento atual).
    2. Header `X-API-Key: ag_live_...` (integração externa).
       Também `Authorization: Bearer ag_live_...` se o cliente preferir.

    Side-effect: quando X-API-Key é usado, popula request.state.api_key_id
    e .api_key_name pra audit log saber qual integração disparou.
    """
    from app.core.database import users_repo

    # Cookie path (UI)
    uid = request.cookies.get("user_id")
    if uid:
        user = await users_repo.find_by_id(uid)
        if user and user.get("status", "active") == "active":
            return {k: v for k, v in dict(user).items() if k != "password_hash"}

    # API key path (integração externa)
    api_key = _extract_api_key_from_headers(request)
    if api_key:
        from app.core.auth_apikey import verify_api_key
        key_record = await verify_api_key(api_key)
        if key_record:
            user = await users_repo.find_by_id(key_record["user_id"])
            if user and user.get("status", "active") == "active":
                request.state.api_key_id = key_record["id"]
                request.state.api_key_name = key_record["name"]
                return {k: v for k, v in dict(user).items() if k != "password_hash"}

    raise HTTPException(
        401,
        "Autenticação requerida — cookie de sessão ou header X-API-Key",
    )


async def require_user_optional(request: Request) -> Optional[dict]:
    """Igual a require_user mas devolve None em vez de 401."""
    try:
        return await require_user(request)
    except HTTPException:
        return None
