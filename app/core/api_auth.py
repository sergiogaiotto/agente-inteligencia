"""Default-deny de autenticação no data plane ``/api/v1/*`` (fail-closed).

Contexto (SKILL.md §2 — Broken Access Control): a plataforma tinha ~159 rotas
``/api/v1`` **sem** verificação de identidade no servidor. O único "gate" era o
redirect do frontend em ``frontend._render`` — que só protege o RENDER das
páginas HTML e é trivialmente contornável chamando a API diretamente
(``curl``/fetch). Endpoints sensíveis (invoke de LLM, proxy/SSRF, CRUD de
usuários/conectores/skills, PUT de settings/roteamento) ficavam abertos a
anônimos.

Este middleware inverte o default para **negar por padrão** (Princípio 5 do
SKILL.md — seguro por padrão / fail closed): todo ``/api/v1/*`` exige uma sessão
válida (cookie ASSINADO — ver ``app.core.auth.read_session_uid``) **ou** um
``X-API-Key`` válido, reusando exatamente a lógica de ``require_user``. Um
allowlist mínimo cobre os endpoints que são públicos por design.

Rotas fora de ``/api/v1/`` (páginas HTML, ``/static``, ``/api/health``,
``/.well-known/*``) NÃO são tocadas aqui — as páginas já se auto-protegem via
redirect e os demais são públicos por natureza.
"""
from __future__ import annotations

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_GUARDED_PREFIX = "/api/v1/"

# (MÉTODO, path exato) públicos por design. Mantido MÍNIMO e explícito.
#  - login/logout/check-setup/me: fluxo de autenticação e bootstrap do frontend
#    (``/me`` devolve {"user": null} para anônimo — o front depende disso).
#  - POST /api/v1/users: criação do 1º usuário (count==0 → root). Quando já há
#    usuários, o próprio handler exige um caller autenticado (self-enforce), então
#    liberar aqui não abre buraco.
#  - POST /api/v1/federation/invoke: ingress federado, autenticado por ASSINATURA
#    de peer (não por cookie/API-key) — gatear com require_user quebraria a
#    federação. Sua própria camada valida a assinatura e falha fechada.
_PUBLIC: set[tuple[str, str]] = {
    ("POST", "/api/v1/users/login"),
    ("POST", "/api/v1/users/logout"),
    ("GET", "/api/v1/users/check-setup"),
    ("GET", "/api/v1/users/me"),
    ("POST", "/api/v1/users"),
    ("POST", "/api/v1/federation/invoke"),
}


def is_public_api_path(method: str, path: str) -> bool:
    """True se ``(method, path)`` é público por design (dispensa autenticação)."""
    return (method.upper(), path) in _PUBLIC


def requires_auth(method: str, path: str) -> bool:
    """True se a rota está sob o gate default-deny (``/api/v1/*`` não-allowlistado)."""
    if not path.startswith(_GUARDED_PREFIX):
        return False
    return not is_public_api_path(method, path)


class ApiAuthMiddleware(BaseHTTPMiddleware):
    """Exige autenticação para todo ``/api/v1/*`` fora do allowlist."""

    async def dispatch(self, request: Request, call_next):
        if requires_auth(request.method, request.url.path):
            # Import tardio: evita ciclo no import do módulo (auth → database).
            from app.core.auth import require_user

            try:
                user = await require_user(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            # Cache best-effort: se o scope propagar (Starlette backing em
            # scope["state"]), o Depends(require_user) do endpoint reusa e evita
            # um 2º lookup. Se não propagar, require_user apenas re-executa —
            # correção preservada em qualquer caso.
            request.state.auth_user = user
        return await call_next(request)


def install_api_auth_middleware(app) -> None:
    app.add_middleware(ApiAuthMiddleware)
