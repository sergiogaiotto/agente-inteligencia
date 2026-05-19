"""Maestro — Aplicação principal FastAPI."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.core.config import get_settings
from app.core.database import init_db, close_db
from app.core.otel import init_otel
from app.routes import agents, skills, workspace, mesh, dashboard, frontend, wizard, users
from app.routes.api_connectors import router as api_connectors_router
from app.routes.mcp_diagnostics import router as mcp_diagnostics_router
from app.routes.help import router as help_router
from app.routes.infra import router as infra_router
from app.routes.api_keys import router as api_keys_router

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    try:
        yield
    finally:
        # Drena tasks async do verifier antes de fechar o pool — evita
        # erros em INSERT contra pool já fechado quando shutdown pega
        # uma task de production sample no meio.
        try:
            from app.verifier.async_dispatcher import drain
            await drain(timeout=5.0)
        except Exception as e:
            logger.warning(f"verifier drain falhou no shutdown: {e}")
        await close_db()

settings = get_settings()
app = FastAPI(title=settings.app_name, description="Plataforma Multi-Agente §SKILL.md sobre AI Mesh", version="2.0.0", lifespan=lifespan)

# ── Observabilidade (Onda 2) ───────────────────────────────────
# Inicializa OpenTelemetry ANTES dos middlewares para que requests sejam
# instrumentadas desde o primeiro byte. No-op se OTEL_ENABLED=false.
init_otel(app)

# ── Middlewares de segurança (Onda 1) ──────────────────────────
# Rate-limit (sliding window via Redis com fallback memory) — defesa LLM04.
from app.core.ratelimit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.state.templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# API routes
app.include_router(agents.router)
app.include_router(skills.router)
app.include_router(workspace.router)
app.include_router(mesh.router)
app.include_router(mesh.car_router)
app.include_router(dashboard.router)
app.include_router(wizard.router)
app.include_router(users.router)
app.include_router(users.domains_router)
app.include_router(frontend.router)

app.include_router(api_connectors_router)
app.include_router(mcp_diagnostics_router)
app.include_router(help_router)
app.include_router(infra_router)
app.include_router(api_keys_router)

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
        "version": "2.0.0",
        "spec": "§1-§24 implemented",
        "mcp_features": features,
        "code_fingerprint": fingerprint,
    }