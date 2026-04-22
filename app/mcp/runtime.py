"""MCP Tool Runtime — parse bindings, register as LLM tools, execute via HTTP e stdio.

Suporta:
- HTTP endpoints → JSON-RPC POST direto (com suporte a MCP Streamable HTTP / SSE)
- Stdio endpoints (npx, node, python) → spawn subprocess + JSON-RPC via stdin/stdout

CORREÇÕES (2026-04):
- Stderr lido em background concorrente (visibilidade real do erro mesmo com processo morto rápido)
- Timeout default subido para 90s (npx -y precisa baixar pacote na 1ª execução)
- Filtros explícitos para mensagens do npm (npm warn, Need to install, Packages installed)
- Diagnóstico mapeado por tipo de erro (ENOENT, EACCES, ECONNREFUSED, Cannot find module)

CORREÇÃO (2026-04-21):
- Header `Accept: application/json, text/event-stream` adicionado a TODAS as chamadas
  HTTP para MCP servers. Sem esse header, servidores que usam o transporte MCP Streamable
  HTTP (spec 2025-03-26) retornam HTTP 406 Not Acceptable (ex: Context7).
- Respostas SSE (text/event-stream) são parseadas automaticamente para extrair JSON-RPC.
"""

import re
import json
import logging
import asyncio
import shlex
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Headers padrão para chamadas HTTP a MCP Servers.
# O transporte MCP Streamable HTTP (spec 2025-03-26) exige que
# o cliente declare Accept com ambos os tipos: o servidor pode
# responder com JSON (respostas curtas) ou SSE (long-running).
# Sem esse header, servidores como Context7 retornam HTTP 406.
# ═══════════════════════════════════════════════════════════════
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Cache por endpoint dos tools expostos via `tools/list` (MCP spec).
# Evita uma chamada extra por invocação sem exigir invalidation manual
# (servidores estáveis em prazo curto). TTL não é crítico para Fase 1.
_MCP_TOOLS_LIST_CACHE: dict[str, list[dict]] = {}


def _extract_json_from_sse(sse_text: str) -> dict | None:
    """Extrai o primeiro objeto JSON-RPC válido de uma resposta SSE.

    Formato SSE esperado:
        event: message
        data: {"jsonrpc":"2.0","result":{...},"id":1}

    Alguns servidores enviam múltiplas linhas `data:`. Esta função
    tenta cada parte individualmente e depois concatena como fallback.
    """
    lines = sse_text.strip().split("\n")
    data_parts = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            if payload:
                data_parts.append(payload)

    # Tentar cada parte individualmente (caso mais comum)
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


# ═══════════════════════════════════════════════════
# Stdio MCP Client — subprocess communication
# ═══════════════════════════════════════════════════

class StdioMCPClient:
    """Comunica com MCP Server via stdin/stdout JSON-RPC."""

    def __init__(self, command: str, timeout: int = 90):
        self.command = command
        self.timeout = timeout
        self.process = None
        self._id = 0
        self._stderr_buf = []
        self._stderr_task = None

    async def start(self):
        """Spawna o processo MCP e inicia leitor de stderr concorrente."""
        import sys
        is_windows = sys.platform == "win32"

        if is_windows:
            # Windows: npx.cmd precisa de shell=True
            self.process = await asyncio.create_subprocess_shell(
                self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            parts = shlex.split(self.command)
            self.process = await asyncio.create_subprocess_exec(
                *parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        # Buffer de stderr alimentado em background — captura tudo, não só pós-morte
        self._stderr_buf = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        logger.info(f"MCP stdio process started: {self.command} (pid={self.process.pid})")

    async def _drain_stderr(self):
        """Lê stderr continuamente em background para que esteja disponível ao diagnóstico."""
        try:
            while self.process and self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors='replace').rstrip()
                if decoded:
                    self._stderr_buf.append(decoded)
                    logger.debug(f"MCP stderr: {decoded}")
        except Exception:
            pass

    async def close(self):
        """Encerra o processo e o leitor de stderr."""
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        if self.process and self.process.returncode is None:
            try:
                self.process.stdin.close()
            except Exception:
                pass
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try: self.process.kill()
                except Exception: pass
            logger.info("MCP stdio process terminated")

    def _stderr_snapshot(self, limit: int = 1500) -> str:
        """Retorna snapshot do stderr acumulado para diagnóstico."""
        return "\n".join(self._stderr_buf)[:limit] if self._stderr_buf else ""

    async def _send_receive(self, method: str, params: dict = None) -> dict:
        """Envia JSON-RPC via stdin e lê resposta de stdout."""
        if not self.process or self.process.returncode is not None:
            stderr_text = self._stderr_snapshot()
            raise RuntimeError(f"Processo MCP não está rodando. stderr: {stderr_text or '(vazio)'}")

        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params is not None:
            msg["params"] = params

        payload = json.dumps(msg) + "\n"
        try:
            self.process.stdin.write(payload.encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            stderr_text = self._stderr_snapshot()
            raise RuntimeError(f"Pipe quebrado ao escrever para MCP: {e}. stderr: {stderr_text or '(vazio)'}")

        # Filtros para output não-JSON gerado pelo npm/npx durante bootstrap
        skip_patterns = (
            "npm warn", "npm notice", "need to install", "ok to proceed",
            "packages installed", "downloading", "added ", "audited ",
            "looking for", "fetching", "resolving", "deprecated",
            "found 0 vulnerabilities",
        )

        deadline = asyncio.get_event_loop().time() + self.timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                stderr_text = self._stderr_snapshot()
                raise TimeoutError(
                    f"MCP Server não respondeu em {self.timeout}s. "
                    f"stderr: {stderr_text or '(vazio)'}"
                )
            try:
                line = await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=max(remaining, 1)
                )
            except asyncio.TimeoutError:
                stderr_text = self._stderr_snapshot()
                raise TimeoutError(
                    f"MCP Server não respondeu em {self.timeout}s. "
                    f"stderr: {stderr_text or '(vazio)'}"
                )

            if not line:
                # Processo morreu — usar buffer de stderr acumulado em vez de leitura tardia
                stderr_text = self._stderr_snapshot()
                rc = self.process.returncode
                raise RuntimeError(
                    f"Processo encerrou (returncode={rc}). stderr completo:\n{stderr_text or '(vazio)'}"
                )

            decoded = line.decode(errors='replace').strip()
            if not decoded:
                continue

            # Tentar parsear como JSON-RPC
            try:
                data = json.loads(decoded)
                if isinstance(data, dict) and ("jsonrpc" in data or "result" in data or "error" in data):
                    return data
            except json.JSONDecodeError:
                low = decoded.lower()
                if any(p in low for p in skip_patterns):
                    logger.debug(f"MCP stdio skip npm bootstrap: {decoded[:120]}")
                else:
                    logger.debug(f"MCP stdio skip non-JSON: {decoded[:120]}")
                continue

    async def _send_notification(self, method: str, params: dict = None):
        """Envia notificação (sem id, sem resposta esperada)."""
        if not self.process or self.process.returncode is not None:
            return
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        payload = json.dumps(msg) + "\n"
        try:
            self.process.stdin.write(payload.encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def initialize(self) -> dict:
        """Handshake MCP: initialize + notifications/initialized."""
        result = await self._send_receive("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "AgenteInteligencia", "version": "1.0.0"},
        })
        await self._send_notification("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        """Lista ferramentas disponíveis."""
        result = await self._send_receive("tools/list", {})
        if "result" in result:
            return result["result"].get("tools", [])
        return []

    async def call_tool(self, name: str, arguments: dict = None) -> str:
        """Chama uma ferramenta e retorna resultado como texto."""
        result = await self._send_receive("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

        if "result" in result:
            content = result["result"]
            if isinstance(content, dict) and "content" in content:
                blocks = content["content"]
                if isinstance(blocks, list):
                    texts = []
                    for b in blocks:
                        if isinstance(b, dict):
                            if b.get("type") == "text":
                                texts.append(b.get("text", ""))
                            else:
                                texts.append(json.dumps(b, indent=2, ensure_ascii=False))
                    return "\n".join(texts) if texts else json.dumps(content, indent=2, ensure_ascii=False)
                return str(content)
            return json.dumps(content, indent=2, ensure_ascii=False) if isinstance(content, (dict, list)) else str(content)

        if "error" in result:
            return json.dumps({"error": result["error"]})

        return json.dumps(result)


async def run_stdio_session(command: str, action: str = "test", tool_name: str = None, arguments: dict = None, timeout: int = 90):
    """Sessão completa stdio: spawn → initialize → ação → cleanup.

    action: 'test' (handshake + list), 'call' (handshake + call tool)
    timeout: 90s default — suficiente para primeira execução de `npx -y` que precisa baixar pacote
    """
    client = StdioMCPClient(command, timeout=timeout)
    try:
        await client.start()
        init_result = await client.initialize()

        server_info = init_result.get("result", {}).get("serverInfo", {})
        server_name = f"{server_info.get('name', '?')} v{server_info.get('version', '?')}"

        if action == "test":
            tools = await client.list_tools()
            discovered = [{"name": t.get("name", ""), "description": t.get("description", ""), "inputSchema": t.get("inputSchema", {})} for t in tools]
            return {
                "success": True,
                "server_name": server_name,
                "discovered_tools": discovered,
                "details": f"Stdio MCP conectado ({len(discovered)} ferramentas)",
            }

        elif action == "call" and tool_name:
            result_text = await client.call_tool(tool_name, arguments or {})
            return {"success": True, "data": result_text}

        return {"success": True, "details": "Conectado"}

    except FileNotFoundError as e:
        cmd_base = command.split()[0] if command else "?"
        return {
            "success": False,
            "details": f"Comando '{cmd_base}' não encontrado",
            "recommendations": [
                f"O comando '{cmd_base}' não está instalado neste sistema.",
                "Para npx: instale Node.js (v18+) com 'apt install nodejs npm' ou via nvm.",
                "Para python: verifique se o pacote está instalado no virtualenv.",
                "Após instalar, reinicie o servidor da aplicação.",
            ],
        }
    except TimeoutError as e:
        return {
            "success": False,
            "details": str(e)[:400],
            "recommendations": [
                f"O processo MCP foi iniciado mas não respondeu no tempo esperado ({timeout}s).",
                "Para 'npx -y': a primeira execução baixa o pacote e suas dependências (10-40MB). Pré-instale globalmente para eliminar esse overhead: 'npm install -g <pacote>'.",
                "Verifique se o pacote MCP existe: 'npm info <pacote>'.",
                "Verifique conectividade com registry.npmjs.org (proxy/firewall corporativo).",
                "Teste manualmente no shell do servidor: 'echo {} | <seu-comando>' para ver o output bruto.",
            ],
        }
    except RuntimeError as e:
        msg = str(e)
        msg_low = msg.lower()
        recs = ["O processo MCP encerrou inesperadamente."]
        if "npm err" in msg_low or "404" in msg or "not found" in msg_low:
            recs.append("Pacote npm não encontrado. Verifique o nome exato: 'npm info <pacote>'.")
        if "enoent" in msg_low:
            recs.append("Node.js/npx não está instalado ou não está no PATH do servidor. Instale Node 20 LTS.")
        if "permission denied" in msg_low or "eacces" in msg_low:
            recs.append("Erro de permissão. Verifique se o usuário do servidor pode executar npx/node e tem acesso ao cache npm.")
        if "econnrefused" in msg_low or "etimedout" in msg_low or "network" in msg_low or "getaddrinfo" in msg_low:
            recs.append("Erro de rede. Verifique proxy/firewall para registry.npmjs.org. Configure 'npm config set proxy <url>' se necessário.")
        if "cannot find module" in msg_low or "module not found" in msg_low:
            recs.append("Dependências do pacote MCP faltando. Tente pré-instalar globalmente: 'npm install -g <pacote>'.")
        if "exit code 1" in msg_low or "returncode=1" in msg_low:
            recs.append("Pacote MCP iniciou mas falhou. Verifique se requer flags ou variáveis de ambiente (ex: API_KEY).")
        recs.append("Sugestão: substitua 'npx -y @upstash/context7-mcp' pelo endpoint HTTP remoto 'https://mcp.context7.com/mcp' (mais estável, sem dependência de Node).")
        recs.append(f"Saída de erro completa:\n{msg[:600]}")
        return {"success": False, "details": "Processo encerrou com erro", "recommendations": recs}
    except Exception as e:
        return {"success": False, "details": str(e)[:300], "recommendations": [f"Erro inesperado: {str(e)[:300]}"]}
    finally:
        await client.close()


# ═══════════════════════════════════════════════════
# Parse / Match / Build
# ═══════════════════════════════════════════════════

def parse_tool_bindings(bindings_text: str) -> list[dict]:
    """Extrai ferramentas declaradas no ## Tool Bindings do SKILL.md."""
    if not bindings_text or not bindings_text.strip():
        return []
    tools = []
    entries = re.split(r'^- \*\*', bindings_text, flags=re.MULTILINE)
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        tool = {}
        name_match = re.match(r'(.+?)\*\*', entry)
        if name_match:
            tool['name'] = name_match.group(1).strip()
        for line in entry.split('\n'):
            line = line.strip().lstrip('- ')
            if ':' in line:
                key, val = line.split(':', 1)
                key = key.strip().lower()
                val = val.strip()
                if 'servidor' in key or 'server' in key or 'mcp' in key:
                    tool['mcp_server'] = val
                elif 'opera' in key:
                    tool['operations'] = [op.strip() for op in val.split(',') if op.strip()]
                elif 'classifica' in key or 'sensitivity' in key:
                    tool['sensitivity'] = val
                elif 'timeout' in key:
                    tool['timeout'] = val
                elif 'condi' in key:
                    tool['condition'] = val
        if tool.get('name'):
            tools.append(tool)
    return tools


async def match_with_registry(parsed_tools: list[dict], tools_repo) -> list[dict]:
    """Cruza ferramentas do SKILL.md com o registro no banco."""
    if not parsed_tools:
        return []
    registered = await tools_repo.find_all(limit=200)
    reg_map = {t['name'].lower(): dict(t) for t in registered}
    enriched = []
    for pt in parsed_tools:
        name_lower = pt.get('name', '').lower()
        matched = reg_map.get(name_lower)
        if not matched:
            for rname, rdata in reg_map.items():
                if name_lower in rname or rname in name_lower:
                    matched = rdata
                    break
        if matched:
            pt['mcp_server'] = matched.get('mcp_server') or pt.get('mcp_server', '')
            pt['description'] = matched.get('description') or ''
            pt['db_id'] = matched.get('id', '')
            pt['auth_requirements'] = matched.get('auth_requirements', '')
            if not pt.get('operations') and matched.get('operations'):
                try: pt['operations'] = json.loads(matched['operations'])
                except: pt['operations'] = []
        enriched.append(pt)
    return enriched


def build_openai_tools(mcp_tools: list[dict]) -> list[dict]:
    """Converte ferramentas MCP em definições OpenAI function calling.

    O campo `description` é o sinal mais forte que o LLM usa para decidir
    invocar a função — precisa ser específico, não genérico.
    """
    if not mcp_tools:
        return []
    openai_tools = []
    for tool in mcp_tools:
        raw_name = tool.get('name', 'tool') or 'tool'
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name).strip('_')[:64]
        ops = tool.get('operations', []) or []
        user_desc = (tool.get('description') or '').strip()
        ops_list = ', '.join(ops) if ops else 'não listadas'
        # Descrição rica: nome humano + operações + instrução de uso
        desc_parts = [
            f"Ferramenta MCP '{raw_name}'. Operações disponíveis: {ops_list}.",
            "Chame esta função sempre que o usuário solicitar dados que exijam "
            "busca externa, pesquisa na web, consulta de documentação, extração "
            "de conteúdo ou qualquer informação que não esteja no contexto atual.",
        ]
        if user_desc:
            desc_parts.insert(1, user_desc[:300])
        desc = ' '.join(desc_parts)

        properties = {
            "operation": {
                "type": "string",
                "description": (
                    f"Operação a executar. Disponíveis: {ops_list}."
                    if ops else "Operação a executar."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Consulta/parâmetros para a operação. Para 'search' use a "
                    "pergunta do usuário em linguagem natural. Para 'extract' use "
                    "a URL ou identificador. Para 'crawl'/'map' use a URL-raiz."
                ),
            },
        }
        if ops:
            properties["operation"]["enum"] = ops
        openai_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc[:900],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": ["operation", "query"],
                },
            },
        })
    return openai_tools


# ═══════════════════════════════════════════════════
# Execute — HTTP + Stdio unified
# ═══════════════════════════════════════════════════

async def _discover_server_tools(client: "httpx.AsyncClient", endpoint: str, headers: dict) -> list[dict]:
    """Chama MCP tools/list no servidor e cacheia por endpoint.

    Retorna lista de dicts {name, description, inputSchema}. Vazio em caso
    de falha — caller deve ser resiliente a lista vazia.
    """
    cached = _MCP_TOOLS_LIST_CACHE.get(endpoint)
    if cached is not None:
        return cached
    try:
        resp = await client.post(endpoint, json={
            "jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 99,
        }, headers=headers)
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            data = _extract_json_from_sse(resp.text)
        else:
            try:
                data = resp.json()
            except Exception:
                data = None
        tools = []
        if isinstance(data, dict):
            result = data.get("result") or {}
            if isinstance(result, dict):
                tools = result.get("tools") or []
        if not isinstance(tools, list):
            tools = []
        _MCP_TOOLS_LIST_CACHE[endpoint] = tools
        return tools
    except Exception as e:
        logger.warning(f"tools/list falhou para {endpoint}: {e}")
        _MCP_TOOLS_LIST_CACHE[endpoint] = []
        return []


def _resolve_tool_name(declared: str, server_tools: list[dict]) -> str:
    """Mapeia nome declarado (ex: 'search') para nome real exposto
    pelo servidor (ex: 'tavily_search').

    Estratégia:
      1. Match exato.
      2. Servidor expõe '<prefix>_<declared>' (ex: 'tavily_search' ⊃ 'search').
      3. 'declared' é um dos tokens do nome (split por '_' ou '-').
      4. Substring case-insensitive.
    Fallback: retorna `declared` — servidor decide se aceita ou rejeita.
    """
    if not declared or not server_tools:
        return declared
    names = [t.get("name", "") for t in server_tools if isinstance(t, dict)]
    # 1. exato
    if declared in names:
        return declared
    low = declared.lower()
    # 2. suffix após underscore/hífen
    for n in names:
        nl = n.lower()
        if nl.endswith(f"_{low}") or nl.endswith(f"-{low}") or nl.startswith(f"{low}_") or nl.startswith(f"{low}-"):
            return n
    # 3. token match
    for n in names:
        tokens = set(re.split(r'[_\-\s]+', n.lower()))
        if low in tokens:
            return n
    # 4. substring
    for n in names:
        if low in n.lower():
            return n
    return declared


def _build_call_arguments(
    actual_name: str,
    query: str,
    raw_arguments: dict,
    server_tools: list[dict],
) -> dict:
    """Monta o dict de arguments respeitando o inputSchema quando conhecido.

    Se o servidor MCP expôs inputSchema para este tool:
      - preserva apenas propriedades declaradas
      - mapeia 'query' para o primeiro campo `required` string (se houver)
    Caso contrário, usa {'query': ...} como fallback histórico.
    """
    extras = {k: v for k, v in (raw_arguments or {}).items() if k not in ("operation", "query")}
    # Acha o tool spec
    tool_spec = None
    for t in server_tools or []:
        if isinstance(t, dict) and t.get("name") == actual_name:
            tool_spec = t
            break

    if not tool_spec:
        return {"query": query, **extras}

    schema = tool_spec.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    if not isinstance(properties, dict):
        return {"query": query, **extras}

    args: dict = {}
    placed = False
    req_set = set(r for r in required if isinstance(r, str))

    # Prioridade 1: campo 'query' explicitamente REQUIRED (ex: tavily_search)
    if "query" in req_set and "query" in properties:
        args["query"] = query
        placed = True

    # Prioridade 2: primeiro required string (ex: context7 'libraryName')
    if not placed:
        for rname in required:
            if not isinstance(rname, str) or rname in args:
                continue
            spec = properties.get(rname, {}) or {}
            if spec.get("type") == "string":
                args[rname] = query
                placed = True
                break

    # Prioridade 3: primeiro required array-of-strings — empacota query como lista
    # (ex: tavily_extract espera `urls: [...]`)
    if not placed:
        for rname in required:
            if not isinstance(rname, str) or rname in args:
                continue
            spec = properties.get(rname, {}) or {}
            if spec.get("type") == "array":
                items_type = (spec.get("items") or {}).get("type")
                if items_type == "string":
                    raw_extra = extras.pop(rname, None)
                    if raw_extra is None:
                        args[rname] = [query]
                    elif isinstance(raw_extra, list):
                        args[rname] = raw_extra
                    else:
                        args[rname] = [raw_extra]
                    placed = True
                    break

    # Prioridade 4: 'query' existe como propriedade opcional e não consumimos
    # nenhum required — ainda assim colocar em query (caso tool sem required)
    if not placed and "query" in properties:
        args["query"] = query
        placed = True

    # Prioridade 5 (fallback): força 'query' mesmo fora do schema
    if not placed:
        args["query"] = query

    # Propaga extras que existem no schema (preserva overrides do LLM)
    for k, v in extras.items():
        if k in properties and k not in args:
            args[k] = v
    return args


async def execute_tool_call(tool_name: str, arguments: dict, mcp_tools: list[dict], timeout: int = 60) -> str:
    """Executa chamada ao MCP Server — HTTP ou stdio automaticamente.

    CORREÇÃO 2026-04-21: Header Accept adicionado para MCP Streamable HTTP.
    Respostas SSE são parseadas automaticamente.
    """
    tool_config = None
    clean_name = tool_name.lower().replace('_', ' ')
    for t in mcp_tools:
        t_clean = t.get('name', '').lower().replace('_', ' ')
        if clean_name == t_clean or tool_name == re.sub(r'[^a-zA-Z0-9_-]', '_', t.get('name', '')).strip('_'):
            tool_config = t
            break
    if not tool_config:
        return json.dumps({"error": f"Ferramenta '{tool_name}' não encontrada"})

    endpoint = tool_config.get('mcp_server', '')
    operation = arguments.get('operation', '')
    query = arguments.get('query', '')

    # ── Build auth (API Key / OAuth2 / mTLS) ──
    auth_type = tool_config.get('auth_requirements', '')
    auth_token = tool_config.get('auth_token', '')
    auth_config_raw = tool_config.get('auth_config', '{}')
    headers = {**MCP_HEADERS}
    client_kwargs = {}
    temp_files = []

    try:
        auth_config = json.loads(auth_config_raw) if auth_config_raw else {}
    except (ValueError, TypeError):
        auth_config = {}

    if auth_type == 'api_key' and auth_token and auth_token.strip():
        headers["Authorization"] = f"Bearer {auth_token.strip()}"

    elif auth_type == 'oauth2' and auth_config.get('client_id'):
        token = await _fetch_oauth2_token_runtime(auth_config)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            return json.dumps({"error": "Falha ao obter token OAuth2. Verifique client_id, client_secret e token_url."})

    elif auth_type == 'mTLS' and auth_config.get('client_cert'):
        import tempfile, os
        cert_pem = auth_config.get('client_cert', '')
        key_pem = auth_config.get('client_key', '')
        if cert_pem and key_pem:
            cf = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            cf.write(cert_pem); cf.close(); temp_files.append(cf.name)
            kf = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
            kf.write(key_pem); kf.close(); temp_files.append(kf.name)
            client_kwargs["cert"] = (cf.name, kf.name)
            ca_pem = auth_config.get('ca_cert', '')
            if ca_pem:
                caf = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
                caf.write(ca_pem); caf.close(); temp_files.append(caf.name)
                client_kwargs["verify"] = caf.name

    elif auth_token and auth_token.strip():
        # Fallback genérico
        headers["Authorization"] = f"Bearer {auth_token.strip()}"

    # ── Stdio endpoints ──
    if not endpoint.startswith('http'):
        try:
            result = await run_stdio_session(
                command=endpoint, action="call",
                tool_name=operation or tool_name,
                arguments={"query": query, **{k: v for k, v in arguments.items() if k not in ('operation', 'query')}},
                timeout=timeout,
            )
            if result.get("success"):
                return result.get("data", "{}")
            return json.dumps({"error": result.get("details", "Erro stdio")})
        except Exception as e:
            return json.dumps({"error": f"Erro stdio: {str(e)[:300]}"})

    # ── HTTP endpoints ──
    try:
        async with httpx.AsyncClient(timeout=timeout, **client_kwargs) as client:
            try:
                await client.post(endpoint, json={
                    "jsonrpc": "2.0", "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "AgenteInteligencia", "version": "1.0.0"}},
                    "id": 0,
                }, headers=headers)
                await client.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, headers=headers)
            except: pass

            # Descobre nomes reais dos tools do servidor (MCP tools/list) e
            # mapeia a operação declarada no SKILL (ex: "search") para o
            # nome real exposto (ex: "tavily_search"). Essencial porque o
            # SKILL.md frequentemente usa nomes curtos/genéricos e o servidor
            # MCP prefixa com o próprio nome (tavily_*, context7_*).
            declared_name = operation or tool_name
            server_tools = await _discover_server_tools(client, endpoint, headers)
            actual_name = _resolve_tool_name(declared_name, server_tools)
            if actual_name != declared_name:
                logger.info(f"MCP name map: '{declared_name}' → '{actual_name}' @ {endpoint}")

            # Constrói arguments respeitando o inputSchema do tool (quando
            # conhecido). Fallback para {"query": ...} se o schema não
            # estiver disponível.
            call_args = _build_call_arguments(actual_name, query, arguments, server_tools)

            payload = {
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": actual_name, "arguments": call_args},
                "id": 1,
            }
            resp = await client.post(endpoint, json=payload, headers=headers)

            # ── Tratar resposta SSE ──
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                data = _extract_json_from_sse(resp.text)
                if data is None:
                    return json.dumps({"error": "SSE sem dados JSON válidos", "raw": resp.text[:300]})
            else:
                data = resp.json()

            if "result" in data:
                result = data["result"]
                if isinstance(result, dict) and "content" in result:
                    contents = result["content"]
                    if isinstance(contents, list):
                        texts = [c.get("text", str(c)) for c in contents if isinstance(c, dict)]
                        return "\n".join(texts) if texts else json.dumps(result)
                    return str(contents)
                return json.dumps(result) if isinstance(result, (dict, list)) else str(result)
            if "error" in data:
                return json.dumps({"error": data["error"]})
            return json.dumps(data)
    except httpx.TimeoutException:
        return json.dumps({"error": f"Timeout ({timeout}s)"})
    except Exception as e:
        return json.dumps({"error": str(e)[:300]})
    finally:
        # Cleanup temp files (mTLS certs)
        import os
        for tf in temp_files:
            try: os.unlink(tf)
            except: pass


# Cache de tokens OAuth2 para runtime
_runtime_oauth2_cache: dict = {}


async def _fetch_oauth2_token_runtime(config: dict) -> str:
    """Busca access_token via OAuth2 Client Credentials Grant (runtime).

    Cacheia até expiração. Retorna string vazia em caso de falha.
    """
    import time as _time

    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    token_url = config.get("token_url", "")
    scope = config.get("scope", "")

    if not client_id or not client_secret or not token_url:
        return ""

    # Check cache
    if client_id in _runtime_oauth2_cache:
        cached = _runtime_oauth2_cache[client_id]
        if cached.get("expires_at", 0) > _time.time():
            return cached["access_token"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
            if scope:
                payload["scope"] = scope

            resp = await client.post(token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
            if resp.status_code != 200:
                logger.warning(f"OAuth2 runtime token fetch failed: HTTP {resp.status_code}")
                return ""

            data = resp.json()
            access_token = data.get("access_token", "")
            expires_in = data.get("expires_in", 3600)

            _runtime_oauth2_cache[client_id] = {
                "access_token": access_token,
                "expires_at": _time.time() + max(expires_in - 60, 60),
            }
            logger.info(f"OAuth2 runtime token obtained for client_id={client_id[:8]}...")
            return access_token
    except Exception as e:
        logger.warning(f"OAuth2 runtime token error: {e}")
        return ""