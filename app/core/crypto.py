"""Cifra simétrica para secrets at-rest (API keys, etc.).

Usa Fernet (AES-128-CBC + HMAC-SHA256) com chave derivada via PBKDF2
de uma master key em env `MAESTRO_SECRET_KEY`. Sem master key, usa
fallback determinístico (insecure — só pra dev local; loga WARNING).

Backward compat: valores sem prefixo `enc::` são tratados como plaintext
legacy e retornados direto. Quando você reescrever via UI, o valor é
cifrado e ganha o prefixo. Permite rollout gradual sem dump+restore.

Caso de uso: api_connectors.api_key. Não rola plaintext em backups,
audit_log ou dump SQL — sempre cifrado a partir do primeiro save.
"""

from __future__ import annotations

import base64
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc::"
_SALT = b"maestro-secret-salt-v1"  # fixo: não muda entre boots; HMAC sai da master key
_ITERATIONS = 100_000


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Constrói Fernet a partir da master key. Cacheado por processo."""
    master = os.environ.get("MAESTRO_SECRET_KEY", "").strip()
    if not master:
        # SEC-02: em PRODUÇÃO, jamais cair no fallback determinístico — ele torna
        # os segredos at-rest recuperáveis por qualquer um com o dump. Falhe-
        # fechado (defesa em profundidade: o boot guard de main.py já barra o
        # startup; isto barra também qualquer chamada direta que o contorne).
        try:
            from app.core.config import is_production
            _prod = is_production()
        except Exception:
            _prod = False
        if _prod:
            raise RuntimeError(
                "MAESTRO_SECRET_KEY ausente em produção — cifra de segredos "
                "at-rest indisponível (o fallback determinístico é inseguro). "
                "Defina MAESTRO_SECRET_KEY e reinicie."
            )
        # Em dev, fallback determinístico — INSEGURO, mas evita crash local.
        logger.warning(
            "MAESTRO_SECRET_KEY não setado — usando fallback determinístico "
            "INSEGURO. Configure no .env antes de produção."
        )
        master = "maestro-dev-insecure-fallback-key"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(master.encode("utf-8")))
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """Cifra um secret. Retorna string com prefixo `enc::` para identificação.

    Valor vazio é retornado como '' (não cifra — sentinel "sem secret").
    Valor já cifrado (começa com `enc::`) é retornado como está (idempotente).
    """
    if not plaintext:
        return ""
    if plaintext.startswith(_ENC_PREFIX):
        return plaintext  # já cifrado
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return _ENC_PREFIX + token.decode("ascii")


def decrypt_secret(stored: str) -> str:
    """Descifra um valor armazenado.

    - Vazio → ''
    - Começa com `enc::` → descifra
    - Caso contrário → retorna como está (plaintext legacy, retrocompat)

    Token inválido (chave trocada, dado corrompido) → '' + WARNING.
    Evita crash; caller decide o que fazer com secret faltante.
    """
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored  # plaintext legacy
    token = stored[len(_ENC_PREFIX):].encode("ascii")
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        logger.warning(
            "decrypt_secret: token inválido (chave trocada ou dado corrompido). "
            "Retornando '' — caller deve tratar como secret ausente."
        )
        return ""
    except Exception as e:
        logger.warning(f"decrypt_secret: erro inesperado {type(e).__name__}: {e}")
        return ""


def is_encrypted(stored: str) -> bool:
    """True se o valor está no formato cifrado (prefixo enc::)."""
    return bool(stored) and stored.startswith(_ENC_PREFIX)
