"""PR-B2 (Trilha B) — a página 'Topologia de conexões' foi aposentada.

/mesh agora redireciona (308 permanente) para /mesh/flow (Fluxograma de agentes),
que é o editor único do mesh. A TABELA mesh_connections e os endpoints
/api/v1/mesh/* permanecem (fonte do grafo executável) — não testados aqui.
"""
import asyncio

from fastapi.responses import RedirectResponse

from app.routes.frontend import pg_mesh


def test_mesh_redirects_to_flow():
    resp = asyncio.run(pg_mesh(None))  # o handler ignora a request (só redireciona)
    assert isinstance(resp, RedirectResponse)
    assert resp.headers["location"] == "/mesh/flow"
    assert resp.status_code == 308  # permanente — bookmarks antigos migram


def test_mesh_flow_still_served():
    # garante que /mesh/flow continua sendo uma página renderizada (não redirect)
    from app.routes.frontend import PAGES
    assert "/mesh/flow" in PAGES
    assert PAGES["/mesh/flow"]["template"] == "pages/mesh_flow.html"
