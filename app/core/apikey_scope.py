"""Enforcement do escopo por API-key (Onda 6, 33.17.0).

Uma API key invocava QUALQUER pipeline (over-privilege). Este gate lê o escopo
que o `require_user` carimba em `request.state.api_key_scope` (só quando o
principal é uma API-key; cookie/UI = sem escopo → sem restrição) e barra ON-PATH:

- `read_only`  → a key não invoca nada (403), só lê/descobre.
- `allowed_pipeline_ids` (JSON array) → a key só invoca os pipelines listados;
  NULL/[] = todos (comportamento atual). Aplicado onde há pipeline_id (invoke de
  pipeline). Sessão de UI (cookie) nunca é restrita.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def _parse_allowed(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def assert_api_key_can_invoke(request: Request, pipeline_id: Optional[str] = None) -> None:
    """Barra o invoke conforme o escopo da API-key. No-op p/ cookie/UI (sem escopo).

    - read_only → 403 sempre.
    - allowed_pipeline_ids não-vazio + pipeline_id fora da lista → 403.
    """
    scope = getattr(request.state, "api_key_scope", None)
    if not scope:      # não é API-key (cookie) → sem restrição
        return
    if scope.get("read_only"):
        logger.info(
            "apikey_scope.readonly_blocked",
            extra={"event": "security.apikey_readonly_blocked",
                   "api_key_id": getattr(request.state, "api_key_id", None)},
        )
        raise HTTPException(403, "Esta API key é somente-leitura (read_only) — invoke não permitido.")
    allowed = _parse_allowed(scope.get("allowed_pipeline_ids"))
    if allowed and pipeline_id is not None and pipeline_id not in allowed:
        logger.info(
            "apikey_scope.pipeline_blocked",
            extra={"event": "security.apikey_pipeline_blocked",
                   "api_key_id": getattr(request.state, "api_key_id", None),
                   "pipeline_id": pipeline_id},
        )
        raise HTTPException(403, "Esta API key não está autorizada a invocar este pipeline.")
