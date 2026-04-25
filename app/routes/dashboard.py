"""Rotas da plataforma: releases §18, gold §9.4, harness §9.5, knowledge §14, tools §10, dashboard, settings.

CORREÇÕES (2026-04):
- Timeout do test_mcp_connection (stdio) elevado de 20s → 90s para acomodar 1ª execução de `npx -y`
- Timeout do execute_mcp_tool (stdio) elevado de 30s → 90s pelo mesmo motivo
- update_tool: usa exclude_unset (não sobrescreve campos não enviados com None), loga, trata empty upd
- create_tool: idem, com logging
- CORREÇÃO (2026-04-21): Header `Accept: application/json, text/event-stream` adicionado a TODAS
  as chamadas HTTP para MCP servers. Sem esse header, servidores que usam o transporte
  MCP Streamable HTTP (spec 2025-03-26) retornam HTTP 406 Not Acceptable.
"""
import uuid, json, logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.models.schemas import ReleaseCreate, GoldCaseCreate, KnowledgeSourceCreate, ToolCreate, ToolUpdate, RunEvalRequest
from app.core.database import (
    releases_repo, gold_cases_repo, eval_runs_repo, knowledge_repo,
    tools_repo, agents_repo, skills_repo, interactions_repo,
    turns_repo, envelopes_repo, drift_repo, audit_repo, get_db,
    settings_store, prompts_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["platform"])

# ═══════════════════════════════════════════════════════════════
# Auth MCP — suporte a API Key, OAuth2 Client Credentials e mTLS.
#
# Headers base: Content-Type + Accept (MCP Streamable HTTP spec
# 2025-03-26 exige Accept com application/json e text/event-stream).
#
# API Key:  → Authorization: Bearer <token>
# OAuth2:   → busca access_token via client_credentials grant
#              e injeta Authorization: Bearer <access_token>
# mTLS:     → conexão TLS mútua via certificado cliente (PEM)
#              injetado como parâmetro `cert` no httpx.AsyncClient
# ═══════════════════════════════════════════════════════════════
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Cache de tokens OAuth2 para evitar buscar a cada chamada
_oauth2_token_cache: dict = {}  # key: client_id → {"access_token": str, "expires_at": float}


def _build_mcp_auth(auth_type: str = "", auth_token: str = "", auth_config: str = "{}") -> dict:
    """Constrói configuração de autenticação MCP.

    Retorna dict com:
      - headers: dict de headers HTTP (sempre inclui MCP_HEADERS base)
      - client_kwargs: dict de kwargs extras para httpx.AsyncClient (cert para mTLS)
      - needs_oauth_fetch: bool — se precisa buscar token OAuth2 antes de usar

    auth_type: 'api_key', 'oauth2', 'mTLS', ou ''
    auth_token: token simples (usado por api_key)
    auth_config: JSON string com config complexa (OAuth2/mTLS)
    """
    import json as _json

    result = {
        "headers": {**MCP_HEADERS},
        "client_kwargs": {},
        "oauth_config": None,
    }

    if not auth_type:
        return result

    # ── API Key: token direto no header ──
    if auth_type == "api_key":
        if auth_token and auth_token.strip():
            result["headers"]["Authorization"] = f"Bearer {auth_token.strip()}"
        return result

    # ── OAuth2 Client Credentials ──
    if auth_type == "oauth2":
        try:
            config = _json.loads(auth_config) if auth_config else {}
        except (ValueError, TypeError):
            config = {}
        result["oauth_config"] = {
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
            "token_url": config.get("token_url", ""),
            "scope": config.get("scope", ""),
        }
        # Verificar cache
        client_id = result["oauth_config"]["client_id"]
        if client_id and client_id in _oauth2_token_cache:
            import time
            cached = _oauth2_token_cache[client_id]
            if cached.get("expires_at", 0) > time.time():
                result["headers"]["Authorization"] = f"Bearer {cached['access_token']}"
                result["oauth_config"] = None  # Não precisa buscar de novo
        return result

    # ── mTLS: certificados cliente ──
    if auth_type == "mTLS":
        try:
            config = _json.loads(auth_config) if auth_config else {}
        except (ValueError, TypeError):
            config = {}
        cert_pem = config.get("client_cert", "")
        key_pem = config.get("client_key", "")
        if cert_pem and key_pem:
            import tempfile, os
            # Escrever PEMs em arquivos temporários (httpx exige paths)
            cert_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            cert_file.write(cert_pem)
            cert_file.close()
            key_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            key_file.write(key_pem)
            key_file.close()
            result["client_kwargs"]["cert"] = (cert_file.name, key_file.name)
            # CA cert opcional
            ca_pem = config.get("ca_cert", "")
            if ca_pem:
                ca_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
                ca_file.write(ca_pem)
                ca_file.close()
                result["client_kwargs"]["verify"] = ca_file.name
        return result

    # Fallback: se auth_type desconhecido mas tem token, usar como Bearer
    if auth_token and auth_token.strip():
        result["headers"]["Authorization"] = f"Bearer {auth_token.strip()}"
    return result


async def _fetch_oauth2_token(oauth_config: dict) -> str:
    """Busca access_token via OAuth2 Client Credentials Grant.

    Retorna o access_token ou string vazia em caso de falha.
    Cacheia o token até expiração.
    """
    import httpx, time

    client_id = oauth_config.get("client_id", "")
    client_secret = oauth_config.get("client_secret", "")
    token_url = oauth_config.get("token_url", "")
    scope = oauth_config.get("scope", "")

    if not client_id or not client_secret or not token_url:
        return ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
            if scope:
                payload["scope"] = scope

            resp = await client.post(
                token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                logger.warning(f"OAuth2 token fetch failed: HTTP {resp.status_code} — {resp.text[:200]}")
                return ""

            data = resp.json()
            access_token = data.get("access_token", "")
            expires_in = data.get("expires_in", 3600)

            # Cachear com margem de 60s
            _oauth2_token_cache[client_id] = {
                "access_token": access_token,
                "expires_at": time.time() + max(expires_in - 60, 60),
            }
            logger.info(f"OAuth2 token obtido para client_id={client_id[:8]}... expires_in={expires_in}s")
            return access_token

    except Exception as e:
        logger.warning(f"OAuth2 token fetch error: {e}")
        return ""


def _cleanup_mcp_auth(auth_result: dict):
    """Remove arquivos temporários de certificados mTLS."""
    import os
    cert = auth_result.get("client_kwargs", {}).get("cert")
    if cert and isinstance(cert, tuple):
        for path in cert:
            try: os.unlink(path)
            except: pass
    verify = auth_result.get("client_kwargs", {}).get("verify")
    if verify and isinstance(verify, str) and verify.endswith('.pem'):
        try: os.unlink(verify)
        except: pass

# ═══ Dashboard ═══
@router.get("/dashboard/stats")
async def dashboard_stats():
    return {
        "total_agents": await agents_repo.count(),
        "agents_by_kind": {
            "aobd": await agents_repo.count(kind="aobd"),
            "router": await agents_repo.count(kind="router"),
            "subagent": await agents_repo.count(kind="subagent"),
        },
        "active_agents": await agents_repo.count(status="active"),
        "inactive_agents": await agents_repo.count(status="inactive"),
        "active_by_kind": {
            "aobd": await agents_repo.count(kind="aobd", status="active"),
            "router": await agents_repo.count(kind="router", status="active"),
            "subagent": await agents_repo.count(kind="subagent", status="active"),
        },
        "total_skills": await skills_repo.count(),
        "total_interactions": await interactions_repo.count(),
        "total_turns": await turns_repo.count(),
        "total_releases": await releases_repo.count(),
        "total_gold_cases": await gold_cases_repo.count(),
        "total_eval_runs": await eval_runs_repo.count(),
        "total_knowledge_sources": await knowledge_repo.count(),
        "total_tools": await tools_repo.count(),
        "total_envelopes": await envelopes_repo.count(),
        "total_drift_events": await drift_repo.count(),
        "recent_interactions": await interactions_repo.find_all(limit=10),
        "recent_eval_runs": await eval_runs_repo.find_all(limit=5),
    }

# ═══ Releases §18 ═══
@router.get("/releases")
async def list_releases(environment: str = None, limit: int = 20):
    f = {}
    if environment: f["environment"] = environment
    return {"releases": await releases_repo.find_all(limit=limit, **f)}

@router.post("/releases", status_code=201)
async def create_release(data: ReleaseCreate):
    rid = str(uuid.uuid4())
    await releases_repo.create({
        "id": rid, "name": data.name, "environment": data.environment,
        "model_config": data.model_config_data, "prompt_config": data.prompt_config,
        "index_config": data.index_config, "policy_config": data.policy_config,
    })
    return {"id": rid, "message": "Release criada"}

@router.put("/releases/{release_id}/promote")
async def promote_release(release_id: str, target_env: str = "canary"):
    r = await releases_repo.find_by_id(release_id)
    if not r: raise HTTPException(404)
    await releases_repo.update(release_id, {"environment": target_env, "status": target_env})
    await audit_repo.create({"entity_type":"release","entity_id":release_id,"action":f"promoted_to_{target_env}"})
    return {"message": f"Release promovida para {target_env}"}

# ═══ Gold Cases §9.4 ═══
@router.get("/gold-cases")
async def list_gold_cases(dataset_version: str = None, case_type: str = None, limit: int = 50):
    f = {}
    if dataset_version: f["dataset_version"] = dataset_version
    if case_type: f["case_type"] = case_type
    return {"cases": await gold_cases_repo.find_all(limit=limit, **f), "total": await gold_cases_repo.count(**f)}

@router.post("/gold-cases", status_code=201)
async def create_gold_case(data: GoldCaseCreate):
    gid = str(uuid.uuid4())
    await gold_cases_repo.create({"id": gid, **data.model_dump()})
    return {"id": gid, "message": "Caso gold criado"}

@router.delete("/gold-cases/{case_id}")
async def delete_gold_case(case_id: str):
    if not await gold_cases_repo.delete(case_id): raise HTTPException(404)
    return {"message": "Caso removido"}

# ═══ Harness §9.5 ═══
@router.get("/eval-runs")
async def list_eval_runs(release_id: str = None, limit: int = 20):
    f = {}
    if release_id: f["release_id"] = release_id
    return {"runs": await eval_runs_repo.find_all(limit=limit, **f)}

@router.post("/eval-runs/execute")
async def run_harness(data: RunEvalRequest):
    """Executa harness de avaliação contra dataset gold §9.5."""
    from app.harness.evaluator import run_evaluation
    try:
        result = await run_evaluation(data.release_id, data.agent_id, data.gold_version, data.run_type)
        return result
    except Exception as e:
        raise HTTPException(500, f"Erro no harness: {str(e)}")

# ═══ Knowledge Sources §14 ═══
@router.get("/knowledge-sources")
async def list_knowledge_sources(limit: int = 50):
    return {"sources": await knowledge_repo.find_all(limit=limit)}

@router.get("/knowledge-sources/{ks_id}")
async def get_knowledge_source(ks_id: str):
    s = await knowledge_repo.find_by_id(ks_id)
    if not s: raise HTTPException(404)
    return s

@router.post("/knowledge-sources", status_code=201)
async def create_knowledge_source(data: KnowledgeSourceCreate):
    kid = str(uuid.uuid4())
    await knowledge_repo.create({"id": kid, **data.model_dump()})
    return {"id": kid, "message": "Base de conhecimento registrada"}

@router.put("/knowledge-sources/{ks_id}")
async def update_knowledge_source(ks_id: str, data: KnowledgeSourceCreate):
    if not await knowledge_repo.find_by_id(ks_id): raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump().items() if v is not None}
    return await knowledge_repo.update(ks_id, upd)

@router.delete("/knowledge-sources/{ks_id}")
async def delete_knowledge_source(ks_id: str):
    if not await knowledge_repo.delete(ks_id): raise HTTPException(404)
    return {"message": "Base removida"}

# ═══ Tools / Tool Registry §10 ═══
@router.get("/tools")
async def list_tools(limit: int = 50, sensitivity: str = None):
    f = {}
    if sensitivity: f["sensitivity"] = sensitivity
    return {"tools": await tools_repo.find_all(limit=limit, **f), "total": await tools_repo.count(**f)}

@router.get("/tools/{tool_id}")
async def get_tool(tool_id: str):
    t = await tools_repo.find_by_id(tool_id)
    if not t: raise HTTPException(404, "Tool não encontrada")
    return t

@router.post("/tools", status_code=201)
async def create_tool(data: ToolCreate):
    """Cria tool. Loga payload recebido para diagnóstico."""
    tid = str(uuid.uuid4())
    payload = data.model_dump()
    logger.info(f"create_tool: name={payload.get('name')!r} mcp_server={payload.get('mcp_server')!r}")
    d = {"id": tid, **payload}
    d["requires_trusted_context"] = 1 if data.requires_trusted_context else 0
    try:
        await tools_repo.create(d)
        await audit_repo.create({"entity_type":"tool","entity_id":tid,"action":"created","details":json.dumps({"name":data.name,"mcp_server":data.mcp_server})})
        logger.info(f"create_tool: id={tid} criada com sucesso")
        return {"id": tid, "message": "Tool registrada", "name": data.name}
    except Exception as e:
        logger.error(f"create_tool: falha — {e}")
        raise HTTPException(500, f"Erro ao registrar tool: {str(e)}")

@router.put("/tools/{tool_id}")
async def update_tool(tool_id: str, data: ToolUpdate):
    """Atualiza tool.

    CORREÇÕES:
    - Usa model_dump(exclude_unset=True) para não sobrescrever campos não enviados com None
    - Loga payload recebido e dict de update para diagnóstico
    - Trata caso de upd vazio (retorna registro existente, não quebra SQL)
    - Mensagem de erro 500 explícita em caso de falha de DB
    """
    existing = await tools_repo.find_by_id(tool_id)
    if not existing:
        raise HTTPException(404, "Tool não encontrada")

    # exclude_unset retorna apenas campos que o cliente enviou explicitamente.
    # Combinado com filtro None, evita sobrescrever colunas com NULL acidentalmente.
    raw = data.model_dump(exclude_unset=True)
    logger.info(f"update_tool {tool_id}: campos recebidos = {list(raw.keys())}")

    upd = {k: v for k, v in raw.items() if v is not None}

    # Boolean → int para SQLite
    if "requires_trusted_context" in upd:
        upd["requires_trusted_context"] = 1 if upd["requires_trusted_context"] else 0

    if not upd:
        logger.warning(f"update_tool {tool_id}: nenhum campo válido para atualizar")
        return existing

    logger.info(f"update_tool {tool_id}: aplicando update = {list(upd.keys())}")

    try:
        result = await tools_repo.update(tool_id, upd)
        if not result:
            logger.error(f"update_tool {tool_id}: update retornou None (find_by_id falhou)")
            raise HTTPException(500, "Update aplicado mas registro não pôde ser recuperado")
        await audit_repo.create({
            "entity_type": "tool", "entity_id": tool_id, "action": "updated",
            "details": json.dumps({"fields": list(upd.keys())}),
        })
        logger.info(f"update_tool {tool_id}: sucesso")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"update_tool {tool_id}: erro de DB")
        raise HTTPException(500, f"Erro ao atualizar tool: {str(e)[:200]}")

@router.delete("/tools/{tool_id}")
async def delete_tool(tool_id: str):
    if not await tools_repo.delete(tool_id): raise HTTPException(404)
    return {"message": "Tool removida"}

class MCPWizardQuery(BaseModel):
    query: str

@router.post("/tools/wizard")
async def mcp_wizard(data: MCPWizardQuery):
    """Wizard: busca MCP Server por URL (mcpservers.org) ou por descrição via LLM."""
    import re, httpx

    query = data.query.strip()
    is_url = query.startswith("http://") or query.startswith("https://")

    try:
        settings = await settings_store.get_all()
        api_key = settings.get("openai_key", "")
        model = settings.get("openai_model", "gpt-4o")
        base_url = "https://api.openai.com/v1"

        if not api_key:
            api_key = settings.get("maritaca_key", "")
            model = settings.get("maritaca_model", "sabia-3")
            base_url = settings.get("maritaca_url", "https://chat.maritaca.ai/api") + "/v1"

        if not api_key:
            return {"results": [], "error": "Configure uma API key em Configurações (OpenAI ou Maritaca)."}

        page_content = ""
        if is_url:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(query, headers={"User-Agent": "AgenteInteligencia/1.0"})
                resp.raise_for_status()
                raw_html = resp.text[:12000]
                page_content = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL)
                page_content = re.sub(r'<style[^>]*>.*?</style>', '', page_content, flags=re.DOTALL)
                page_content = re.sub(r'<[^>]+>', ' ', page_content)
                page_content = re.sub(r'\s+', ' ', page_content).strip()[:6000]

        if is_url and page_content:
            prompt = f"""Extraia as informações do MCP Server desta página.

Conteúdo:
{page_content}

Responda SOMENTE com JSON array, sem markdown:
[{{"name":"nome","description":"descrição","endpoint":"comando npx ou URL","operations":["op1","op2"],"install_cmd":"npx -y ...","source_url":"{query}","auth":"none","sensitivity":"internal"}}]"""
        else:
            prompt = f"""Recomende 1 a 3 MCP servers para: "{query}"
Responda SOMENTE com JSON array, sem markdown:
[{{"name":"nome","description":"descrição","endpoint":"npx -y @scope/server","operations":["op1"],"install_cmd":"npx -y ...","source_url":"https://github.com/...","auth":"none","sensitivity":"internal"}}]
Se não conhecer, retorne: []"""

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
            )
            resp.raise_for_status()
            data_resp = resp.json()
            raw = data_resp["choices"][0]["message"]["content"]

        clean = re.sub(r'```json\s*', '', raw)
        clean = re.sub(r'```\s*', '', clean).strip()
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        results = json.loads(match.group(0)) if match else []
        return {"results": results}
    except httpx.HTTPError as e:
        return {"results": [], "error": f"Erro de rede: {str(e)[:150]}"}
    except (KeyError, IndexError) as e:
        return {"results": [], "error": f"Resposta inesperada da API: {str(e)[:150]}"}
    except Exception as e:
        return {"results": [], "error": str(e)[:200]}

# ═══ History ═══
@router.get("/history")
async def get_history(entity_type: str = None, search: str = None, limit: int = 50, offset: int = 0):
    results = {}
    if not entity_type or entity_type == "interactions":
        results["interactions"] = await interactions_repo.find_all(limit=limit, offset=offset) if not search else await interactions_repo.search(search, ["state","channel","metadata"])
    if not entity_type or entity_type == "turns":
        results["turns"] = await turns_repo.find_all(limit=limit, offset=offset) if not search else await turns_repo.search(search, ["user_text_redacted","output_text_redacted"])
    if not entity_type or entity_type == "envelopes":
        results["envelopes"] = await envelopes_repo.find_all(limit=limit, offset=offset)
    if not entity_type or entity_type == "audit":
        results["audit_log"] = await audit_repo.find_all(limit=limit, offset=offset) if not search else await audit_repo.search(search, ["action","details","entity_type"])
    return results

# ═══ Drift Events §18.2 ═══
@router.get("/drift-events")
async def list_drift_events(release_id: str = None, limit: int = 20):
    f = {}
    if release_id: f["release_id"] = release_id
    return {"events": await drift_repo.find_all(limit=limit, **f)}


# ═══ MCP Test Connection ═══

class MCPTestRequest(BaseModel):
    endpoint: str
    name: Optional[str] = ""
    operations: Optional[str] = "[]"
    auth_type: Optional[str] = ""
    auth_token: Optional[str] = ""
    auth_config: Optional[str] = "{}"

@router.post("/tools/test")
async def test_mcp_connection(data: MCPTestRequest):
    """Testa conexão com MCP Server — HTTP ou stdio.

    NOTA: timeout de stdio elevado para 90s para acomodar a 1ª execução de
    'npx -y <pacote>' que precisa baixar dependências do registry npm.

    CORREÇÃO 2026-04-21: Header Accept adicionado para compatibilidade com
    MCP Streamable HTTP (spec 2025-03-26). Sem ele, servidores como Context7
    retornam HTTP 406 Not Acceptable.
    """
    import httpx, time

    endpoint = data.endpoint.strip()

    # ── Build auth (API Key, OAuth2, mTLS) ──
    auth = _build_mcp_auth(data.auth_type, data.auth_token, data.auth_config)

    # ── Non-HTTP: stdio via subprocess (timeout 90s) ──
    if not endpoint.startswith("http"):
        from app.mcp.runtime import run_stdio_session
        result = await run_stdio_session(command=endpoint, action="test", timeout=90)
        result.setdefault("latency", None)
        result.setdefault("recommendations", [])
        result.setdefault("discovered_tools", [])
        result.setdefault("server_name", result.get("server_name"))
        return result

    # ── OAuth2: buscar token se necessário ──
    if auth.get("oauth_config"):
        token = await _fetch_oauth2_token(auth["oauth_config"])
        if token:
            auth["headers"]["Authorization"] = f"Bearer {token}"
        else:
            _cleanup_mcp_auth(auth)
            return {"success": False, "details": "Falha ao obter token OAuth2",
                    "latency": None, "server_name": None, "discovered_tools": [],
                    "recommendations": [
                        "Verifique client_id, client_secret e token_url.",
                        "Confirme que o grant_type 'client_credentials' está habilitado no Authorization Server.",
                        "Verifique se o scope está correto (pode ser obrigatório).",
                    ]}

    headers = auth["headers"]
    client_kwargs = auth.get("client_kwargs", {})

    start = time.time()
    recommendations = []
    discovered_tools = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, **client_kwargs) as client:
            resp = await client.post(endpoint, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "AgenteInteligencia", "version": "1.0.0"}},
                "id": 1,
            }, headers=headers)
            latency = int((time.time() - start) * 1000)

            if resp.status_code >= 400:
                recs = [f"HTTP {resp.status_code}"]
                if resp.status_code == 404: recs.append("Tente adicionar /sse, /mcp ou /v1 ao endpoint.")
                elif resp.status_code in (401,403): recs.append("Autenticação necessária. Configure Auth.")
                elif resp.status_code == 405: recs.append("Tente /sse ao final da URL (SSE transport).")
                elif resp.status_code == 406: recs.append("Servidor rejeitou o content-type. Verifique se o endpoint suporta MCP Streamable HTTP.")
                return {"success": False, "details": f"HTTP {resp.status_code}", "latency": latency, "server_name": None, "discovered_tools": [], "recommendations": recs}

            # ── Tratar resposta SSE (text/event-stream) ──
            content_type = resp.headers.get("content-type", "")

            if "text/event-stream" in content_type:
                # Servidor respondeu com SSE — extrair JSON do stream
                json_data = _extract_json_from_sse(resp.text)
                if json_data is None:
                    return {"success": False, "details": "SSE sem dados JSON válidos", "latency": latency,
                            "server_name": None, "discovered_tools": [],
                            "recommendations": ["Servidor respondeu com SSE mas não continha dados JSON-RPC válidos.",
                                                 f"Resposta bruta: {resp.text[:200]}"]}
                data_resp = json_data
            else:
                try:
                    data_resp = resp.json()
                except Exception:
                    ct = content_type
                    recs = ["Resposta não-JSON recebida."]
                    if "text/html" in ct:
                        recs.append("Servidor retornou HTML. Verifique se o endpoint MCP está correto.")
                    recs.append(f"Content-Type: {ct}")
                    recs.append(f"Resposta: {resp.text[:150]}")
                    return {"success": False, "details": "Resposta não-JSON", "latency": latency, "server_name": None, "discovered_tools": [], "recommendations": recs}

            if "result" in data_resp:
                server_info = data_resp["result"].get("serverInfo", {})
                server_name = f"{server_info.get('name', '?')} v{server_info.get('version', '?')}"

                try:
                    await client.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                                      headers=headers)
                except: pass

                try:
                    tools_resp = await client.post(endpoint, json={
                        "jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2,
                    }, headers=headers)

                    tools_ct = tools_resp.headers.get("content-type", "")
                    if "text/event-stream" in tools_ct:
                        tools_data = _extract_json_from_sse(tools_resp.text) or {}
                    else:
                        tools_data = tools_resp.json()

                    if "result" in tools_data:
                        for t in tools_data["result"].get("tools", []):
                            discovered_tools.append({"name": t.get("name",""), "description": t.get("description",""), "inputSchema": t.get("inputSchema",{})})
                except: pass

                recommendations.append(f"Servidor: {server_name}")
                if discovered_tools:
                    recommendations.append(f"{len(discovered_tools)} ferramenta(s) descoberta(s)")

                return {"success": True, "details": "MCP Server conectado (JSON-RPC)", "latency": latency,
                        "server_name": server_name, "discovered_tools": discovered_tools, "recommendations": recommendations}

            if "error" in data_resp:
                err = data_resp["error"]
                return {"success": False, "details": f"Erro: {err.get('message', str(err))}", "latency": latency,
                        "server_name": None, "discovered_tools": [],
                        "recommendations": ["Servidor acessível mas retornou erro.", f"Código: {err.get('code','?')}"]}

            return {"success": False, "details": "Resposta JSON sem result/error", "latency": latency,
                    "server_name": None, "discovered_tools": [],
                    "recommendations": ["Servidor respondeu JSON mas não no formato MCP/JSON-RPC.", f"Resposta: {json.dumps(data_resp)[:200]}"]}

    except httpx.ConnectError:
        return {"success": False, "details": "Conexão recusada", "latency": None, "server_name": None, "discovered_tools": [],
                "recommendations": ["Verifique URL, host, porta e firewall.", f"Endpoint: {endpoint}"]}
    except httpx.TimeoutException:
        return {"success": False, "details": "Timeout (15s)", "latency": None, "server_name": None, "discovered_tools": [],
                "recommendations": ["Servidor não respondeu em 15s."]}
    except Exception as e:
        return {"success": False, "details": str(e)[:200], "latency": None, "server_name": None, "discovered_tools": [],
                "recommendations": [f"Erro: {str(e)[:300]}"]}
    finally:
        _cleanup_mcp_auth(auth)


class MCPExecuteRequest(BaseModel):
    endpoint: str
    tool_name: str
    arguments: Optional[dict] = {}
    auth_type: Optional[str] = ""
    auth_token: Optional[str] = ""
    auth_config: Optional[str] = "{}"

@router.post("/tools/execute")
async def execute_mcp_tool(data: MCPExecuteRequest):
    """Executa uma ferramenta MCP via JSON-RPC — HTTP ou stdio.
    Suporta autenticação API Key, OAuth2 Client Credentials e mTLS.
    """
    import httpx, time

    endpoint = data.endpoint.strip()

    # ── Build auth ──
    auth = _build_mcp_auth(data.auth_type, data.auth_token, data.auth_config)

    if not endpoint.startswith("http"):
        from app.mcp.runtime import run_stdio_session
        result = await run_stdio_session(
            command=endpoint, action="call",
            tool_name=data.tool_name, arguments=data.arguments or {},
            timeout=90,
        )
        if result.get("success"):
            return {"success": True, "data": result.get("data", ""), "latency": None}
        return {"success": False, "error": result.get("details", "Erro"), "latency": None,
                "recommendations": result.get("recommendations", [])}

    # ── OAuth2: buscar token se necessário ──
    if auth.get("oauth_config"):
        token = await _fetch_oauth2_token(auth["oauth_config"])
        if token:
            auth["headers"]["Authorization"] = f"Bearer {token}"
        else:
            _cleanup_mcp_auth(auth)
            return {"success": False, "error": "Falha ao obter token OAuth2. Verifique client_id, client_secret e token_url.", "latency": None}

    headers = auth["headers"]
    client_kwargs = auth.get("client_kwargs", {})

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, **client_kwargs) as client:
            await client.post(endpoint, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "AgenteInteligencia", "version": "1.0.0"}},
                "id": 1,
            }, headers=headers)

            try:
                await client.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                                  headers=headers)
            except: pass

            resp = await client.post(endpoint, json={
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": data.tool_name, "arguments": data.arguments or {}},
                "id": 3,
            }, headers=headers)

            latency = int((time.time() - start) * 1000)

            # ── Tratar resposta SSE ──
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                result = _extract_json_from_sse(resp.text)
                if result is None:
                    return {"success": False, "error": "SSE sem dados JSON válidos", "latency": latency}
            else:
                result = resp.json()

            if "result" in result:
                content = result["result"]
                if isinstance(content, dict) and "content" in content:
                    blocks = content["content"]
                    if isinstance(blocks, list):
                        texts = []
                        for b in blocks:
                            if isinstance(b, dict):
                                if b.get("type") == "text": texts.append(b.get("text",""))
                                elif b.get("type") == "resource": texts.append(json.dumps(b.get("resource",{}), indent=2, ensure_ascii=False))
                                else: texts.append(json.dumps(b, indent=2, ensure_ascii=False))
                        return {"success": True, "data": "\n".join(texts), "latency": latency}
                    return {"success": True, "data": json.dumps(content, indent=2, ensure_ascii=False), "latency": latency}
                return {"success": True, "data": json.dumps(content, indent=2, ensure_ascii=False) if isinstance(content, (dict,list)) else str(content), "latency": latency}

            if "error" in result:
                return {"success": False, "error": result["error"].get("message", str(result["error"])), "latency": latency}

            return {"success": True, "data": json.dumps(result, indent=2, ensure_ascii=False), "latency": latency}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout (30s)", "latency": int((time.time()-start)*1000)}
    except Exception as e:
        return {"success": False, "error": str(e)[:300], "latency": int((time.time()-start)*1000)}
    finally:
        _cleanup_mcp_auth(auth)


def _extract_json_from_sse(sse_text: str) -> dict | None:
    """Extrai o primeiro objeto JSON-RPC válido de uma resposta SSE.

    Formato SSE esperado:
        event: message
        data: {"jsonrpc":"2.0","result":{...},"id":1}

    Alguns servidores enviam múltiplas linhas `data:`. Esta função
    concatena todas e tenta parsear o resultado.
    """
    lines = sse_text.strip().split("\n")
    data_parts = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            if payload:
                data_parts.append(payload)

    # Tentar cada parte individualmente primeiro (caso mais comum)
    for part in data_parts:
        try:
            obj = json.loads(part)
            if isinstance(obj, dict) and ("result" in obj or "error" in obj or "jsonrpc" in obj):
                return obj
        except (json.JSONDecodeError, TypeError):
            continue

    # Fallback: concatenar todas as partes
    if data_parts:
        combined = "".join(data_parts)
        try:
            obj = json.loads(combined)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass

    return None


# ═══ Settings (persistidas em SQLite) ═══

class SettingsSave(BaseModel):
    openai_key: Optional[str] = ""
    openai_model: Optional[str] = "gpt-4o"
    maritaca_key: Optional[str] = ""
    maritaca_model: Optional[str] = "sabia-3"
    maritaca_url: Optional[str] = "https://chat.maritaca.ai/api"
    ollama_url: Optional[str] = "http://187.77.46.137:32768"
    ollama_model: Optional[str] = "Gemma-3-Gaia-PT-BR-4b-it-GGUF"
    langfuse_public: Optional[str] = ""
    langfuse_secret: Optional[str] = ""
    langfuse_host: Optional[str] = "https://cloud.langfuse.com"
    max_iterations: Optional[int] = 25
    timeout: Optional[int] = 120
    mesh_groups: Optional[str] = None
    mesh_chain_names: Optional[str] = None

@router.get("/settings")
async def get_settings():
    """Carrega configurações salvas."""
    data = await settings_store.get_all()
    return {"settings": data}

@router.put("/settings")
async def save_settings(data: SettingsSave):
    """Salva configurações na plataforma."""
    settings_dict = {k: str(v) for k, v in data.model_dump().items() if v is not None}
    await settings_store.set_many(settings_dict)
    await audit_repo.create({
        "entity_type": "settings", "entity_id": "platform",
        "action": "settings_saved",
        "details": json.dumps({"keys": list(settings_dict.keys())}),
    })
    return {"message": "Configurações salvas", "keys_saved": len(settings_dict)}


class ProviderTestRequest(BaseModel):
    provider: str  # openai | maritaca | ollama
    model: str
    api_key: Optional[str] = ""
    base_url: Optional[str] = ""


@router.post("/settings/test-provider")
async def test_provider(data: ProviderTestRequest):
    """Testa conectividade com um provedor LLM usando os valores informados.

    Não persiste nada; usa as credenciais/URL recebidas no body para fazer
    uma chamada trivial e medir latência. Retorna {ok, latency_ms, sample, error}.
    """
    import time as _time
    import httpx
    provider = (data.provider or "").lower().strip()
    if provider not in {"openai", "maritaca", "ollama"}:
        raise HTTPException(400, f"Provedor inválido: {data.provider}")
    if not data.model:
        raise HTTPException(400, "Modelo obrigatório")

    # Resolve base_url e api_key conforme provedor
    if provider == "openai":
        base_url = (data.base_url or "https://api.openai.com").rstrip("/")
        chat_url = f"{base_url}/v1/chat/completions" if not base_url.endswith("/v1") else f"{base_url}/chat/completions"
        api_key = data.api_key or ""
        if not api_key:
            return {"ok": False, "error": "API Key obrigatória para OpenAI"}
    elif provider == "maritaca":
        base_url = (data.base_url or "https://chat.maritaca.ai/api").rstrip("/")
        chat_url = f"{base_url}/v1/chat/completions"
        api_key = data.api_key or ""
        if not api_key:
            return {"ok": False, "error": "API Key obrigatória para Maritaca"}
    else:  # ollama
        base_url = (data.base_url or "http://localhost:11434").rstrip("/")
        chat_url = f"{base_url}/v1/chat/completions"
        api_key = data.api_key or "ollama"

    payload = {
        "model": data.model,
        "messages": [
            {"role": "system", "content": "Você é um agente de teste. Responda em uma única palavra."},
            {"role": "user", "content": "Diga: pong"},
        ],
        "temperature": 0.0,
        "max_tokens": 16,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    start = _time.time()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(chat_url, json=payload, headers=headers)
        latency = round((_time.time() - start) * 1000, 0)
        if r.status_code >= 400:
            try:
                err = r.json()
                msg = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else (err.get("error") or err.get("message") or r.text[:300])
            except Exception:
                msg = r.text[:300]
            return {"ok": False, "status": r.status_code, "latency_ms": latency, "error": str(msg)[:400]}
        data_resp = r.json()
        try:
            sample = data_resp["choices"][0]["message"]["content"][:120]
        except Exception:
            sample = ""
        usage = data_resp.get("usage") or {}
        return {
            "ok": True,
            "status": r.status_code,
            "latency_ms": latency,
            "model": data_resp.get("model") or data.model,
            "sample": sample,
            "tokens": usage.get("total_tokens") or 0,
        }
    except httpx.ConnectError as e:
        return {"ok": False, "latency_ms": round((_time.time() - start) * 1000, 0), "error": f"Falha de conexão: {str(e)[:200]}"}
    except httpx.TimeoutException:
        return {"ok": False, "latency_ms": round((_time.time() - start) * 1000, 0), "error": "Timeout (30s)"}
    except Exception as e:
        return {"ok": False, "latency_ms": round((_time.time() - start) * 1000, 0), "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ═══ System Prompts ═══

class SystemPromptCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    category: str = "geral"
    kind: str = "subagent"
    prompt_text: str
    variables: Optional[str] = "[]"
    is_default: bool = False

class SystemPromptUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    kind: Optional[str] = None
    prompt_text: Optional[str] = None
    variables: Optional[str] = None
    is_default: Optional[bool] = None

@router.get("/system-prompts")
async def list_system_prompts(category: str = None, kind: str = None, limit: int = 100):
    f = {}
    if category: f["category"] = category
    if kind: f["kind"] = kind
    return {"prompts": await prompts_repo.find_all(limit=limit, **f), "total": await prompts_repo.count(**f)}

@router.get("/system-prompts/{prompt_id}")
async def get_system_prompt(prompt_id: str):
    p = await prompts_repo.find_by_id(prompt_id)
    if not p: raise HTTPException(404, "Prompt não encontrado")
    return p

@router.post("/system-prompts", status_code=201)
async def create_system_prompt(data: SystemPromptCreate):
    pid = str(uuid.uuid4())
    d = {"id": pid, **data.model_dump()}
    d["is_default"] = 1 if data.is_default else 0
    await prompts_repo.create(d)
    await audit_repo.create({"entity_type":"system_prompt","entity_id":pid,"action":"created","details":json.dumps({"name":data.name})})
    return {"id": pid, "message": "System prompt criado"}

@router.put("/system-prompts/{prompt_id}")
async def update_system_prompt(prompt_id: str, data: SystemPromptUpdate):
    existing = await prompts_repo.find_by_id(prompt_id)
    if not existing: raise HTTPException(404)
    upd = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "is_default" in upd: upd["is_default"] = 1 if upd["is_default"] else 0
    if "prompt_text" in upd: upd["version"] = existing.get("version", 1) + 1
    if not upd:
        return existing
    return await prompts_repo.update(prompt_id, upd)

@router.delete("/system-prompts/{prompt_id}")
async def delete_system_prompt(prompt_id: str):
    if not await prompts_repo.delete(prompt_id): raise HTTPException(404)
    return {"message": "System prompt removido"}


# ═══ Busca Global ═══

@router.get("/search")
async def global_search(q: str = "", limit: int = 5):
    """Busca global em agentes, skills, sessões, prompts e bases de conhecimento."""
    if not q or len(q.strip()) < 2:
        return {"results": []}

    results = []

    agents = await agents_repo.search(q, ["name", "description", "domain", "system_prompt"])
    for a in agents[:limit]:
        results.append({"type": "agent", "id": a["id"], "title": a["name"], "subtitle": f"{a.get('kind','')} · {a.get('model','')}", "url": f"/agents/{a['id']}/edit", "icon": "agent"})

    skills = await skills_repo.search(q, ["name", "purpose", "domain", "raw_content"])
    for s in skills[:limit]:
        results.append({"type": "skill", "id": s["id"], "title": s["name"], "subtitle": f"{s.get('kind','')} · v{s.get('version','')}", "url": f"/skills/{s['id']}/edit", "icon": "skill"})

    sessions = await interactions_repo.search(q, ["title", "channel", "state"])
    for s in sessions[:limit]:
        results.append({"type": "session", "id": s["id"], "title": s.get("title") or "Sessão", "subtitle": f"{s.get('state','')} · {s.get('channel','')}", "url": f"/workspace?session={s['id']}", "icon": "session"})

    prompts = await prompts_repo.search(q, ["name", "description", "prompt_text", "category"])
    for p in prompts[:limit]:
        results.append({"type": "prompt", "id": p["id"], "title": p["name"], "subtitle": f"{p.get('kind','')} · {p.get('category','')}", "url": "/settings", "icon": "prompt"})

    sources = await knowledge_repo.search(q, ["name", "description", "source_type"])
    for s in sources[:limit]:
        results.append({"type": "knowledge", "id": s["id"], "title": s["name"], "subtitle": s.get("source_type", ""), "url": "/evidence", "icon": "knowledge"})

    return {"results": results, "total": len(results), "query": q}
