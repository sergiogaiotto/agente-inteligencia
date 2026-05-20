"""Helpers HTTP compartilhados — auth headers + body preparation.

Extraído de `app/routes/api_connectors.py` e `app/agents/declarative_engine.py`
para eliminar duplicação. Toda chamada HTTP do projeto que precisa
construir auth ou body usa daqui.

Auth: 5 tipos suportados (none, api_key, bearer, basic, cookie).
Body: 5 tipos (json, form_urlencoded, multipart, text, xml).

Secrets cifrados (api_connectors.api_key) são descifrados aqui no momento
do uso — nunca circulam plaintext fora da memória do request.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

from app.core.crypto import decrypt_secret

logger = logging.getLogger(__name__)


# ─── Auth headers ────────────────────────────────────────────────


def build_auth_headers(connector: dict) -> dict[str, str]:
    """Constrói os headers de autenticação para uma chamada via connector.

    `connector` é o dict do api_connectors (com auth_type, auth_header,
    api_key). API key é descifrada aqui — nunca vaza plaintext em logs.

    Tipos suportados:
      none        → {}
      api_key     → {<auth_header>: <key>}  (default header X-API-Key)
      bearer      → {Authorization: Bearer <key>}
      basic       → {Authorization: Basic <b64(user:pass)>}
                    api_key deve estar no formato "user:pass" (plaintext)
      cookie      → {Cookie: <key>}  (sessão obtida via extract_cookie)
    """
    auth_type = (connector.get("auth_type") or "none").lower().strip()
    api_key_stored = connector.get("api_key") or ""
    api_key = decrypt_secret(api_key_stored) if api_key_stored else ""

    if auth_type == "none" or not api_key:
        return {}

    if auth_type == "api_key":
        header = (connector.get("auth_header") or "X-API-Key").strip()
        return {header: api_key}

    if auth_type == "bearer":
        return {"Authorization": f"Bearer {api_key}"}

    if auth_type == "basic":
        try:
            encoded = base64.b64encode(api_key.encode("utf-8")).decode("ascii")
        except Exception:
            encoded = ""
        return {"Authorization": f"Basic {encoded}"} if encoded else {}

    if auth_type == "cookie":
        return {"Cookie": api_key}

    logger.warning(f"build_auth_headers: auth_type desconhecido '{auth_type}'")
    return {}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mascara valores sensíveis em headers (para logs/audit)."""
    sensitive = {"authorization", "cookie", "x-api-key", "x-auth-token", "idempotency-key"}
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        out[k] = "***" if k.lower() in sensitive else v
    return out


# ─── Body preparation ────────────────────────────────────────────


BODY_TYPES = ("json", "form_urlencoded", "multipart", "text", "xml")


def prepare_request_body(
    body_type: str,
    body: Any,
    extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Prepara o body para httpx.AsyncClient.request(**kwargs).

    Retorna dict com kwargs httpx + headers ajustados para Content-Type.

    `body_type`:
      json            → {json: body}                  Content-Type: application/json (httpx auto)
      form_urlencoded → {data: body}                  Content-Type: application/x-www-form-urlencoded (httpx auto)
      multipart       → {files: <files>, data: <fields>} Content-Type: multipart/form-data (httpx auto)
                        body deve ter shape: {fields: {k: v, ...}, files: [{name, content, filename, content_type}, ...]}
      text            → {content: str(body)}          Content-Type: text/plain
      xml             → {content: str(body)}          Content-Type: application/xml
      desconhecido    → trata como json (fallback seguro com warning)
    """
    headers = dict(extra_headers or {})
    bt = (body_type or "json").strip().lower()

    if body is None or body == "" or body == {} or body == []:
        return {"headers": headers}

    if bt == "json":
        return {"json": body, "headers": headers}

    if bt == "form_urlencoded":
        if isinstance(body, str):
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            return {"content": body, "headers": headers}
        return {"data": body, "headers": headers}

    if bt == "multipart":
        if not isinstance(body, dict):
            logger.warning(f"prepare_request_body multipart: body inválido {type(body)}")
            return {"json": body, "headers": headers}
        fields = body.get("fields") or {}
        files_input = body.get("files") or []
        files: list[tuple] = []
        for f in files_input:
            if not isinstance(f, dict):
                continue
            name = f.get("name", "file")
            content = f.get("content", b"")
            if isinstance(content, str):
                content = content.encode("utf-8")
            filename = f.get("filename", name)
            content_type = f.get("content_type", "application/octet-stream")
            files.append((name, (filename, content, content_type)))
        kwargs: dict[str, Any] = {"headers": headers}
        if files:
            kwargs["files"] = files
        if fields:
            kwargs["data"] = fields
        return kwargs

    if bt == "text":
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        return {"content": str(body), "headers": headers}

    if bt == "xml":
        headers.setdefault("Content-Type", "application/xml; charset=utf-8")
        return {"content": str(body), "headers": headers}

    logger.warning(f"prepare_request_body: body_type desconhecido '{bt}' — usando json")
    return {"json": body, "headers": headers}
