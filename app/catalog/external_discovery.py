"""Discovery de Plataforma Externa por URL (PR7) — "tornar acessível".

Recebe uma base_url e tenta DETECTAR o tipo de plataforma, pré-preenchendo a
config de conexão (adapter). Reaproveita a guarda SSRF, o cliente httpx isolado
de external_probe e os helpers de auth.

Detecta hoje:
- OpenAI-compatível: GET {base}/v1/models → lista modelos (200) ou auth-required
  (401/403). Sugere mode=openai_chat, path=/v1/chat/completions, model=1º da lista.
- Instância Maestro (federação): GET {base}/.well-known/maestro-federation.json
  com 'capabilities' → informa workspace + nº de capabilities (candidato a peer).

Fail-soft: cada probe é isolado; nunca levanta. Devolve sempre um dict
{base_url, detected: [...], suggested: {...}|None, error: str|None}.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from app.catalog.external_probe import _http_request
from app.core.http_auth import build_auth_headers
from app.core.ssrf import SSRFError, validate_public_url

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10.0


def _parse_openai_models(raw: bytes) -> list[str]:
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for m in items:
        if isinstance(m, dict) and m.get("id"):
            out.append(str(m["id"]))
    return out


async def discover(base_url: str, *, secret: str = "", allow_http: bool = False) -> dict:
    """Detecta o tipo da plataforma externa a partir da base_url. Nunca levanta."""
    base = (base_url or "").strip().rstrip("/")
    result: dict = {"base_url": base, "detected": [], "suggested": None, "error": None}

    if not base:
        result["error"] = "base_url vazia"
        return result
    try:
        validate_public_url(base, allow_http=allow_http)
    except SSRFError as e:
        result["error"] = f"URL bloqueada pela guarda SSRF: {e}"
        return result

    bearer = build_auth_headers({"auth_type": "bearer", "api_key": secret}) if secret else {}

    # 1) OpenAI-compatível — /v1/models
    try:
        status, raw = await _http_request(
            "GET", base + "/v1/models", headers=bearer, json_body=None, timeout_s=_TIMEOUT_S
        )
        if status == 200:
            models = _parse_openai_models(raw)
            result["detected"].append({
                "type": "openai_compatible",
                "detail": f"{len(models)} modelo(s) disponíveis",
                "models": models[:20],
            })
            result["suggested"] = {
                "mode": "openai_chat",
                "path": "/v1/chat/completions",
                "auth_type": "bearer",
                "model": models[0] if models else None,
            }
        elif status in (401, 403):
            result["detected"].append({
                "type": "openai_compatible",
                "detail": f"endpoint OpenAI-compatível (auth requerida — HTTP {status})",
                "models": [],
            })
            result["suggested"] = {
                "mode": "openai_chat",
                "path": "/v1/chat/completions",
                "auth_type": "bearer",
                "model": None,
            }
    except Exception as e:  # noqa: BLE001 — fail-soft por probe
        logger.debug("discover openai probe falhou: %s", e)

    # 2) Instância Maestro (federação)
    try:
        status, raw = await _http_request(
            "GET", base + "/.well-known/maestro-federation.json",
            headers={}, json_body=None, timeout_s=_TIMEOUT_S,
        )
        if status == 200:
            try:
                data = json.loads(raw or b"{}")
            except (ValueError, TypeError):
                data = {}
            if isinstance(data, dict) and isinstance(data.get("capabilities"), list):
                result["detected"].append({
                    "type": "maestro_federation",
                    "detail": (
                        f"instância Maestro (workspace '{data.get('workspace')}', "
                        f"{len(data['capabilities'])} capabilities) — candidata a peer de federação"
                    ),
                })
    except Exception as e:  # noqa: BLE001
        logger.debug("discover federation probe falhou: %s", e)

    if not result["detected"]:
        result["error"] = "nenhum tipo conhecido detectado (OpenAI-compatível ou Maestro)"
    return result
