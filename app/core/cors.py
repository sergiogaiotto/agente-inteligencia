"""CORS configurável por platform_settings (``cors_allowed_origins``).

Adicionado como middleware MAIS EXTERNO (curto-circuita o preflight ``OPTIONS``
ANTES do default-deny do ApiAuth — que devolvia 401 no preflight e bloqueava
todo frontend de browser). As origens são lidas DINAMICAMENTE de
``get_settings()`` a cada request → mudar em Configurações vale sem restart.

Invariantes de segurança:
- Allowlist VAZIA ⇒ inerte (nenhum header ``Access-Control-*``) ⇒ comportamento
  atual preservado (browsers cross-origin seguem bloqueados).
- NUNCA reflete origem fora da allowlist; NUNCA usa ``*``. Como a app também
  autentica por cookie de sessão, refletir origem arbitrária com credenciais
  seria CSRF-via-CORS — por isso a allowlist é EXPLÍCITA e por-origem.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings

# Headers que um frontend precisa MANDAR (a resposta ao preflight espelha o que
# o browser pediu, com este fallback). x-csrf-token cobre o fluxo por cookie.
_ALLOW_HEADERS_FALLBACK = "authorization, x-api-key, content-type, x-csrf-token"
# Headers que o JS do frontend precisa LER da resposta (senão o browser os esconde).
_EXPOSE_HEADERS = (
    "x-request-id, x-ratelimit-limit, x-ratelimit-remaining, "
    "x-ratelimit-reset, retry-after"
)
_ALLOW_METHODS = "GET, POST, PUT, DELETE, PATCH, OPTIONS"


def parse_allowed_origins(raw: str | None) -> set[str]:
    """CSV de origens → set normalizado (sem barra final). Vazio ⇒ set()."""
    raw = (raw or "").strip()
    if not raw:
        return set()
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


def origin_allowed(origin: str) -> bool:
    """True se ``origin`` está na allowlist atual (lida dinamicamente)."""
    if not origin:
        return False
    return origin.rstrip("/") in parse_allowed_origins(get_settings().cors_allowed_origins)


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """CORS por-request lendo a allowlist de get_settings() (runtime, sem restart)."""

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")
        allowed = origin_allowed(origin)

        # Preflight CORS: responde AQUI (antes de auth/rate-limit) quando a origem
        # é permitida. Preflight nunca carrega credencial — é mecânica do browser.
        if request.method == "OPTIONS" and request.headers.get(
            "access-control-request-method"
        ):
            if allowed:
                req_headers = request.headers.get(
                    "access-control-request-headers", _ALLOW_HEADERS_FALLBACK
                )
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": _ALLOW_METHODS,
                        "Access-Control-Allow-Headers": req_headers,
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Max-Age": "600",
                        "Vary": "Origin",
                    },
                )
            # Origem não permitida: segue o fluxo normal (auth/404). O browser
            # bloqueia por não haver Access-Control-Allow-Origin. Não vazamos CORS.
            return await call_next(request)

        response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Expose-Headers"] = _EXPOSE_HEADERS
            vary = response.headers.get("Vary")
            response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
        return response


def install_cors_middleware(app) -> None:
    """Registra o CORS. DEVE ser chamado por ÚLTIMO em main.py para ser o
    middleware mais externo (trata o preflight antes do ApiAuth)."""
    app.add_middleware(DynamicCORSMiddleware)
