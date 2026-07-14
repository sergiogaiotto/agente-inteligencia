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

from typing import Optional

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


# ── P0: Contenção de privilégio da API Key ──────────────────────────────────
# Uma X-API-Key herda a IDENTIDADE do usuário dono (e o ROLE). Sem isto, uma key
# entregue a um frontend externo alcança TODA a superfície autenticada
# (~235 rotas): lê PII de /users, cunha novas keys, muda /settings, CRUD de
# agents/skills. Aqui limitamos o que um principal-via-key pode alcançar.

# SEMPRE negados a um principal-via-key (escalação / leitura de segredo). Não há
# uso legítimo de INTEGRAÇÃO que precise cunhar credenciais, ler/gravar settings
# da plataforma ou gerir usuários — isso é superfície de ADMIN (só cookie/UI).
_APIKEY_ALWAYS_DENY_PREFIXES = (
    "/api/v1/api-keys",   # cunhar/revogar/listar credenciais (persistência/escala)
    "/api/v1/settings",   # ler segredos + mudar config/roteamento da plataforma
    "/api/v1/users",      # PII de todos + gestão de usuários (guarded; /me é público)
    "/api/v1/domains",    # gestão de domínios/tenancy
)


def _is_escalation_path(path: str) -> bool:
    return any(
        path == p or path.startswith(p + "/") for p in _APIKEY_ALWAYS_DENY_PREFIXES
    )


def _is_public_surface(method: str, path: str) -> bool:
    """Superfície PÚBLICA de integração: descoberta + invoke de pipelines.

    Um frontend externo só precisa disto. GET em /pipelines/* (list/detail/
    inputs-schema/jobs) e POST no invoke[/stream|/async]. Criar/mutar pipeline
    NÃO entra. O GET /pipelines/{id}/jobs/{job_id} (polling do 202) já passa
    pelo ramo GET — a key que criou o job consegue consultá-lo.
    """
    if path.startswith("/api/v1/pipelines"):
        m = method.upper()
        if m == "GET":
            return True
        if m == "POST" and (
            path.endswith("/invoke")
            or path.endswith("/invoke/stream")
            or path.endswith("/invoke/async")
        ):
            return True
    return False


def apikey_route_denied(method: str, path: str) -> Optional[str]:
    """Retorna o MOTIVO (str) se um principal-via-key NÃO pode alcançar a rota;
    None se pode. Escalação é SEMPRE negada; a restrição 'só superfície pública'
    é opt-in via setting api_key_public_surface_only (default OFF)."""
    if _is_escalation_path(path):
        return "escalation_or_secret_route"
    from app.core.config import get_settings
    if get_settings().api_key_public_surface_only and not _is_public_surface(method, path):
        return "public_surface_only_enabled"
    return None


def requires_auth(method: str, path: str) -> bool:
    """True se a rota está sob o gate default-deny (``/api/v1/*`` não-allowlistado)."""
    if not path.startswith(_GUARDED_PREFIX):
        return False
    # Preflight CORS (OPTIONS) NUNCA carrega credencial — é mecânica do browser.
    # Isentar aqui evita 401 no preflight; o CORS middleware (mais externo) já
    # responde os preflights de origens permitidas antes de chegar aqui.
    if method.upper() == "OPTIONS":
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
            # P0: contenção de privilégio quando o principal veio por API Key.
            # require_user popula request.state.api_key_id no caminho da key.
            if getattr(request.state, "api_key_id", None):
                reason = apikey_route_denied(request.method, request.url.path)
                if reason:
                    return JSONResponse(
                        {"detail": {
                            "error": "api_key_forbidden_route",
                            "reason": reason,
                            "hint": "API keys só acessam descoberta + invoke de pipelines; "
                                    "gestão de credenciais/settings/usuários é só pela UI.",
                        }},
                        status_code=403,
                    )
        return await call_next(request)


def install_api_auth_middleware(app) -> None:
    app.add_middleware(ApiAuthMiddleware)
