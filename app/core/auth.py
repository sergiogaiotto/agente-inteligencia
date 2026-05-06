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
