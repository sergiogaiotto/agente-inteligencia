"""Rotas — Módulo API Connectors.

CRUD de conectores (aplicações externas), endpoints salvos,
proxy para execução, health check, e histórico de chamadas.

Revisão de qualidade (2026-05): auth headers e body preparation
centralizados em app/core/http_auth.py. API keys cifradas at-rest
via app/core/crypto.py. verify_ssl e body_type por connector/endpoint.
"""
import uuid
import json
import re
import time
import logging
import httpx
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.crypto import encrypt_secret, decrypt_secret
from app.core.http_auth import (
    build_auth_headers as _http_build_auth_headers,
    prepare_request_body,
    BODY_TYPES,
)

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
    auth_type: Optional[str] = "none"       # none | api_key | bearer | basic | cookie
    auth_header: Optional[str] = "X-API-Key" # header name for api_key/bearer
    health_path: Optional[str] = "/api/health"
    timeout_ms: Optional[int] = 30000
    sort_order: Optional[int] = 99
    verify_ssl: Optional[int] = 1            # 0=desativa validação TLS (APIs self-signed)

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
    verify_ssl: Optional[int] = None

class EndpointCreate(BaseModel):
    name: str
    method: str       # GET | POST | PUT | PATCH | DELETE
    path: str
    description: Optional[str] = ""
    category: Optional[str] = "geral"
    sample_body: Optional[str] = "{}"
    sample_headers: Optional[str] = "{}"
    body_type: Optional[str] = "json"  # json | form_urlencoded | multipart | text | xml

class EndpointUpdate(BaseModel):
    name: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    sample_body: Optional[str] = None
    sample_headers: Optional[str] = None
    is_favorite: Optional[int] = None
    body_type: Optional[str] = None

class ProxyRequest(BaseModel):
    connector_id: str
    method: str
    path: str
    body: Optional[object] = None    # dict (json/form) ou str (text/xml) ou {fields, files} (multipart)
    headers: Optional[dict] = None
    endpoint_id: Optional[str] = None
    body_type: Optional[str] = None  # override do body_type do endpoint; default json


class InlineTestRequest(BaseModel):
    """Shape p/ testar conexão antes de salvar — usa os mesmos campos
    do connector, mas não persiste nada."""
    base_url: str
    auth_type: Optional[str] = "none"
    auth_header: Optional[str] = "X-API-Key"
    api_key: Optional[str] = ""
    health_path: Optional[str] = "/api/health"
    timeout_ms: Optional[int] = 15000


class IntrospectRequest(BaseModel):
    url: str
    bearer_token: Optional[str] = ""  # retrocompat: quando não há connector_id
    connector_id: Optional[str] = ""  # preferível: usa auth do connector (cookie, bearer, api_key, basic)
    max_endpoints: Optional[int] = 25


class ExtractCookieRequest(BaseModel):
    """Faz um POST de login e extrai o cookie da response Set-Cookie.

    Útil para APIs cookie-based (session auth) onde o token não volta
    no body do JSON, só no header Set-Cookie.
    """
    login_url: str              # URL completa do endpoint de login
    login_body: dict            # body JSON a enviar (ex: {"login":"...","password":"..."})
    cookie_name: Optional[str] = ""  # se vazio, tenta detectar do primeiro Set-Cookie
    timeout_ms: Optional[int] = 15000


# ═══════════════════════════════════════════════════════
# DB helpers — repositórios resolvidos via database.py central.
# Schema das tabelas api_connectors / api_endpoints / api_call_logs
# está em app/core/database.py e é criado pelo init_db() no startup.
# ═══════════════════════════════════════════════════════

_cached_repos = None
_migrated = True  # tabelas vivem no schema central — sempre True


async def _ensure_tables():
    """No-op de retrocompat — schema vem do init_db() central."""
    return


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
        # API key sempre cifrada at-rest. encrypt_secret('') == '' (sentinel).
        "api_key": encrypt_secret(data.api_key or ""),
        "auth_type": data.auth_type or "none",
        "auth_header": data.auth_header or "X-API-Key",
        "health_path": data.health_path or "/api/health",
        "timeout_ms": data.timeout_ms or 30000,
        "is_active": 1,
        "sort_order": data.sort_order if data.sort_order != 99 else max_order + 1,
        "verify_ssl": 1 if (data.verify_ssl is None or data.verify_ssl) else 0,
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
    # API key cifrada antes de persistir. encrypt_secret é idempotente
    # (valor já cifrado é retornado como está). Permite re-save sem dupla cifra.
    if "api_key" in upd:
        upd["api_key"] = encrypt_secret(upd["api_key"])
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
    body_type = (data.body_type or "json").strip().lower()
    if body_type not in BODY_TYPES:
        raise HTTPException(422, f"body_type inválido. Use: {list(BODY_TYPES)}")
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
        "body_type": body_type,
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
    """Health check de um API connector. PR #231: emite evento estruturado
    `api_connector.test.completed` / `.failed` para rastreio via Log Viewer 2.0.
    """
    conn_repo, _, _ = _repos()
    c = await conn_repo.find_by_id(connector_id)
    if not c:
        raise HTTPException(404)
    base = c.get("base_url", "").rstrip("/")
    path = c.get("health_path", "/api/health")
    url = f"{base}{path}"
    headers = _build_auth_headers(c)
    start = time.time()
    result: dict
    try:
        async with httpx.AsyncClient(headers=headers, **_client_kwargs(c)) as client:
            r = await client.get(url)
        latency = round((time.time() - start) * 1000, 2)
        result = {
            "ok": 200 <= r.status_code < 400,
            "status": r.status_code,
            "latency_ms": latency,
            "url": url,
        }
    except httpx.ConnectError:
        result = {"ok": False, "status": 0, "error": f"Não foi possível conectar a {base}", "url": url}
    except httpx.TimeoutException:
        result = {"ok": False, "status": 408, "error": "Timeout", "url": url}
    except Exception as e:
        result = {"ok": False, "status": 500, "error": str(e)[:200], "url": url}

    duration_ms = round((time.time() - start) * 1000, 2)
    ok = bool(result.get("ok"))
    log_payload = {
        "event": "api_connector.test.completed" if ok else "api_connector.test.failed",
        "connector_id": connector_id,
        "connector_name": c.get("name", ""),
        "url": url,
        "ok": ok,
        "status": result.get("status"),
        "error": (result.get("error") or "")[:300] if not ok else "",
        "latency_ms": result.get("latency_ms"),
        "duration_ms": duration_ms,
    }
    if ok:
        logger.info(log_payload["event"], extra=log_payload)
    else:
        logger.warning(log_payload["event"], extra=log_payload)
    return result


@router.get("/health/all")
async def health_all():
    """Health check em batch — usa timeout_ms do connector (não 10s hardcoded
    que ignorava a config) e respeita verify_ssl."""
    await _ensure_tables()
    conn_repo, _, _ = _repos()
    connectors = await conn_repo.find_all(is_active=1, limit=50)
    results = {}
    for c in connectors:
        base = c.get("base_url", "").rstrip("/")
        path = c.get("health_path", "/api/health")
        headers = _build_auth_headers(c)
        try:
            async with httpx.AsyncClient(headers=headers, **_client_kwargs(c)) as client:
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
    """Proxy: executa chamada HTTP via connector.

    body_type (override em data.body_type; default 'json'):
      json | form_urlencoded | multipart | text | xml

    Multipart espera shape: {fields: {k: v, ...}, files: [{name, content, filename, content_type}]}.
    Text/XML espera body como string.
    """
    conn_repo, ep_repo, log_repo = _repos()
    c = await conn_repo.find_by_id(data.connector_id)
    if not c:
        return {"error": f"Conector '{data.connector_id}' não encontrado", "status": 0}

    base = c.get("base_url", "").rstrip("/")
    url = f"{base}{data.path}"
    headers = _build_auth_headers(c)
    if data.headers:
        headers.update(data.headers)

    # Resolve body_type: override do request > endpoint salvo > 'json'
    body_type = data.body_type or "json"
    if data.endpoint_id and not data.body_type:
        try:
            ep = await ep_repo.find_by_id(data.endpoint_id)
            if ep and ep.get("body_type"):
                body_type = ep["body_type"]
        except Exception:
            pass  # endpoint não encontrado — usa json default

    # Para JSON, mantemos Content-Type já setado pelo _build_auth_headers.
    # Para outros tipos, prepare_request_body ajusta o Content-Type.
    if body_type != "json":
        headers.pop("Content-Type", None)
    body_kwargs = prepare_request_body(body_type, data.body, extra_headers={})
    # Mescla headers de body (Content-Type específico) com headers de auth
    if "headers" in body_kwargs:
        body_headers = body_kwargs.pop("headers")
        for k, v in body_headers.items():
            headers.setdefault(k, v)

    start = time.time()
    call_id = str(uuid.uuid4())
    method = data.method.upper()
    valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    if method not in valid_methods:
        return {"error": f"Método {method} não suportado", "status": 400}

    try:
        async with httpx.AsyncClient(headers=headers, **_client_kwargs(c)) as client:
            # httpx aceita GET com body via .request() — mais consistente
            r = await client.request(method, url, **body_kwargs)

        latency = round((time.time() - start) * 1000, 2)
        try:
            resp_data = r.json()
        except Exception:
            resp_data = {"raw": r.text[:5000]}

        # Log — proxy manual não tem contexto de interaction; interaction_id fica vazio.
        await log_repo.create({
            "id": call_id,
            "connector_id": data.connector_id,
            "endpoint_id": data.endpoint_id or "",
            "agent_id": "",
            "interaction_id": "",
            "method": method,
            "url": url,
            "request_headers": json.dumps({k: v for k, v in headers.items()}, ensure_ascii=False),
            "request_body": json.dumps(data.body, ensure_ascii=False, default=str)[:5000] if data.body else "{}",
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
# TEST INLINE — valida conexão ANTES de salvar
# ═══════════════════════════════════════════════════════

@router.post("/test-inline")
async def test_inline(data: InlineTestRequest):
    """Testa uma configuração de conector antes de persistir.

    Recebe os mesmos campos do form (base_url, auth, health_path) e
    dispara GET {base_url}{health_path}. Retorna {ok, status, latency_ms,
    url, error?} — mesmo shape do /test tradicional.
    """
    base = (data.base_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "base_url obrigatório")
    path = data.health_path or "/api/health"
    if not path.startswith("/"):
        path = "/" + path
    url = f"{base}{path}"
    timeout = (data.timeout_ms or 15000) / 1000
    headers = _build_auth_headers({
        "auth_type": data.auth_type or "none",
        "auth_header": data.auth_header or "X-API-Key",
        "api_key": data.api_key or "",
    })
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
        latency = round((time.time() - start) * 1000, 2)
        return {
            "ok": 200 <= r.status_code < 400,
            "status": r.status_code,
            "latency_ms": latency,
            "url": url,
            "hint": _test_hint(r.status_code),
        }
    except httpx.ConnectError:
        return {"ok": False, "status": 0, "error": f"Não foi possível conectar a {base}", "url": url,
                "hint": "Verifique se a URL está correta e o host está acessível."}
    except httpx.TimeoutException:
        return {"ok": False, "status": 408, "error": "Timeout", "url": url,
                "hint": "A API demorou mais que o timeout. Aumente timeout_ms ou verifique latência."}
    except Exception as e:
        return {"ok": False, "status": 500, "error": str(e)[:200], "url": url,
                "hint": None}


def _test_hint(status_code: int) -> Optional[str]:
    if status_code == 401:
        return "Auth falhou — confira auth_type e o token."
    if status_code == 403:
        return "Auth ok mas sem permissão — confira escopo do token."
    if status_code == 404:
        return "health_path não existe — tente /health, /healthz, / ou /api/health."
    if 200 <= status_code < 300:
        return None
    if 300 <= status_code < 400:
        return "Redirecionamento — pode funcionar ao salvar."
    if 500 <= status_code:
        return "Servidor retornou erro — a API pode estar fora do ar."
    return None


# ═══════════════════════════════════════════════════════
# EXTRACT-COOKIE — auxilia auth session-based (Set-Cookie)
# ═══════════════════════════════════════════════════════

@router.post("/extract-cookie")
async def extract_cookie(data: ExtractCookieRequest):
    """Faz um POST de login e extrai o valor do cookie de sessão do
    header Set-Cookie da resposta.

    Retorna {ok, cookie_name, cookie_value, status, error?}.
    """
    if not data.login_url.strip():
        raise HTTPException(400, "login_url obrigatório")
    timeout = (data.timeout_ms or 15000) / 1000
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            r = await client.post(
                data.login_url.strip(),
                json=data.login_body or {},
                headers={"Content-Type": "application/json"},
            )
    except httpx.ConnectError as e:
        return {"ok": False, "status": 0, "error": f"Não consegui conectar: {e}"}
    except httpx.TimeoutException:
        return {"ok": False, "status": 408, "error": "Timeout no login"}
    except Exception as e:
        return {"ok": False, "status": 500, "error": str(e)[:200]}

    # coleta TODOS os Set-Cookie
    set_cookies_raw: list[str] = []
    try:
        # httpx.Headers.get_list for multi-valued headers
        set_cookies_raw = r.headers.get_list("set-cookie")
    except Exception:
        sc = r.headers.get("set-cookie")
        if sc:
            set_cookies_raw = [sc]

    if not set_cookies_raw:
        # provável falha de login — body pode ter detail
        try:
            body_preview = r.json()
        except Exception:
            body_preview = r.text[:300]
        return {
            "ok": False,
            "status": r.status_code,
            "error": "Login não retornou nenhum Set-Cookie. Verifique credenciais.",
            "body_preview": body_preview,
        }

    # Parse cookies via http.cookies.SimpleCookie — RFC 6265 strict.
    # Trata Max-Age, Secure, SameSite, Expires, Domain, Path, HttpOnly
    # corretamente. Anteriormente: split por ';' manual perdia atributos.
    from http.cookies import SimpleCookie
    parsed_cookies: list[dict] = []
    for raw in set_cookies_raw:
        try:
            jar = SimpleCookie()
            jar.load(raw)
            for name, morsel in jar.items():
                parsed_cookies.append({
                    "name": name.strip(),
                    "value": morsel.value,
                    "attrs": {
                        "path": morsel["path"],
                        "domain": morsel["domain"],
                        "secure": bool(morsel["secure"]),
                        "httponly": bool(morsel["httponly"]),
                        "samesite": morsel["samesite"],
                        "max_age": morsel["max-age"],
                        "expires": morsel["expires"],
                    },
                    "raw": raw[:400],
                })
        except Exception as _parse_err:
            # Fallback ao parser antigo se SimpleCookie falhar com formato exótico
            logger.warning(f"SimpleCookie falhou para '{raw[:80]}': {_parse_err}")
            first = raw.split(";", 1)[0].strip()
            if "=" in first:
                name, value = first.split("=", 1)
                parsed_cookies.append({"name": name.strip(), "value": value.strip(), "raw": raw[:400]})

    # Escolhe o cookie: por nome se informado, senão o primeiro
    chosen = None
    if data.cookie_name:
        target = data.cookie_name.strip()
        for c in parsed_cookies:
            if c["name"] == target:
                chosen = c
                break
        if not chosen:
            return {
                "ok": False, "status": r.status_code,
                "error": f"Cookie '{target}' não encontrado. Disponíveis: "
                          + ", ".join(c["name"] for c in parsed_cookies),
                "available_cookies": [c["name"] for c in parsed_cookies],
            }
    else:
        chosen = parsed_cookies[0] if parsed_cookies else None

    if not chosen:
        return {"ok": False, "status": r.status_code, "error": "Nenhum cookie parseável."}

    return {
        "ok": 200 <= r.status_code < 300,
        "status": r.status_code,
        "cookie_name": chosen["name"],
        "cookie_value": chosen["value"],
        "all_cookie_names": [c["name"] for c in parsed_cookies],
    }


# ═══════════════════════════════════════════════════════
# INTROSPECT — "IA, me ajude!" preenche via OpenAPI
# ═══════════════════════════════════════════════════════

_OPENAPI_CANDIDATE_PATHS = (
    # FastAPI / Starlette default
    "/openapi.json",
    "/api/openapi.json",
    "/v1/openapi.json",
    "/v2/openapi.json",
    "/api/v1/openapi.json",
    "/api/v2/openapi.json",
    # Spring Boot 2/3
    "/v3/api-docs",
    "/v2/api-docs",
    "/api/v3/api-docs",
    # ASP.NET / Swashbuckle
    "/swagger.json",
    "/swagger/v1/swagger.json",
    # Express + swagger-ui-express / generic
    "/docs/openapi.json",
    "/api-docs/swagger.json",
)


def _format_auth_hint(auth_source: str) -> str:
    """Pista contextual para 401/403 durante introspect, considerando o
    `auth_type` que o connector usou.

    Cobre todos os 4 tipos suportados por `_build_auth_headers` (api_key,
    bearer, basic, cookie) + fallback para "none" / desconhecido. Sempre
    inclui o sufixo universal sugerindo cadastro manual — porque é comum
    APIs protegerem `/openapi.json` por sessão web mesmo quando a API
    real é exposta por API Key (ex: fcsd-claro), caso em que nenhuma
    correção de auth resolve a Descoberta automática.
    """
    raw_type = auth_source.split(":", 1)[1] if ":" in auth_source else (auth_source or "")
    type_hints = {
        "api_key": "Confira o header e a chave em Editar connector → Autenticação → API Key.",
        "bearer": "Bearer token pode ter expirado — gere um novo no provedor da API.",
        "basic": "Confira user:password em Editar connector → Autenticação → Basic.",
        "cookie": "Cookie pode ter expirado — renove via 'Gerar cookie via login' no form do connector.",
    }
    specific = type_hints.get(raw_type, "Confira a configuração de autenticação do connector.")
    return (
        f"Tentei com auth do connector ({auth_source}) e o servidor retornou 401/403. "
        f"{specific} "
        "Algumas APIs protegem /openapi.json apenas via sessão web — nesse caso, "
        "cadastre os endpoints manualmente em '+ Novo endpoint'."
    )


@router.post("/introspect")
async def introspect(data: IntrospectRequest):
    """Descobre um conector a partir de uma URL com OpenAPI/Swagger.

    Tenta: URL bruta → paths comuns de openapi.json. Se achou, mapeia para
    sugestões de conector + lista de endpoints. Nunca salva nada — só
    propõe. O frontend decide o que aplicar.
    """
    raw = (data.url or "").strip()
    if not raw:
        raise HTTPException(400, "url obrigatória")
    if "://" not in raw:
        raw = "https://" + raw

    parsed = httpx.URL(raw)
    origin = f"{parsed.scheme}://{parsed.host}" + (f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else "")

    # Strip anchor/query; se path termina em .json usamos direto
    url_path = (parsed.path or "/").rstrip("/")
    candidate_urls: list[str] = []
    if url_path.endswith(".json") or url_path.endswith("/api-docs"):
        candidate_urls.append(f"{origin}{url_path}")
    else:
        # 1. candidatos na raiz do host
        for p in _OPENAPI_CANDIDATE_PATHS:
            candidate_urls.append(f"{origin}{p}")
        # 2. candidatos combinados com path do usuário (ex: /api/v3 + /openapi.json)
        if url_path and url_path not in ("/", ""):
            # remove /docs/swagger etc para tentar sob o prefixo "real" da API
            base_path = re.sub(r"/(docs|swagger(?:-ui)?|redoc)\b.*$", "", url_path).rstrip("/")
            if base_path:
                for p in _OPENAPI_CANDIDATE_PATHS:
                    cand = f"{origin}{base_path}{p}"
                    if cand not in candidate_urls:
                        candidate_urls.append(cand)
        # 3. URL original (pode ser um Swagger UI que vamos parsear)
        candidate_urls.append(f"{origin}{url_path or '/'}")

    headers = {"Accept": "application/json, text/html"}
    auth_source = "none"
    # Preferível: usar auth do próprio connector (cobre cookie/bearer/api_key/basic)
    if data.connector_id:
        conn_repo, _, _ = _repos()
        conn = await conn_repo.find_by_id(data.connector_id.strip())
        if conn:
            auth_headers = _build_auth_headers(conn)
            # _build_auth_headers devolve Content-Type: json — aqui preferimos
            # preservar o Accept que já setamos. Remove o Content-Type pra não
            # sobrepor um GET.
            auth_headers.pop("Content-Type", None)
            headers.update(auth_headers)
            auth_source = f"connector:{conn.get('auth_type','none')}"
    # Retrocompat: bearer_token avulso (quando não há connector ainda)
    elif data.bearer_token and data.bearer_token.strip():
        headers["Authorization"] = f"Bearer {data.bearer_token.strip()}"
        auth_source = "bearer_inline"

    spec: Optional[dict] = None
    tried: list = []
    final_url = ""
    auth_hint: Optional[str] = None
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
        for cand in candidate_urls:
            try:
                r = await client.get(cand)
            except Exception as e:
                tried.append({"url": cand, "status": 0, "error": str(e)[:120]})
                continue
            ct = (r.headers.get("content-type") or "").lower()
            tried.append({"url": cand, "status": r.status_code, "content_type": ct[:60]})
            if r.status_code == 401 or r.status_code == 403:
                auth_hint = (
                    "A URL de OpenAPI exige autenticação. Cole um bearer token no campo 'Token (opcional)' "
                    "e tente novamente."
                )
                continue
            if r.status_code != 200:
                continue
            # Tenta parsear JSON direto
            if "json" in ct:
                try:
                    candidate_spec = r.json()
                except Exception:
                    candidate_spec = None
                if isinstance(candidate_spec, dict) and ("openapi" in candidate_spec or "swagger" in candidate_spec):
                    spec = candidate_spec
                    final_url = cand
                    break
            # HTML? Procura hint de Swagger UI com URL do spec embutida
            if "html" in ct:
                hinted = _extract_openapi_url_from_html(r.text, origin)
                if hinted and hinted not in [t["url"] for t in tried]:
                    try:
                        r2 = await client.get(hinted)
                        ct2 = (r2.headers.get("content-type") or "").lower()
                        tried.append({"url": hinted, "status": r2.status_code, "content_type": ct2[:60], "via": "html-hint"})
                        if r2.status_code == 200 and "json" in ct2:
                            candidate_spec = r2.json()
                            if isinstance(candidate_spec, dict) and ("openapi" in candidate_spec or "swagger" in candidate_spec):
                                spec = candidate_spec
                                final_url = hinted
                                break
                    except Exception as e:
                        tried.append({"url": hinted, "status": 0, "error": str(e)[:120], "via": "html-hint"})

    if not spec:
        # PR #234: mensagem agora começa pela razão MAIS COMUM (API não publica
        # OpenAPI) e destaca o atalho que funciona (cURL wizard). Muitas APIs
        # públicas (Brasilapi, ViaCEP, OpenWeather no plano free, etc.) documentam
        # em HTML estático sem expor .json — não há "URL exata do spec" para o
        # operador colar, então sugerir isso primeiro só confunde.
        hint_not_found = (
            "Esta API provavelmente não expõe OpenAPI publicamente (tentei "
            f"{len(candidate_urls)} caminhos comuns — todos sem sucesso). "
            "Atalhos: 1) cole um comando cURL no campo abaixo — eu extraio "
            "base_url, auth e endpoint automaticamente; 2) se mesmo assim souber "
            "a URL exata do openapi.json, cole-a aqui; 3) preencha manualmente."
        )
        if auth_hint and auth_source != "none":
            # já tentou com auth do connector mas ainda deu 401/403 — pista contextual
            auth_hint = _format_auth_hint(auth_source)
        return {
            "found": False,
            "tried": tried,
            "origin": origin,
            "auth_source": auth_source,
            "hint": auth_hint or hint_not_found,
        }

    # ── Extração ──
    info = spec.get("info") or {}
    title = (info.get("title") or "").strip()
    version = (info.get("version") or "").strip()
    description = (info.get("description") or "").strip()

    # base_url vem de servers[0].url — pode ser relativo
    servers = spec.get("servers") or []
    base_url_proposal = origin
    if servers and isinstance(servers, list):
        first = servers[0].get("url", "") if isinstance(servers[0], dict) else ""
        if first:
            if first.startswith(("http://", "https://")):
                base_url_proposal = first.rstrip("/")
            else:
                base_url_proposal = (origin + "/" + first.lstrip("/")).rstrip("/")

    # auth a partir de securitySchemes
    sec_schemes = (spec.get("components", {}) or {}).get("securitySchemes", {}) or {}
    auth_type = "none"
    auth_header = "X-API-Key"
    for _name, sch in sec_schemes.items():
        if not isinstance(sch, dict):
            continue
        st = (sch.get("type") or "").lower()
        if st == "http":
            scheme = (sch.get("scheme") or "").lower()
            if scheme == "bearer":
                auth_type = "bearer"
                auth_header = "Authorization"
                break
            if scheme == "basic":
                auth_type = "basic"
                auth_header = "Authorization"
                break
        if st == "apikey":
            # OpenAPI: apiKey pode estar em: header | query | cookie
            in_loc = (sch.get("in") or "").lower()
            if in_loc == "cookie":
                auth_type = "cookie"
                auth_header = sch.get("name") or "session"
            else:
                auth_type = "api_key"
                auth_header = sch.get("name") or "X-API-Key"
            break
        if st in ("oauth2", "openidconnect"):
            auth_type = "bearer"
            auth_header = "Authorization"
            break

    # health_path candidato: /health, /healthz, /api/health, / (fallback)
    paths_obj = spec.get("paths") or {}
    health_candidates = ["/health", "/healthz", "/api/health", "/status", "/"]
    health_path = next((p for p in health_candidates if p in paths_obj), "/api/health")

    # Endpoints list (top N, prioriza os com operationId ou summary)
    endpoints: list = []
    max_eps = max(1, min(data.max_endpoints or 25, 100))
    for path, methods in paths_obj.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(op, dict):
                continue
            summary = (op.get("summary") or op.get("operationId") or f"{method.upper()} {path}").strip()
            desc = (op.get("description") or "").strip()
            tags = op.get("tags") or []
            category = tags[0] if tags and isinstance(tags, list) else "geral"
            body_example = _build_body_example(op, spec)
            endpoints.append({
                "name": summary[:80],
                "method": method.upper(),
                "path": path,
                "description": desc[:300],
                "category": str(category)[:40],
                "sample_body": json.dumps(body_example, ensure_ascii=False)[:2000] if body_example else "{}",
            })
            if len(endpoints) >= max_eps:
                break
        if len(endpoints) >= max_eps:
            break

    # Visual: cor deterministicamente a partir do nome; ícone = 2 letras
    colors = ["bg-brand-500", "bg-violet-500", "bg-teal-500", "bg-emerald-600",
               "bg-amber-500", "bg-rose-500", "bg-indigo-500", "bg-orange-500"]
    color = colors[(sum(ord(c) for c in (title or "A")) % len(colors))]
    icon = "".join(c for c in title.upper() if c.isalnum())[:2] or "AP"

    return {
        "found": True,
        "spec_url": final_url,
        "origin": origin,
        "auth_source": auth_source,
        "proposal": {
            "name": title or parsed.host.split(".")[0].title(),
            "base_url": base_url_proposal,
            "description": description[:500],
            "icon": icon,
            "color": color,
            "auth_type": auth_type,
            "auth_header": auth_header,
            "health_path": health_path,
            "timeout_ms": 30000,
        },
        "meta": {
            "openapi_version": spec.get("openapi") or spec.get("swagger"),
            "api_version": version,
            "paths_count": len(paths_obj),
            "endpoints_discovered": len(endpoints),
        },
        "endpoints": endpoints,
        "tried": tried,
    }


def _extract_openapi_url_from_html(html: str, origin: str) -> Optional[str]:
    """Detecta a URL do openapi.json num HTML de Swagger UI / ReDoc.

    Ex: Swagger UI gera `<script>... url: "/openapi.json" ...</script>`.
    ReDoc: `<redoc spec-url="/openapi.json">`.
    """
    if not html:
        return None
    # ReDoc
    m = re.search(r'spec-url=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        url = m.group(1)
        return url if url.startswith("http") else origin + (url if url.startswith("/") else "/" + url)
    # Swagger UI
    m = re.search(r'url:\s*["\']([^"\']+\.(?:json|yaml|yml))["\']', html, re.IGNORECASE)
    if m:
        url = m.group(1)
        return url if url.startswith("http") else origin + (url if url.startswith("/") else "/" + url)
    # Swagger UI v3+ com urls:[{url: ...}]
    m = re.search(r'urls:\s*\[\s*\{\s*url:\s*["\']([^"\']+)["\']', html)
    if m:
        url = m.group(1)
        return url if url.startswith("http") else origin + (url if url.startswith("/") else "/" + url)
    return None


def _build_body_example(op: dict, spec: dict) -> Optional[dict]:
    """Extrai um exemplo de body do OpenAPI, seguindo $ref quando possível."""
    rb = op.get("requestBody") or {}
    content = rb.get("content") or {}
    js = content.get("application/json") or {}
    if "example" in js:
        return js["example"]
    examples = js.get("examples") or {}
    if examples:
        first = next(iter(examples.values()), None)
        if isinstance(first, dict) and "value" in first:
            return first["value"]
    schema = js.get("schema") or {}
    ref = schema.get("$ref")
    if ref and ref.startswith("#/"):
        parts = ref[2:].split("/")
        cur = spec
        for p in parts:
            cur = cur.get(p) if isinstance(cur, dict) else None
            if cur is None:
                break
        schema = cur or schema
    if isinstance(schema, dict) and "example" in schema:
        return schema["example"]
    # Skeleton a partir de properties (útil p/ POSTs)
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        skel = {}
        for k, v in list(props.items())[:10]:
            t = (v.get("type") if isinstance(v, dict) else "") or "string"
            skel[k] = {"string": "", "integer": 0, "number": 0, "boolean": False,
                       "array": [], "object": {}}.get(t, "")
        return skel
    return None


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════

def _build_auth_headers(connector: dict) -> dict:
    """Wrapper local: delega para app.core.http_auth.build_auth_headers (que
    decifra a api_key e cobre os 5 tipos de auth) e adiciona Content-Type
    default para retrocompat de chamadas que esperam JSON.

    Para o caso especial de auth_type='cookie' com formato 'name=value' direto
    no api_key (sem auth_header), mantemos a lógica antiga aqui — http_auth
    cobre só {Cookie: <api_key>} bruto.
    """
    headers = {"Content-Type": "application/json"}
    auth_headers = _http_build_auth_headers(connector)
    headers.update(auth_headers)
    # Cookie com auth_header específico: ajusta para "name=value" se preciso
    if connector.get("auth_type") == "cookie" and "Cookie" in headers:
        cookie_name = (connector.get("auth_header") or "").strip()
        cookie_val = headers["Cookie"]
        if cookie_name and "=" not in cookie_val:
            headers["Cookie"] = f"{cookie_name}={cookie_val}"
    return headers


def _client_kwargs(connector: dict, default_timeout: Optional[float] = None) -> dict:
    """Kwargs comuns para httpx.AsyncClient baseados no connector.

    Centraliza: verify_ssl, timeout (em segundos), follow_redirects.
    Default follow_redirects=True — exceto onde explicitamente precisamos
    interceptar redirect (ex: extract_cookie precisa do Set-Cookie do primeiro
    response, antes de seguir).
    """
    timeout_s = default_timeout
    if timeout_s is None:
        timeout_s = (connector.get("timeout_ms", 30000) or 30000) / 1000
    verify = bool(connector.get("verify_ssl", 1))
    return {
        "timeout": timeout_s,
        "verify": verify,
        "follow_redirects": True,
    }


# ═══════════════════════════════════════════════════════
# ADMIN — Retention de api_call_logs
# ═══════════════════════════════════════════════════════


@router.post("/admin/cleanup-logs")
async def cleanup_logs(days: int = Query(30, ge=1, le=3650)):
    """Remove rows antigas em api_call_logs.

    Retention policy: operador escolhe quantos dias manter. Sem cleanup
    automático — para previsibilidade. Logs crescem em GB/mês em produção;
    cron mensal recomendado.

    Returns: {deleted_count, kept_after, kept_before_date}.
    """
    from app.core.database import _get_pool
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pool = _get_pool()
    async with pool.acquire() as con:
        res = await con.execute(
            "DELETE FROM api_call_logs WHERE created_at < $1",
            cutoff,
        )
    try:
        deleted = int(res.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        deleted = 0
    return {
        "deleted_count": deleted,
        "kept_after": cutoff.isoformat(),
        "days_kept": days,
    }