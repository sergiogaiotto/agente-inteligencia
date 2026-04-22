"""AgenteInteligência-AI — Aplicação principal FastAPI."""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.core.config import get_settings
from app.core.database import init_db
from app.routes import agents, skills, workspace, mesh, dashboard, frontend, wizard, users
from app.routes.api_connectors import router as api_connectors_router

BASE_DIR = Path(__file__).resolve().parent

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    try:
        yield
    except Exception:
        pass

settings = get_settings()
app = FastAPI(title=settings.app_name, description="Plataforma Multi-Agente §SKILL.md sobre AI Mesh", version="2.0.0", lifespan=lifespan)
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

@app.get("/api/health")
async def health():
    return {"status": "ok", "app": settings.app_name, "version": "2.0.0", "spec": "§1-§24 implemented"}