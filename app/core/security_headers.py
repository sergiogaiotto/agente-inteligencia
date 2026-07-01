"""Cabeçalhos de segurança no nível do app (SKILL.md §7 — defesa em profundidade).

O Caddy (infra/caddy/Caddyfile) só cobre parte dos headers e só existe no
caminho de produção; o app em si não emitia NENHUM header de segurança. Assim,
qualquer deploy sem Caddy (ou atrás de outro proxy) ficava exposto a clickjacking
e MIME-sniffing. Este middleware garante uma baseline **independente do proxy**
(Princípio 4 do SKILL.md).

Usa ``setdefault`` — não sobrescreve o que a app ou o proxy já tenham definido.

Nota sobre CSP: definimos apenas as diretivas que NÃO quebram o frontend atual
(``frame-ancestors``/``object-src``/``base-uri``). Um ``script-src`` restritivo
exige migrar os scripts inline (Alpine + blocos <script>) para nonces — planejado
como evolução (ver docs/security-audit); fazê-lo agora quebraria a UI.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_CSP = "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        h.setdefault("Content-Security-Policy", _CSP)
        # HSTS só sob HTTPS (atrás do Caddy/TLS). Em http (dev) NÃO enviamos —
        # HSTS é sticky no browser e travaria o acesso local.
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        if proto == "https":
            h.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


def install_security_headers_middleware(app) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
