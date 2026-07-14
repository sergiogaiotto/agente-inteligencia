"""Maestro — Aplicação principal FastAPI."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.core.config import get_settings, is_production
from app.core.version import APP_VERSION
from app.core.database import init_db, close_db
from app.core.otel import init_otel
from app.routes import agents, skills, workspace, mesh, dashboard, frontend, wizard, users, pipelines, federation
from app.routes.api_connectors import router as api_connectors_router
from app.routes.mcp_diagnostics import router as mcp_diagnostics_router
from app.routes.skill_dryrun import router as skill_dryrun_router
from app.routes.help import router as help_router
from app.routes.infra import router as infra_router
from app.routes.api_keys import router as api_keys_router
from app.routes.catalog import router as catalog_router
from app.routes.data_tables import router as data_tables_router
from app.routes.logs_admin import router as logs_admin_router
from app.routes.db_health import router as db_health_router
from app.routes.playground import router as playground_router
from app.routes.privacy import router as privacy_router

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Logging estruturado (JSON em logs/*.log) — antes de tudo pra capturar
    # qualquer erro de startup.
    try:
        from app.core.logging_setup import setup_logging
        setup_logging()
    except Exception as e:
        # Não derruba app se logging falhar (defensivo)
        print(f"WARN: setup_logging falhou: {e}", flush=True)
    # SEC-02: falha-fecha o boot se app_env=produção com default inseguro
    # (SECRET_KEY 'change-me', MAESTRO_SECRET_KEY ausente, COOKIE_SECURE=false).
    # ANTES de init_db — um prod mal configurado não deve nem conectar ao banco.
    # No-op em dev/staging.
    from app.core.config import assert_secure_production_posture
    assert_secure_production_posture()
    await init_db()
    # Após pool aberto, popula os.environ com overrides do settings_store
    # (UI gravou via PUT /settings em sessões anteriores). Sem isso, providers
    # e embedder leem só do .env e ignoram o que o operador editou na página
    # de Configurações. cache_clear de get_settings() já está dentro.
    try:
        from app.core.config import apply_settings_to_env
        applied = await apply_settings_to_env()
        if applied:
            logger.info(f"Settings UI override aplicados: {applied} env vars do banco")
    except Exception as e:
        logger.warning(f"apply_settings_to_env falhou no startup: {e}")
    # Estúdio de Pipelines (PR1): migra mesh_groups → pipelines (idempotente,
    # guardada por flag). Nunca derruba o startup.
    try:
        from app.core.database import migrate_mesh_groups_to_pipelines
        res = await migrate_mesh_groups_to_pipelines()
        if not res.get("skipped"):
            logger.info(f"Pipelines: migração mesh_groups→pipelines {res}")
    except Exception as e:
        logger.warning(f"migrate_mesh_groups_to_pipelines falhou no startup: {e}")
    # Fila de juiz DURÁVEL (Onda 6): re-despacha os verifier_jobs 'pending' e os
    # 'running' órfãos (juiz async que ficou do processo anterior — crash/restart;
    # a fila em memória não sobrevivia). DEPOIS do init_db (pool aberto) e ANTES
    # de servir → single-flight, sem double-processing. Nunca derruba o boot.
    try:
        from app.verifier.async_dispatcher import resume_jobs
        from app.core.config import get_settings as _gs
        resumed = await resume_jobs(batch=_gs().verifier_max_concurrent_jobs)
        if resumed:
            logger.info(f"verifier_jobs: {resumed} job(s) do juiz re-despachado(s) no boot")
    except Exception as e:
        logger.warning(f"verifier resume_jobs falhou no startup: {e}")
    # Invoke assíncrono 202 (Onda 6, 34.0.0): 'running' órfão → 'lost' (invoke
    # NUNCA re-executa às cegas — custo LLM + efeitos colaterais), 'queued' →
    # retoma; e sobe o reaper (retenção + despacho de fila — o 1º loop periódico
    # do app; cancelado no shutdown ANTES do close_db). Nunca derruba o boot.
    try:
        from app.core.invoke_jobs import resume_invoke_jobs
        rj = await resume_invoke_jobs()
        if rj.get("lost") or rj.get("dispatched"):
            logger.info(f"invoke_jobs no boot: {rj}")
    except Exception as e:
        logger.warning(f"invoke_jobs resume falhou no startup: {e}")
    # Reaper em try PRÓPRIO (review): uma falha no resume não pode deixar o
    # processo inteiro sem retenção/despacho até o próximo restart.
    try:
        from app.core.invoke_jobs import start_reaper
        start_reaper()
    except Exception as e:
        logger.warning(f"invoke_jobs reaper falhou no startup: {e}")
    try:
        yield
    finally:
        # Invoke async: cancela o reaper e marca jobs ainda ativos como 'lost'
        # (o cliente não fica pollando um 'running' que nunca vai terminar).
        # ANTES do close_db — o mark escreve no pool.
        try:
            from app.core.invoke_jobs import shutdown_invoke_jobs
            await shutdown_invoke_jobs(timeout=5.0)
        except Exception as e:
            logger.warning(f"invoke_jobs shutdown falhou: {e}")
        # Drena tasks async do verifier antes de fechar o pool — evita
        # erros em INSERT contra pool já fechado quando shutdown pega
        # uma task de production sample no meio.
        try:
            from app.verifier.async_dispatcher import drain
            await drain(timeout=5.0)
        except Exception as e:
            logger.warning(f"verifier drain falhou no shutdown: {e}")
        # Drena as escritas de analytics do invoke (auditoria/atribuição/débito
        # fire-and-forget) antes de fechar o pool — mesma razão do verifier.
        try:
            from app.routes.pipelines import drain_invoke_analytics
            n = await drain_invoke_analytics(timeout=5.0)
            if n:
                logger.info(f"invoke analytics drenados no shutdown: {n}")
        except Exception as e:
            logger.warning(f"invoke analytics drain falhou no shutdown: {e}")
        await close_db()

settings = get_settings()


# ── OpenAPI/docs (API-4 + API-5) ───────────────────────────────
# API-4: o schema OpenAPI expõe a versão REAL do produto (APP_VERSION), não mais
# um "2.0.0" estático que induzia a erro sobre o que está no ar.
# API-5: em produção, /docs + /redoc + /openapi.json ficam ATRÁS de admin — sem
# isso, a superfície inteira da API (todas as rotas + schemas) era enumerável
# anonimamente (recon). Em dev ficam abertos. Como o local roda app_env=production,
# GATEAMOS por admin (não desligamos) — o operador logado ainda abre o /docs.
def _install_protected_docs(application: FastAPI, app_name: str) -> None:
    """API-5: serve /docs /redoc /openapi.json só para root/admin autenticado."""
    from fastapi import Depends
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
    from app.core.auth import require_role

    guard = require_role("root", "admin")

    @application.get("/openapi.json", include_in_schema=False)
    async def _protected_openapi(_user: dict = Depends(guard)):
        return application.openapi()

    @application.get("/docs", include_in_schema=False)
    async def _protected_docs(_user: dict = Depends(guard)):
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app_name} — API")

    @application.get("/redoc", include_in_schema=False)
    async def _protected_redoc(_user: dict = Depends(guard)):
        return get_redoc_html(openapi_url="/openapi.json", title=f"{app_name} — API")


def build_app(app_settings, *, lifespan_fn=lifespan) -> FastAPI:
    """Constrói o FastAPI com a versão real (API-4) e, em produção, o /docs
    trancado por admin (API-5). Fatorado para ser testável sem depender do env
    no momento do import (o app do módulo é construído uma vez, abaixo)."""
    locked = is_production(app_settings)
    kwargs = dict(
        title=app_settings.app_name,
        description="Plataforma Multi-Agente §SKILL.md sobre AI Mesh",
        version=APP_VERSION,
        lifespan=lifespan_fn,
    )
    if locked:
        kwargs.update(docs_url=None, redoc_url=None, openapi_url=None)
    application = FastAPI(**kwargs)
    if locked:
        _install_protected_docs(application, app_settings.app_name)
    return application


app = build_app(settings)

# ── Observabilidade (Onda 2) ───────────────────────────────────
# Inicializa OpenTelemetry ANTES dos middlewares para que requests sejam
# instrumentadas desde o primeiro byte. No-op se OTEL_ENABLED=false.
init_otel(app)

# ── Request context (logs/Observabilidade) ─────────────────────
# Middleware que propaga request_id + trace_id via contextvars; JsonFormatter
# inclui esses IDs em TODOS os logs do ciclo do request. Loga req/resp em
# logs/api.log. Adiciona X-Request-Id no response.
from app.core.request_context import install_request_context_middleware
install_request_context_middleware(app)

# ── Middlewares de segurança (Onda 1) ──────────────────────────
# Cabeçalhos de segurança baseline no nível do app (independentes do proxy):
# X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy,
# CSP (frame-ancestors/object-src/base-uri) e HSTS sob HTTPS.
from app.core.security_headers import install_security_headers_middleware
install_security_headers_middleware(app)

# Autenticação default-deny no data plane /api/v1/* (fail-closed): todo endpoint
# sob /api/v1 exige sessão assinada OU X-API-Key, salvo allowlist mínimo
# (login/logout/me/check-setup, bootstrap do 1º usuário, ingress federado).
# Registrado ANTES do RateLimit para que o RateLimit (adicionado depois) seja o
# mais externo e rejeite floods com 429 antes do trabalho de auth.
from app.core.api_auth import install_api_auth_middleware
install_api_auth_middleware(app)

# Rate-limit (sliding window via Redis com fallback memory) — defesa LLM04.
from app.core.ratelimit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)

# Teto global de corpo (API-6): rejeita 413 por Content-Length antes de ler o
# corpo — anti-OOM de um único request grande. Registrado antes do CORS (fica
# INTERNO a ele) para que o 413 saia com os headers CORS.
from app.core.request_limits import install_request_body_limit_middleware
install_request_body_limit_middleware(app)

# CORS (P0 — frontends externos no browser). Registrado por ÚLTIMO = middleware
# MAIS EXTERNO: trata o preflight OPTIONS ANTES do ApiAuth (que devolvia 401 no
# preflight). Origens lidas dinamicamente de platform_settings (cors_allowed_origins);
# allowlist vazia = inerte (comportamento atual).
from app.core.cors import install_cors_middleware
install_cors_middleware(app)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.state.templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Versão do produto (PR-driven) disponível em todos os templates como
# {{ app_version }} (rodapé da UI). Fonte única: app/core/version.py (importado
# no topo).
app.state.templates.env.globals["app_version"] = APP_VERSION
# Tier 2 (text-to-SQL governado): flag exposta como CALLABLE aos templates —
# `{% if text_to_sql_enabled() %}` reflete o toggle em runtime (lê o env a cada
# render), sem restart. Default OFF → a aba "Perguntar" não renderiza.
from app.data_tables.runtime import text_to_sql_enabled as _text_to_sql_enabled
app.state.templates.env.globals["text_to_sql_enabled"] = _text_to_sql_enabled
# Timezone da plataforma (parametrizável em Configurações > Plataforma). CALLABLE
# avaliado a cada render: lê os.environ['TZ'] (setado por apply_settings_to_env a
# partir de platform_settings) com fallback America/Sao_Paulo (GMT-3 Brasília).
# Exposto aos templates como {{ platform_tz() }} → window.PLATFORM_TZ.
import os as _os
app.state.templates.env.globals["platform_tz"] = lambda: (_os.environ.get("TZ") or "America/Sao_Paulo")

# API routes
app.include_router(agents.router)
app.include_router(skills.router)
app.include_router(workspace.router)
app.include_router(mesh.router)
app.include_router(mesh.car_router)
app.include_router(pipelines.router)
app.include_router(federation.router)
app.include_router(federation.peers_router)
app.include_router(dashboard.router)
app.include_router(wizard.router)
app.include_router(users.router)
app.include_router(users.domains_router)
app.include_router(frontend.router)

app.include_router(api_connectors_router)
app.include_router(mcp_diagnostics_router)
app.include_router(skill_dryrun_router)
app.include_router(help_router)
app.include_router(infra_router)
app.include_router(api_keys_router)
app.include_router(catalog_router)
app.include_router(data_tables_router)
app.include_router(logs_admin_router)
app.include_router(db_health_router)
app.include_router(playground_router)
app.include_router(privacy_router)

@app.get("/api/health")
async def health():
    """Health + identidade do código rodando.

    mcp_features expõe features esperadas — permite conferir, sem
    inspecionar stdout, se o servidor está de fato rodando o código
    mais recente após merge + restart.
    """
    import hashlib
    from pathlib import Path as _P
    engine_src = (_P(__file__).parent / "agents" / "engine.py").read_text(encoding="utf-8", errors="replace")
    runtime_src = (_P(__file__).parent / "mcp" / "runtime.py").read_text(encoding="utf-8", errors="replace")
    features = {
        "force_tool_choice": "_should_force_tool_call" in engine_src,      # PR #6
        "tool_choice_forced_log": "MCP tool_choice=forced" in engine_src,  # PR #6
        "tools_list_discovery": "_discover_server_tools" in runtime_src,   # PR #4
        "name_resolver": "_resolve_tool_name" in runtime_src,              # PR #4
        "prompt_hardening": "REGRA CRÍTICA" in engine_src,                 # PR #3
        "auth_token_propagation": "pt['auth_token'] = matched.get" in runtime_src,  # this PR
    }
    fingerprint = hashlib.sha256(
        (engine_src + runtime_src).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": APP_VERSION,
        "spec": "§1-§24 implemented",
        "mcp_features": features,
        "code_fingerprint": fingerprint,
    }


# ── Probes de orquestrador + métricas (OBS-2 + OBS-1) ──────────
# Anônimos e leves (fora de /api/v1 → não passam pelo default-deny; isentos de
# rate-limit). /livez e /readyz distinguem "processo vivo" de "pronto p/ tráfego"
# — um LB/k8s pode drenar uma réplica com o banco caído em vez de martelá-la.
@app.get("/livez", include_in_schema=False)
async def livez():
    """Liveness: 200 se o processo está de pé. ZERO I/O (nunca toca o banco)."""
    return {"status": "alive"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """Readiness: 200 só quando o pool asyncpg está pronto e acquirable; 503 senão."""
    from starlette.responses import JSONResponse
    import app.core.database as _db

    pool = _db._pool
    if pool is None:
        return JSONResponse({"status": "not_ready", "reason": "db_pool_uninitialized"}, status_code=503)
    try:
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
    except Exception:
        return JSONResponse({"status": "not_ready", "reason": "db_unavailable"}, status_code=503)
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Exposição Prometheus das métricas RED + escalonamento (app/core/metrics.py)."""
    from starlette.responses import Response
    from app.core.metrics import render_latest

    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)