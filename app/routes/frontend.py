"""Rotas de frontend — renderização Jinja2 com autenticação."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.core.database import users_repo

router = APIRouter(tags=["frontend"])

PAGES = {
    "/": {"template":"pages/dashboard.html","title":"Dashboard","section":"dashboard"},
    "/agents": {"template":"pages/agents.html","title":"Agentes","section":"agents"},
    "/agents/new": {"template":"pages/agent_form.html","title":"Novo Agente","section":"agents"},
    "/agents/invocations": {"template":"pages/agent_invocations.html","title":"Invocações","section":"agents"},
    "/skills": {"template":"pages/skills.html","title":"Skills","section":"skills"},
    "/skills/new": {"template":"pages/skill_form.html","title":"Nova Skill","section":"skills"},
    "/catalog": {"template":"pages/catalog.html","title":"Catálogo","section":"catalog"},
    "/catalog/detail": {"template":"pages/catalog_detail.html","title":"Detalhe da Entry","section":"catalog"},
    "/catalog/publish": {"template":"pages/catalog_publish.html","title":"Publicar no Catálogo","section":"catalog"},
    "/catalog/queue": {"template":"pages/catalog_queue.html","title":"Fila de Revisão","section":"catalog_queue"},
    "/catalog/inventory": {"template":"pages/catalog_inventory.html","title":"Inventário Regulatório","section":"catalog_inventory"},
    "/catalog/stewardship": {"template":"pages/catalog_stewardship.html","title":"Stewardship","section":"catalog_stewardship"},
    "/catalog/cost": {"template":"pages/catalog_cost.html","title":"Custo & Consumo","section":"catalog_cost"},
    "/workspace": {"template":"pages/workspace.html","title":"Workspace","section":"workspace"},
    "/mesh": {"template":"pages/mesh.html","title":"AI Mesh","section":"mesh"},
    "/mesh/flow": {"template":"pages/mesh_flow.html","title":"Fluxograma de agentes","section":"mesh"},
    "/mcp": {"template":"pages/tools.html","title":"MCP","section":"tools"},
    "/rag": {"template":"pages/evidence.html","title":"RAG — Base de Conhecimento","section":"evidence"},
    "/harness": {"template":"pages/harness.html","title":"Avaliação","section":"harness"},
    "/releases": {"template":"pages/releases.html","title":"Releases","section":"releases"},
    "/quality": {"template":"pages/quality.html","title":"Qualidade","section":"quality"},
    "/observability": {"template":"pages/observability.html","title":"Observabilidade","section":"observability"},
    "/infra": {"template":"pages/infra.html","title":"Infraestrutura","section":"infra"},
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
        "app_name": "Maestro",
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
@router.get("/agents/{agent_id}/invocations", response_class=HTMLResponse)
async def pg_agent_invocations(r: Request, agent_id: str): return await _render(r, "/agents/invocations", agent_id=agent_id)
@router.get("/skills", response_class=HTMLResponse)
async def pg_skills(r: Request): return await _render(r, "/skills")
@router.get("/skills/new", response_class=HTMLResponse)
async def pg_skill_new(r: Request): return await _render(r, "/skills/new")
@router.get("/skills/{skill_id}/edit", response_class=HTMLResponse)
async def pg_skill_edit(r: Request, skill_id: str): return await _render(r, "/skills/new", skill_id=skill_id)
@router.get("/catalog", response_class=HTMLResponse)
async def pg_catalog(r: Request): return await _render(r, "/catalog")
@router.get("/catalog/publish", response_class=HTMLResponse)
async def pg_catalog_publish(r: Request): return await _render(r, "/catalog/publish")
@router.get("/catalog/queue", response_class=HTMLResponse)
async def pg_catalog_queue(r: Request): return await _render(r, "/catalog/queue")
@router.get("/catalog/inventory", response_class=HTMLResponse)
async def pg_catalog_inventory(r: Request): return await _render(r, "/catalog/inventory")
@router.get("/catalog/stewardship", response_class=HTMLResponse)
async def pg_catalog_stewardship(r: Request): return await _render(r, "/catalog/stewardship")
@router.get("/catalog/cost", response_class=HTMLResponse)
async def pg_catalog_cost(r: Request): return await _render(r, "/catalog/cost")
@router.get("/catalog/{entry_id}", response_class=HTMLResponse)
async def pg_catalog_detail(r: Request, entry_id: str): return await _render(r, "/catalog/detail", entry_id=entry_id)
@router.get("/workspace", response_class=HTMLResponse)
async def pg_workspace(r: Request): return await _render(r, "/workspace")
@router.get("/mesh")
async def pg_mesh(r: Request):
    # Trilha B / PR-B2: a página "Topologia de conexões" foi aposentada — o
    # Fluxograma de agentes é o editor único do mesh. /mesh redireciona p/ ele
    # (bookmarks/links antigos continuam funcionando). A TABELA mesh_connections
    # e os endpoints /api/v1/mesh/* permanecem (fonte do grafo executável).
    return RedirectResponse("/mesh/flow", status_code=308)
@router.get("/mesh/flow", response_class=HTMLResponse)
async def pg_mesh_flow(r: Request): return await _render(r, "/mesh/flow")
@router.get("/mcp", response_class=HTMLResponse)
async def pg_mcp(r: Request): return await _render(r, "/mcp")
@router.get("/rag", response_class=HTMLResponse)
async def pg_rag(r: Request): return await _render(r, "/rag")
@router.get("/harness", response_class=HTMLResponse)
async def pg_harness(r: Request): return await _render(r, "/harness")
@router.get("/releases", response_class=HTMLResponse)
async def pg_releases(r: Request): return await _render(r, "/releases")
@router.get("/quality", response_class=HTMLResponse)
async def pg_quality(r: Request): return await _render(r, "/quality")
@router.get("/observability", response_class=HTMLResponse)
async def pg_observability(r: Request): return await _render(r, "/observability")
@router.get("/infra", response_class=HTMLResponse)
async def pg_infra(r: Request): return await _render(r, "/infra")
@router.get("/history", response_class=HTMLResponse)
async def pg_history(r: Request): return await _render(r, "/history")
@router.get("/settings", response_class=HTMLResponse)
async def pg_settings(r: Request): return await _render(r, "/settings")

@router.get("/api-connectors", response_class=HTMLResponse)
async def pg_api_connectors(r: Request): return await _render(r, "/api-connectors")