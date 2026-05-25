"""Middleware HTTP que propaga request_id + trace_id + user_id via contextvars.

Cada request entrando na app:
1. Lê `X-Request-Id` (gerado pelo cliente/proxy) ou gera novo se ausente
2. Lê `X-Client-Trace-Id` (vindo do JS do frontend) — opcional, pra
   correlação end-to-end browser↔backend
3. Seta `request_id_var`, `trace_id_var`, `user_id_var` em contextvars
   — JsonFormatter automaticamente inclui nos logs do request
4. Loga request inicial (level DEBUG) e response (INFO) em `api.log` com
   método, path, status, duração
5. Adiciona `X-Request-Id` no response header (cliente pode usar pra
   suporte/troubleshooting)

Convenção dos IDs:
- request_id: `req_` + 12 chars hex (gerado servidor, único por request)
- trace_id (client): `cli_` + N chars (gerado JS, único por ação do user)
- correlation entre os 2 = ações do user → várias requests
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging_setup import (
    request_id_var,
    trace_id_var,
    user_id_var,
)

_logger = logging.getLogger("app.api")

_REQ_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _generate_request_id() -> str:
    """req_<12hex>. 12 hex = 48 bits → colisão é negligível."""
    return f"req_{secrets.token_hex(6)}"


def _validate_or_generate(header_val: str | None, prefix: str = "req") -> str:
    """Valida (alphanumeric + _- entre 4 e 64 chars) ou gera novo."""
    if header_val and _REQ_ID_PATTERN.match(header_val):
        return header_val
    return f"{prefix}_{secrets.token_hex(6)}"


def _resolve_user_id(request: Request) -> str:
    """Tenta extrair user_id do request sem fazer DB lookup pesado.

    Best-effort: usa cookie 'session' se houver (apenas o id, sem decode);
    OU header X-User-Id (em dev). Não chama require_user pra evitar
    overhead em endpoints públicos.
    """
    # Header explícito tem precedência (útil em integrações server-to-server)
    hdr_uid = request.headers.get("x-user-id")
    if hdr_uid:
        return hdr_uid[:64]
    # Cookie session: se existir, usa como dica (mesmo sem validar)
    sid = request.cookies.get("session")
    if sid:
        return f"sess:{sid[:8]}"
    return ""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware FastAPI que injeta request_id em contextvars + log da request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Resolve IDs
        rid = _validate_or_generate(request.headers.get("x-request-id"), "req")
        cli_trace = request.headers.get("x-client-trace-id")
        tid = cli_trace if (cli_trace and _REQ_ID_PATTERN.match(cli_trace)) else ""
        uid = _resolve_user_id(request)

        # Seta context vars (vai aparecer em TODOS os logs deste request)
        rid_tok = request_id_var.set(rid)
        tid_tok = trace_id_var.set(tid)
        uid_tok = user_id_var.set(uid)

        method = request.method
        path = request.url.path
        # Não loga health checks (poluição)
        is_noisy = path in ("/api/health", "/api/v1/infra/status")

        t0 = time.perf_counter()
        status_code = 500
        try:
            if not is_noisy:
                _logger.debug(
                    "request_received",
                    extra={
                        "event": "http.request",
                        "method": method,
                        "path": path,
                        "client_trace_id": tid,
                    },
                )
            response = await call_next(request)
            status_code = response.status_code
            # Echo request_id no response pro cliente poder reportar
            response.headers["X-Request-Id"] = rid
            return response
        except Exception as e:
            # Loga exceção não tratada
            _logger.exception(
                "request_unhandled_exception",
                extra={
                    "event": "http.exception",
                    "method": method,
                    "path": path,
                    "error_type": type(e).__name__,
                },
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            if not is_noisy:
                # 4xx = WARNING, 5xx = ERROR, 2xx/3xx = INFO
                if status_code >= 500:
                    level = logging.ERROR
                elif status_code >= 400:
                    level = logging.WARNING
                else:
                    level = logging.INFO
                _logger.log(
                    level,
                    "request_completed",
                    extra={
                        "event": "http.response",
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                    },
                )
            # Reset context vars (defensivo — concurrency)
            request_id_var.reset(rid_tok)
            trace_id_var.reset(tid_tok)
            user_id_var.reset(uid_tok)


def install_request_context_middleware(app: FastAPI) -> None:
    """Instala o middleware na app FastAPI. Chamar uma vez no startup."""
    app.add_middleware(RequestContextMiddleware)
