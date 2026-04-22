"""Rotas — Módulo API Connectors.

CRUD de conectores (aplicações externas), endpoints salvos,
proxy para execução, health check, e histórico de chamadas.
"""
import uuid
import json
import time
import logging
import httpx
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/api-connectors", tags=["api-connectors"])

# ═══════════════════════════════════════════════════════
# Pydantic models
# ═══════════════════════════════════════════════════════

class ConnectorCreate(BaseModel):
    name: str
    base_url: str
    description: Optional[str] = ""
    icon: Optional[str] = "AP"
    color: Optional[str] = "bg-brand-500"
    api_key: Optional[str] = ""
    auth_type: Optional[str] = "none"       # none | api_key | bearer | basic
    auth_header: Optional[str] = "X-API-Key" # header name for api_key/bearer
    health_path: Optional[str] = "/api/health"
    timeout_ms: Optional[int] = 30000
    sort_order: Optional[int] = 99

class ConnectorUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    api_key: Optional[str] = None
    auth_type: Optional[str] = None
    auth_header: Optional[str] = None
    health_path: Optional[str] = None
    timeout_ms: Optional[int] = None
    is_active: Optional[int] = None
    sort_order: Optional[int] = None

class EndpointCreate(BaseModel):
    name: str
    method: str       # GET | POST | PUT | PATCH | DELETE
    path: str
    description: Optional[str] = ""
    category: Optional[str] = "geral"
    sample_body: Optional[str] = "{}"
    sample_headers: Optional[str] = "{}"

class EndpointUpdate(BaseModel):
    name: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    sample_body: Optional[str] = None
    sample_headers: Optional[str] = None
    is_favorite: Optional[int] = None

class ProxyRequest(BaseModel):
    connector_id: str
    method: str
    path: str
    body: Optional[dict] = None
    headers: Optional[dict] = None
    endpoint_id: Optional[str] = None


# ═══════════════════════════════════════════════════════
# DB helpers — auto-bootstrap tabelas e repositórios
# ═══════════════════════════════════════════════════════

_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS api_connectors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'AP',
    color TEXT DEFAULT 'bg-brand-500',
    api_key TEXT DEFAULT '',
    auth_type TEXT DEFAULT 'none',
    auth_header TEXT DEFAULT 'X-API-Key',
    health_path TEXT DEFAULT '/api/health',
    timeout_ms INTEGER DEFAULT 30000,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS api_endpoints (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    name TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'geral',
    sample_body TEXT DEFAULT '{}',
    sample_headers TEXT DEFAULT '{}',
    is_favorite INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (connector_id) REFERENCES api_connectors(id)
);
CREATE TABLE IF NOT EXISTS api_call_logs (
    id TEXT PRIMARY KEY,
    connector_id TEXT DEFAULT '',
    endpoint_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    request_headers TEXT DEFAULT '{}',
    request_body TEXT DEFAULT '{}',
    response_body TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (connector_id) REFERENCES api_connectors(id)
);
"""

_cached_repos = None
_migrated = False


async def _ensure_tables():
    global _migrated
    if _migrated:
        return
    from app.core.database import get_db
    async with get_db() as db:
        await db.executescript(_MIGRATION_SQL)
        await db.commit()
    _migrated = True


def _repos():
    global _cached_repos
    if _cached_repos:
        return _cached_repos
    # Tentar importar do database.py (caso já tenha sido adicionado)
    try:
        from app.core.database import api_connectors_repo, api_endpoints_repo, api_call_logs_repo
        _cached_repos = (api_connectors_repo, api_endpoints_repo, api_call_logs_repo)
    except ImportError:
        # Criar repositórios inline usando a mesma classe Repository
        from app.core.database import Repository
        _cached_repos = (
            Repository("api_connectors"),
            Repository("api_endpoints"),
            Repository("api_call_logs"),
        )
    return _cached_repos


# ═══════════════════════════════════════════════════════
# CONNECTORS CRUD
# ═══════════════════════════════════════════════════════

@router.get("")
async def list_connectors():
    await _ensure_tables()
    conn_repo, ep_repo, _ = _repos()
    connectors = await conn_repo.find_all(limit=100)
    connectors.sort(key=lambda c: c.get("sort_order", 0))
    return {"connectors": connectors}


@router.get("/{connector_id}")
async def get_connector(connector_id: str):
    conn_repo, ep_repo, _ = _repos()
    c = await conn_repo.find_by_id(connector_id)
    if not c:
        raise HTTPException(404, "Conector não encontrado")
    endpoints = await ep_repo.find_all(connector_id=connector_id, limit=200)
    return {**c, "endpoints": endpoints}


@router.post("", status_code=201)
async def create_connector(data: ConnectorCreate):
    conn_repo, _, _ = _repos()
    cid = str(uuid.uuid4())
    all_conns = await conn_repo.find_all(limit=100)
    max_order = max((c.get("sort_order", 0) for c in all_conns), default=0)
    await conn_repo.create({
        "id": cid,
        "name": data.name,
        "base_url": data.base_url.rstrip("/"),
        "description": data.description or "",
        "icon": data.icon or data.name[:2].upper(),
        "color": data.color or "bg-brand-500",
        "api_key": data.api_key or "",
        "auth_type": data.auth_type or "none",
        "auth_header": data.auth_header or "X-API-Key",
        "health_path": data.health_path or "/api/health",
        "timeout_ms": data.timeout_ms or 30000,
        "is_active": 1,
        "sort_order": data.sort_order if data.sort_order != 99 else max_order + 1,
    })
    return {"id": cid, "message": f"Conector '{data.name}' criado"}


@router.put("/{connector_id}")
async def update_connector(connector_id: str, data: ConnectorUpdate):
    conn_repo, _, _ = _repos()
    existing = await conn_repo.find_by_id(connector_id)
    if not existing:
        raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    if "base_url" in upd:
        upd["base_url"] = upd["base_url"].rstrip("/")
    if not upd:
        raise HTTPException(400, "Nenhum campo para atualizar")
    await conn_repo.update(connector_id, upd)
    return await conn_repo.find_by_id(connector_id)


@router.delete("/{connector_id}")
async def delete_connector(connector_id: str):
    conn_repo, ep_repo, _ = _repos()
    if not await conn_repo.delete(connector_id):
        raise HTTPException(404)
    # Cascade: delete related endpoints
    eps = await ep_repo.find_all(connector_id=connector_id, limit=500)
    for ep in eps:
        await ep_repo.delete(ep["id"])
    return {"message": "Conector removido"}


@router.post("/{connector_id}/toggle")
async def toggle_connector(connector_id: str):
    conn_repo, _, _ = _repos()
    c = await conn_repo.find_by_id(connector_id)
    if not c:
        raise HTTPException(404)
    new_val = 0 if c.get("is_active", 1) else 1
    await conn_repo.update(connector_id, {"is_active": new_val})
    return {"is_active": new_val}


# ═══════════════════════════════════════════════════════
# ENDPOINTS CRUD
# ═══════════════════════════════════════════════════════

@router.get("/{connector_id}/endpoints")
async def list_endpoints(connector_id: str, category: str = None):
    _, ep_repo, _ = _repos()
    filters = {"connector_id": connector_id}
    if category:
        filters["category"] = category
    endpoints = await ep_repo.find_all(limit=200, **filters)
    # Group by category
    by_cat = {}
    for ep in endpoints:
        cat = ep.get("category", "geral")
        by_cat.setdefault(cat, []).append(ep)
    return {"endpoints": endpoints, "by_category": by_cat}


@router.post("/{connector_id}/endpoints", status_code=201)
async def create_endpoint(connector_id: str, data: EndpointCreate):
    conn_repo, ep_repo, _ = _repos()
    c = await conn_repo.find_by_id(connector_id)
    if not c:
        raise HTTPException(404, "Conector não encontrado")
    eid = str(uuid.uuid4())
    await ep_repo.create({
        "id": eid,
        "connector_id": connector_id,
        "name": data.name,
        "method": data.method.upper(),
        "path": data.path,
        "description": data.description or "",
        "category": data.category or "geral",
        "sample_body": data.sample_body or "{}",
        "sample_headers": data.sample_headers or "{}",
        "is_favorite": 0,
    })
    return {"id": eid, "message": f"Endpoint '{data.name}' criado"}


@router.put("/{connector_id}/endpoints/{endpoint_id}")
async def update_endpoint(connector_id: str, endpoint_id: str, data: EndpointUpdate):
    _, ep_repo, _ = _repos()
    ep = await ep_repo.find_by_id(endpoint_id)
    if not ep or ep.get("connector_id") != connector_id:
        raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    if "method" in upd:
        upd["method"] = upd["method"].upper()
    if not upd:
        raise HTTPException(400)
    await ep_repo.update(endpoint_id, upd)
    return await ep_repo.find_by_id(endpoint_id)


@router.delete("/{connector_id}/endpoints/{endpoint_id}")
async def delete_endpoint(connector_id: str, endpoint_id: str):
    _, ep_repo, _ = _repos()
    if not await ep_repo.delete(endpoint_id):
        raise HTTPException(404)
    return {"message": "Endpoint removido"}


@router.patch("/{connector_id}/endpoints/{endpoint_id}/favorite")
async def toggle_favorite(connector_id: str, endpoint_id: str):
    _, ep_repo, _ = _repos()
    ep = await ep_repo.find_by_id(endpoint_id)
    if not ep:
        raise HTTPException(404)
    new_val = 0 if ep.get("is_favorite") else 1
    await ep_repo.update(endpoint_id, {"is_favorite": new_val})
    return {"is_favorite": bool(new_val)}


# ═══════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════

@router.post("/{connector_id}/test")
async def test_connector(connector_id: str):
    conn_repo, _, _ = _repos()
    c = await conn_repo.find_by_id(connector_id)
    if not c:
        raise HTTPException(404)
    base = c.get("base_url", "").rstrip("/")
    path = c.get("health_path", "/api/health")
    url = f"{base}{path}"
    timeout = (c.get("timeout_ms", 30000) or 30000) / 1000
    headers = _build_auth_headers(c)
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            r = await client.get(url)
        latency = round((time.time() - start) * 1000, 2)
        return {
            "ok": 200 <= r.status_code < 400,
            "status": r.status_code,
            "latency_ms": latency,
            "url": url,
        }
    except httpx.ConnectError:
        return {"ok": False, "status": 0, "error": f"Não foi possível conectar a {base}", "url": url}
    except httpx.TimeoutException:
        return {"ok": False, "status": 408, "error": "Timeout", "url": url}
    except Exception as e:
        return {"ok": False, "status": 500, "error": str(e)[:200], "url": url}


@router.get("/health/all")
async def health_all():
    await _ensure_tables()
    conn_repo, _, _ = _repos()
    connectors = await conn_repo.find_all(is_active=1, limit=50)
    results = {}
    for c in connectors:
        base = c.get("base_url", "").rstrip("/")
        path = c.get("health_path", "/api/health")
        headers = _build_auth_headers(c)
        try:
            async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                r = await client.get(f"{base}{path}")
            results[c["id"]] = {"ok": 200 <= r.status_code < 400, "status": r.status_code, "name": c["name"]}
        except Exception:
            results[c["id"]] = {"ok": False, "status": 0, "name": c["name"]}
    return results


# ═══════════════════════════════════════════════════════
# PROXY — Execute API call
# ═══════════════════════════════════════════════════════

@router.post("/proxy")
async def proxy_call(data: ProxyRequest):
    conn_repo, _, log_repo = _repos()
    c = await conn_repo.find_by_id(data.connector_id)
    if not c:
        return {"error": f"Conector '{data.connector_id}' não encontrado", "status": 0}

    base = c.get("base_url", "").rstrip("/")
    url = f"{base}{data.path}"
    timeout = (c.get("timeout_ms", 30000) or 30000) / 1000
    headers = _build_auth_headers(c)
    if data.headers:
        headers.update(data.headers)

    start = time.time()
    call_id = str(uuid.uuid4())
    method = data.method.upper()

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            if method == "GET":
                r = await client.get(url)
            elif method == "POST":
                r = await client.post(url, json=data.body or {})
            elif method == "PUT":
                r = await client.put(url, json=data.body or {})
            elif method == "PATCH":
                r = await client.patch(url, json=data.body or {})
            elif method == "DELETE":
                r = await client.delete(url)
            else:
                return {"error": f"Método {method} não suportado", "status": 400}

        latency = round((time.time() - start) * 1000, 2)
        try:
            resp_data = r.json()
        except Exception:
            resp_data = {"raw": r.text[:5000]}

        # Log
        await log_repo.create({
            "id": call_id,
            "connector_id": data.connector_id,
            "endpoint_id": data.endpoint_id or "",
            "agent_id": "",
            "method": method,
            "url": url,
            "request_headers": json.dumps({k: v for k, v in headers.items()}, ensure_ascii=False),
            "request_body": json.dumps(data.body, ensure_ascii=False) if data.body else "{}",
            "response_body": json.dumps(resp_data, ensure_ascii=False, default=str)[:5000],
            "status_code": r.status_code,
            "latency_ms": latency,
        })

        return {
            "call_id": call_id,
            "status": r.status_code,
            "data": resp_data,
            "latency_ms": latency,
            "method": method,
            "url": url,
        }
    except httpx.ConnectError:
        return {"error": f"Não foi possível conectar a {base}", "status": 0}
    except httpx.TimeoutException:
        return {"error": "Timeout na chamada", "status": 408}
    except Exception as e:
        return {"error": str(e)[:300], "status": 500}


# ═══════════════════════════════════════════════════════
# CALL HISTORY
# ═══════════════════════════════════════════════════════

@router.get("/history/calls")
async def call_history(connector_id: str = None, limit: int = 50):
    _, _, log_repo = _repos()
    filters = {}
    if connector_id:
        filters["connector_id"] = connector_id
    calls = await log_repo.find_all(limit=limit, **filters)
    return {"calls": calls, "total": len(calls)}


# ═══════════════════════════════════════════════════════
# CATALOG — tree view data (all connectors + endpoints)
# ═══════════════════════════════════════════════════════

@router.get("/catalog/tree")
async def catalog_tree():
    await _ensure_tables()
    conn_repo, ep_repo, _ = _repos()
    connectors = await conn_repo.find_all(is_active=1, limit=50)
    connectors.sort(key=lambda c: c.get("sort_order", 0))
    tree = []
    for c in connectors:
        eps = await ep_repo.find_all(connector_id=c["id"], limit=200)
        by_cat = {}
        for ep in eps:
            cat = ep.get("category", "geral")
            by_cat.setdefault(cat, []).append(ep)
        tree.append({
            "id": c["id"],
            "name": c["name"],
            "icon": c.get("icon", "AP"),
            "color": c.get("color", "bg-brand-500"),
            "base_url": c.get("base_url", ""),
            "is_active": c.get("is_active", 1),
            "endpoint_count": len(eps),
            "categories": by_cat,
        })
    return {"tree": tree}


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════

def _build_auth_headers(connector: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    auth_type = connector.get("auth_type", "none")
    api_key = connector.get("api_key", "")
    if not api_key:
        return headers
    header_name = connector.get("auth_header", "X-API-Key")
    if auth_type == "api_key":
        headers[header_name] = api_key
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "basic":
        import base64
        headers["Authorization"] = f"Basic {base64.b64encode(api_key.encode()).decode()}"
    return headers