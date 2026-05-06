"""Cifragem simétrica de segredos em repouso (Fernet/AES-128-CBC + HMAC).

Uso típico — proteger `tools.auth_token` no banco contra vazamento por
backups/dumps/replicas. Chave derivada do `SECRET_KEY` da aplicação via
PBKDF2-HMAC-SHA256, então a rotação do `SECRET_KEY` invalida todos os
tokens cifrados (intencional — força re-cadastro em incidente).

Estratégia de migração transparente:
- `encrypt(plain)` produz string com prefixo "fernet:" + base64.
- `read_secret(value)` detecta o prefixo: se presente, decifra; senão
  retorna como veio (compatibilidade com tokens em texto plano legados).
- Todo write novo via `write_secret(value)` sempre cifra.

Decisão por Fernet (cryptography stdlib) e não pgcrypto/KMS:
- Zero dependência de extensão Postgres ou serviço externo (VPS-friendly).
- Migração para KMS futura é só reimplementar `_get_fernet()`.
- Bytes a mais por valor (~80 chars cifrado vs ~32 plain) — aceitável.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Salt fixo: aceitável porque a entropia vem do SECRET_KEY (>=32 chars
# recomendado). Trocar o salt invalida todos os tokens cifrados.
_SALT = b"agente-inteligencia-secrets-v1"

# Prefixo que identifica valores cifrados — permite migração lazy.
_CIPHER_PREFIX = "fernet:"


def _derive_fernet_key(secret_key: str) -> bytes:
    """Deriva chave Fernet (32 bytes base64) de um SECRET_KEY arbitrário."""
    if not secret_key:
        raise ValueError("SECRET_KEY vazio — defina no .env antes de usar secrets.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=200_000,
    )
    raw = kdf.derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    settings = get_settings()
    return Fernet(_derive_fernet_key(settings.secret_key))


def encrypt(plaintext: str) -> str:
    """Cifra `plaintext` e retorna string com prefixo `fernet:`."""
    if not plaintext:
        return ""
    f = _get_fernet()
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_CIPHER_PREFIX}{token}"


def decrypt(ciphertext: str) -> str:
    """Decifra valor produzido por `encrypt()`. Vazio se inválido."""
    if not ciphertext or not ciphertext.startswith(_CIPHER_PREFIX):
        return ""
    f = _get_fernet()
    raw = ciphertext[len(_CIPHER_PREFIX):]
    try:
        return f.decrypt(raw.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.warning("Falha ao decifrar segredo — token inválido ou SECRET_KEY rotacionada.")
        return ""


def is_encrypted(value: Optional[str]) -> bool:
    """True se `value` aparenta ter sido produzido por `encrypt()`."""
    return bool(value) and value.startswith(_CIPHER_PREFIX)


def read_secret(value: Optional[str]) -> str:
    """Lê valor de uma coluna que pode estar cifrada ou em texto plano (legado).

    Compatibilidade transparente: tokens antigos em texto plano continuam
    funcionando. Novos writes via `write_secret()` sempre cifram.
    """
    if not value:
        return ""
    if is_encrypted(value):
        return decrypt(value)
    return value


def write_secret(plaintext: Optional[str]) -> str:
    """Cifra um plaintext para gravação no banco. Vazio passa direto."""
    if not plaintext:
        return ""
    return encrypt(plaintext)


def fingerprint(value: Optional[str]) -> str:
    """Hash curto não-reversível para logs/UI (substitui exibir o segredo)."""
    if not value:
        return ""
    raw = read_secret(value)  # se cifrado, hash o plaintext (consistente)
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
