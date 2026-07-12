"""Teto global do corpo de requisição — anti-DoS de memória (API-6, CWE-400).

Middleware ASGI PURO que rejeita com 413 quando o header ``Content-Length``
excede o cap (``max_request_body_mb``) — ANTES de o corpo ser lido/parseado.
Sem este teto, um POST de corpo arbitrariamente grande no ``/invoke`` (ou em
qualquer rota) era buferizado inteiro em memória pelo Pydantic → OOM de um único
request.

Por que ASGI puro (e não BaseHTTPMiddleware): não embrulha a resposta nem
bufferiza o corpo — preserva o streaming (SSE do ``/invoke/stream``) e a leitura
em chunks dos uploads. Só inspeciona um header e devolve 413 no caminho de erro.

Limitação conhecida: cobre o caso realista (cliente/ferramenta declara o corpo
via ``Content-Length``). ``Transfer-Encoding: chunked`` sem Content-Length não é
barrado aqui — o parse do endpoint e os limites de anexo (5 × 10MB) contêm o
resto; uma checagem por streaming pode vir como evolução.
"""

from __future__ import annotations

import json


class RequestBodySizeLimitMiddleware:
    """Rejeita (413) requests cujo Content-Length excede o cap, antes de ler o corpo."""

    def __init__(self, app, max_bytes_getter):
        self.app = app
        self._max_bytes_getter = max_bytes_getter

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            cap = self._max_bytes_getter()
            if cap > 0:
                for name, value in scope.get("headers") or ():
                    if name == b"content-length":
                        try:
                            declared = int(value)
                        except (ValueError, TypeError):
                            break
                        if declared > cap:
                            await self._reject(send, cap)
                            return
                        break
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, cap: int) -> None:
        payload = json.dumps({
            "error": "request_too_large",
            "message": (
                f"Corpo da requisição excede o limite de {cap // (1024 * 1024)} MB. "
                "Reduza o payload (ex.: menos anexos) ou use upload dedicado."
            ),
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


def install_request_body_limit_middleware(app) -> None:
    """Registra o teto global de corpo. Cap lido de settings a cada request
    (runtime-tunável via max_request_body_mb)."""
    from app.core.config import get_settings

    app.add_middleware(
        RequestBodySizeLimitMiddleware,
        max_bytes_getter=lambda: get_settings().max_request_body_mb * 1024 * 1024,
    )
