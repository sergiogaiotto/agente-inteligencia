"""Identidade de federação desta instância (PR8a — fundação da federação A2A).

Hoje todo URN do catálogo é mintado com workspace='default' (`app/catalog/urn.py`).
Para federação entre instâncias, cada instância precisa de uma IDENTIDADE de
workspace estável — o namespace que aparece nos seus URNs e que distingue
capabilities locais de remotas.

Fonte única de "quem sou eu": `local_workspace()`. Lê de `platform_settings`
(`federation.workspace`), valida o charset (mesmo de urn.py) e CAI PARA 'default'
em qualquer ambiguidade — nunca devolve um workspace inválido, senão `make_urn`
levantaria ValueError e quebraria a criação de entries.

Backward-compat: com a chave ausente (estado de hoje), `local_workspace()`
devolve 'default' e os URNs ficam idênticos ao comportamento pré-federação.

Rede e endpoints de federação são PRs seguintes (PR8b/PR8c); aqui só a fundação.
"""
from __future__ import annotations

import logging
import os
import re

from app.catalog.urn import DEFAULT_WORKSPACE
from app.core.database import settings_store

logger = logging.getLogger(__name__)

WORKSPACE_SETTING_KEY = "federation.workspace"
ENABLED_SETTING_KEY = "federation.enabled"

# Mesmo charset aceito por urn._URN_RE para o segmento <workspace>.
_WORKSPACE_RE = re.compile(r"^[a-z0-9-]+$")


def is_valid_workspace(ws: str) -> bool:
    """True se `ws` é um namespace de workspace sintaticamente válido."""
    return bool(ws) and bool(_WORKSPACE_RE.match(ws))


async def local_workspace() -> str:
    """Workspace desta instância (namespace dos URNs locais).

    Lê `federation.workspace` de platform_settings. Devolve 'default' quando a
    chave está ausente OU contém um valor inválido (com WARNING) — garante que o
    retorno é SEMPRE um workspace válido para `make_urn`.
    """
    try:
        raw = (await settings_store.get(WORKSPACE_SETTING_KEY, DEFAULT_WORKSPACE) or "").strip()
    except Exception as e:  # pool ausente em testes de unidade puros, etc.
        logger.debug("local_workspace: leitura de settings falhou (%s) — usando default", e)
        return DEFAULT_WORKSPACE
    if not raw:
        return DEFAULT_WORKSPACE
    if not is_valid_workspace(raw):
        logger.warning(
            "federation.workspace=%r é inválido (esperado %s) — usando '%s'",
            raw, _WORKSPACE_RE.pattern, DEFAULT_WORKSPACE,
        )
        return DEFAULT_WORKSPACE
    return raw


async def federation_enabled() -> bool:
    """True se a federação está ligada (`federation.enabled` truthy).

    Default OFF. Os endpoints de federação (PR8b+) gateiam por isto e devem
    FALHAR FECHADO se MAESTRO_SECRET_KEY não estiver setado (segredos de peer
    não podem usar o fallback inseguro de crypto.py)."""
    try:
        raw = (await settings_store.get(ENABLED_SETTING_KEY, "") or "").strip().lower()
    except Exception:
        return False
    return raw in ("1", "true", "yes", "on")


def secret_key_present() -> bool:
    """True se MAESTRO_SECRET_KEY está setado. A federação FALHA FECHADO sem ele:
    crypto.py cairia no fallback determinístico INSEGURO, e um segredo de peer
    cifrado com chave previsível tornaria o HMAC forjável. Os endpoints de
    federação (PR8b3+) devem 503 quando isto é False e a federação está ligada."""
    return bool(os.environ.get("MAESTRO_SECRET_KEY", "").strip())
