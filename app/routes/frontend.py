"""Rotas de frontend — renderização Jinja2 com autenticação."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.core.database import users_repo

router = APIRouter(tags=["frontend"])

PAGES = {
    "/": {"template":"pages/dashboard.html","title":"Dashboard","section":"dashboard"},
    "/agents": {"template":"pages/agents.html","title":"Agentes","section":"agents"},
    "/agents/new": {"template":"pages/agent_form.html","title":"Novo Agente","section":"agents"},
    "/skills": {"template":"pages/skills.html","title":"Skills","section":"skills"},
    "/skills/new": {"template":"pages/skill_form.html","title":"Nova Skill","section":"skills"},
    "/workspace": {"template":"pages/workspace.html","title":"Workspace","section":"workspace"},
    "/mesh": {"template":"pages/mesh.html","title":"AI Mesh","section":"mesh"},
    "/mcp": {"template":"pages/tools.html","title":"MCP","section":"tools"},
    "/rag": {"template":"pages/evidence.html","title":"RAG — Base de Conhecimento","section":"evidence"},
    "/harness": {"template":"pages/harness.html","title":"Harness","section":"harness"},
    "/releases": {"template":"pages/releases.html","title":"Releases","section":"releases"},
    "/observability": {"template":"pages/observability.html","title":"Observabilidade","section":"observability"},
    "/history": {"template":"pages/history.html","title":"Histórico","section":"history"},
    "/settings": {"template":"pages/settings.html","title":"Configurações","section":"settings"},
    "/api-connectors": {"template":"pages/api_connectors.html","title":"API Connectors","section":"api_connectors"},
}

async def _get_user(request: Request):
    uid = request.cookies.get("user_id")
    if not uid:
        return None
    return await users_repo.find_by_id(uid)

async def _render(request: Request, key: str, **extra):
    # Check if system has any users
    count = await users_repo.count()
    if count == 0 and key != "/login":
        return RedirectResponse("/login", status_code=302)

    user = await _get_user(request)
    if count > 0 and not user and key != "/login":
        return RedirectResponse("/login", status_code=302)

    p = PAGES[key]
    t = request.app.state.templates
    user_data = {}
    if user:
        user_data = {k: v for k, v in dict(user).items() if k != "password_hash"}
    context = {
        "request": request, "title": p["title"], "section": p["section"],
        "app_name": "AgenteInteligência-AI",
        "current_user": user_data,
        "user_role": user_data.get("role", ""),
        **extra,
    }
    # Compatible with both old and new Starlette TemplateResponse signatures
    try:
        # New Starlette (0.28+): TemplateResponse(request, name, context)
        return t.TemplateResponse(request, p["template"], context)
    except TypeError:
        # Old Starlette: TemplateResponse(name, context)
        return t.TemplateResponse(p["template"], context)

# ── Login (no auth required) ──
@router.get("/login", response_class=HTMLResponse)
async def pg_login(request: Request):
    t = request.app.state.templates
    try:
        return t.TemplateResponse(request, "pages/login.html", {"request": request})
    except TypeError:
        return t.TemplateResponse("pages/login.html", {"request": request})

# ── Protected pages ──
@router.get("/", response_class=HTMLResponse)
async def pg_dashboard(r: Request): return await _render(r, "/")
@router.get("/agents", response_class=HTMLResponse)
async def pg_agents(r: Request): return await _render(r, "/agents")
@router.get("/agents/new", response_class=HTMLResponse)
async def pg_agent_new(r: Request): return await _render(r, "/agents/new")
@router.get("/agents/{agent_id}/edit", response_class=HTMLResponse)
async def pg_agent_edit(r: Request, agent_id: str): return await _render(r, "/agents/new", agent_id=agent_id)
@router.get("/skills", response_class=HTMLResponse)
async def pg_skills(r: Request): return await _render(r, "/skills")
@router.get("/skills/new", response_class=HTMLResponse)
async def pg_skill_new(r: Request): return await _render(r, "/skills/new")
@router.get("/skills/{skill_id}/edit", response_class=HTMLResponse)
async def pg_skill_edit(r: Request, skill_id: str): return await _render(r, "/skills/new", skill_id=skill_id)
@router.get("/workspace", response_class=HTMLResponse)
async def pg_workspace(r: Request): return await _render(r, "/workspace")
@router.get("/mesh", response_class=HTMLResponse)
async def pg_mesh(r: Request): return await _render(r, "/mesh")
@router.get("/mcp", response_class=HTMLResponse)
async def pg_mcp(r: Request): return await _render(r, "/mcp")
@router.get("/rag", response_class=HTMLResponse)
async def pg_rag(r: Request): return await _render(r, "/rag")
@router.get("/harness", response_class=HTMLResponse)
async def pg_harness(r: Request): return await _render(r, "/harness")
@router.get("/releases", response_class=HTMLResponse)
async def pg_releases(r: Request): return await _render(r, "/releases")
@router.get("/observability", response_class=HTMLResponse)
async def pg_observability(r: Request): return await _render(r, "/observability")
@router.get("/history", response_class=HTMLResponse)
async def pg_history(r: Request): return await _render(r, "/history")
@router.get("/settings", response_class=HTMLResponse)
async def pg_settings(r: Request): return await _render(r, "/settings")

@router.get("/api-connectors", response_class=HTMLResponse)
async def pg_api_connectors(r: Request): return await _render(r, "/api-connectors")