"""Rotas do Wizard IA — geração assistida de agentes e skills.

Wave Wizard Routing (PR atual): integra os 3 wizards (agent/skill/refine)
ao sistema de roteamento por task_type da Onda 7 (`app/llm_routing.py`).

Antes cada wizard recebia `provider` + `model` do frontend (dropdown manual).
Agora envia `task_type` semântico — backend resolve via `resolve_llm_for_task`
consultando os pares configurados em /settings → Roteamento LLM. Mesmo
sistema que agents usam em runtime — consistência total.

Retrocompat: clients antigos que enviam `provider/model` continuam
funcionando (legacy path quando task_type não vem).

Defaults sensatos por wizard:
- /skill   → reasoning  (planejar workflow + failure modes + guardrails)
- /agent   → reasoning  (planejar system_prompt + skills + tools)
- /refine  → instruct   (refinar texto existente é instruction-following)
"""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from app.core.llm_providers import get_provider
from app.llm_routing import resolve_llm_for_task
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wizard", tags=["wizard"])


# Defaults por rota — usado quando frontend não enviar task_type.
# Os valores batem com TASK_TYPES de app/llm_routing.py.
_DEFAULT_TASK_TYPE = {
    "agent": "reasoning",
    "skill": "reasoning",
    "refine": "instruct",
}


async def _resolve_wizard_llm(data, route_name: str) -> tuple[str, str, str]:
    """Resolve (provider, model, task_type) para uma requisição de wizard.

    Estratégia:
    1. Se data.task_type vier preenchido → resolve via roteamento global.
    2. Se data.provider vier preenchido E for diferente do default antigo
       ("openai" ou "azure") → respeita escolha legacy (compatibilidade
       com clients que ainda mandam dropdown manual).
    3. Caso nenhum acima → usa default da rota (reasoning/instruct).

    Returns:
        (provider, model, task_type_effective)
        task_type pode vir vazio "" se legacy path foi usado.

    Logs todo resolve pra debug ("qual modelo o wizard usou hoje?").
    """
    explicit_task = (getattr(data, "task_type", "") or "").strip()

    # Caso 1: task_type explícito — caminho moderno.
    if explicit_task:
        provider, model = await resolve_llm_for_task(explicit_task)
        logger.info(
            "wizard.llm.resolved_via_task_type",
            extra={
                "event": "wizard.llm.resolved",
                "wizard_route": route_name,
                "task_type": explicit_task,
                "provider": provider,
                "model": model,
                "source": "task_type",
            },
        )
        return provider, model, explicit_task

    # Caso 2: legacy — client antigo mandou provider/model explícitos.
    # Heurística pra detectar "default vs intenção real": se provider veio
    # vazio OU igual ao default ("openai"), trata como "use o padrão" e cai
    # no roteamento global. Se veio algo específico ("maritaca", "ollama"),
    # respeita.
    legacy_provider = (getattr(data, "provider", "") or "").strip().lower()
    legacy_model = (getattr(data, "model", "") or "").strip()
    if legacy_provider and legacy_provider not in ("openai", "azure"):
        logger.info(
            "wizard.llm.resolved_via_legacy_provider",
            extra={
                "event": "wizard.llm.resolved",
                "wizard_route": route_name,
                "provider": legacy_provider,
                "model": legacy_model or "(default)",
                "source": "legacy_explicit",
            },
        )
        return legacy_provider, legacy_model, ""

    # Caso 3: nada explícito → default da rota.
    fallback_task = _DEFAULT_TASK_TYPE.get(route_name, "reasoning")
    provider, model = await resolve_llm_for_task(fallback_task)
    logger.info(
        "wizard.llm.resolved_via_default",
        extra={
            "event": "wizard.llm.resolved",
            "wizard_route": route_name,
            "task_type": fallback_task,
            "provider": provider,
            "model": model,
            "source": "route_default",
        },
    )
    return provider, model, fallback_task


class WizardAgentRequest(BaseModel):
    description: str
    domain: Optional[str] = ""
    # Wave Wizard Routing: task_type vira o jeito moderno de escolher LLM.
    # Frontend novo manda task_type=reasoning (default da rota /agent);
    # backend resolve via /settings → Roteamento LLM.
    task_type: Optional[str] = ""
    # Legacy (retrocompat). Clients antigos que ainda escolhem dropdown manual.
    # Quando task_type vier preenchido, estes campos são ignorados.
    provider: str = "openai"
    model: Optional[str] = ""  # vazio → provider usa default da config


class WizardSkillRequest(BaseModel):
    """Request do Wizard IA para gerar SKILL.md.

    Wave Wizard UX (PR atual): aceita IDs ESTRUTURADOS dos bindings (MCP, RAG,
    Tabelas, APIs). Backend resolve nomes humanos via lookup e monta o prompt
    enriquecido — antes o frontend concatenava texto no campo `description`
    (gambiarra frágil quando LLM ignorava instruções).

    Retrocompat: campos novos têm default vazio. Clients antigos que mandam só
    `description, kind, domain, provider` continuam funcionando — apenas perdem
    o enriquecimento estruturado.
    """
    description: str
    kind: str = "subagent"
    domain: Optional[str] = ""
    # Wave Wizard Routing: task_type=reasoning por default da rota /skill.
    task_type: Optional[str] = ""
    # Legacy (retrocompat — quando task_type vier, ignora).
    provider: str = "openai"
    model: Optional[str] = ""
    # Wave Wizard UX: bindings declarados explicitamente em vez de texto livre.
    # Backend faz lookup nos repositórios e injeta nome+id no prompt.
    mcp_tool_ids: list[str] = Field(default_factory=list)  # MCP tools IDs
    source_ids: list[str] = Field(default_factory=list)    # knowledge_sources IDs
    table_ids: list[str] = Field(default_factory=list)     # data_tables IDs
    api_keys: list[str] = Field(default_factory=list)      # "conn_id:ep_id"
    # Execution Profile — fast/standard/rigorous. Influencia mode + reflection +
    # evidence no SKILL.md gerado. Default vazio = backend infere (smart default).
    exec_mode: Optional[str] = ""
    # Threshold de confiança mínima para evidência (## Evidence Policy →
    # min_relevance). None = wizard não emite a chave; engine usa default 0.3.
    # Range [0..1] validado pelo Pydantic. Aceitar exatamente 0.0 e 1.0 é
    # deliberado (0.0 = aceita qualquer; 1.0 = só evidência perfeita).
    min_relevance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Onda 1 Output Shape: preset de tamanho da resposta. None = wizard não
    # emite ## Output Shape; engine usa default 'digest' (1500 chars).
    # Validado contra LENGTH_PRESETS (intent/summary/digest/analysis/report/
    # unbounded).
    length_preset: Optional[str] = Field(
        default=None,
        pattern=r"^(intent|summary|digest|analysis|report|unbounded)$",
    )


class WizardRefineRequest(BaseModel):
    current_content: str
    instruction: str
    field: str = "all"
    # Wave Wizard Routing: task_type=instruct por default da rota /refine
    # (refinar texto existente é instruction-following, modelo menor basta).
    task_type: Optional[str] = ""
    # Legacy (retrocompat).
    provider: str = "openai"
    model: Optional[str] = ""


@router.post("/agent")
async def wizard_agent(data: WizardAgentRequest):
    """Wizard IA: gera configuração completa de agente a partir de descrição livre.

    Wave Wizard Routing: usa task_type=reasoning (default) e resolve provider+model
    via roteamento global. Frontend novo não precisa mais mandar provider.
    """
    try:
        provider, model, _ = await _resolve_wizard_llm(data, "agent")
        llm = get_provider(provider, model=(model or None))
        response = await llm.generate([
            {"role": "system", "content": """Você é um arquiteto de agentes de IA. 
Dado uma descrição do usuário, gere a configuração completa de um agente.

Responda APENAS com JSON válido (sem markdown, sem ```), contendo:
{
  "name": "Nome curto e descritivo do agente",
  "description": "Descrição detalhada do que o agente faz",
  "kind": "aobd|router|subagent",
  "domain": "domínio de negócio (ex: financeiro, rh, operacoes)",
  "system_prompt": "System prompt completo e detalhado para o agente, com persona, capacidades, restrições e formato de resposta",
  "suggested_skills": ["lista de skills que o agente precisaria"],
  "suggested_tools": ["lista de ferramentas MCP sugeridas"]
}

Regras:
- kind=aobd para orquestradores de domínio que interpretam intenção
- kind=router para processos de negócio que decompõem em tarefas
- kind=subagent para tarefas atômicas e específicas
- O system_prompt deve ser rico, com instruções claras, formato de saída e guardrails"""},
            {"role": "user", "content": data.description},
        ])
        content = response["content"].strip()
        if content.startswith("```"):
            import re
            m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if m: content = m.group(1).strip()
        result = json.loads(content)
        return {"status": "ok", "agent": result}
    except json.JSONDecodeError:
        return {"status": "ok", "agent": {"name": "", "description": data.description, "kind": "subagent", "domain": data.domain, "system_prompt": content, "suggested_skills": [], "suggested_tools": []}}
    except Exception as e:
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


def _infer_exec_mode(data: WizardSkillRequest) -> str:
    """Smart default pra Execution Profile baseado nos bindings selecionados.

    Heurística:
    - RAG (source_ids) → standard (precisa de reflexão pra usar evidence corretamente)
    - APIs (api_keys) sem RAG → standard (reflexão on-error pra retry em API)
    - Só MCP/Tabelas/nada → fast (workload típico, latência <12s)

    Aplica só quando data.exec_mode vier vazio. Se o user setou explícito,
    respeita.
    """
    explicit = (data.exec_mode or "").strip().lower()
    if explicit in ("fast", "standard", "rigorous"):
        return explicit
    if data.source_ids:
        return "standard"
    if data.api_keys:
        return "standard"
    return "fast"


def _build_exec_profile_yaml(mode: str) -> str:
    """Retorna o YAML da seção ## Execution Profile pra um mode dado."""
    profiles = {
        "fast": "mode: fast\nreflection: off\nevidence: skip",
        "standard": "mode: standard\nreflection: on-error\nevidence: optional",
        "rigorous": "mode: rigorous\nreflection: always\nevidence: required",
    }
    return profiles.get(mode, profiles["fast"])


async def _resolve_bindings_for_prompt(data: WizardSkillRequest) -> dict:
    """Lookup dos IDs estruturados → dicts com nome humano + metadata.

    Frontend manda só IDs (refatoração desta wave). Backend resolve nas tabelas
    e devolve dados ricos pro prompt do LLM. Isso evita que o user tenha que
    escrever nome no description (gambiarra antiga).

    Returns:
        {
          "mcp_tools": [{"name": str, "id": str, "description": str}],
          "rag_sources": [{"name": str, "id": str, "confidentiality_label": str}],
          "data_tables": [{"name": str, "id": str, "urn": str, "schema_summary": str}],
          "api_endpoints": [{"conn_name": str, "ep_name": str, "method": str, "url": str, "key": str}],
        }
        Cada lista pode vir vazia se o user não selecionou ou se o ID não existe.
    """
    result = {
        "mcp_tools": [],
        "rag_sources": [],
        "data_tables": [],
        "api_endpoints": [],
    }

    # Imports lazy — evita acoplar wizard.py a módulos que podem nem estar
    # carregados em testes unitários do próprio wizard.
    from app.core.database import knowledge_repo, _get_pool

    # 1. MCP tools — vive em app.tools.* via repository simples
    if data.mcp_tool_ids:
        try:
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, name, description FROM tools WHERE id = ANY($1::text[])",
                    data.mcp_tool_ids,
                )
                result["mcp_tools"] = [dict(r) for r in rows]
        except Exception as e:
            logger.warning(
                "wizard: lookup MCP tools falhou — segue sem enriquecimento",
                extra={"event": "wizard.lookup_mcp_failed", "error_type": type(e).__name__},
            )

    # 2. Knowledge sources (RAG)
    if data.source_ids:
        try:
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, name, confidentiality_label, kb_mode FROM knowledge_sources "
                    "WHERE id = ANY($1::text[])",
                    data.source_ids,
                )
                result["rag_sources"] = [dict(r) for r in rows]
        except Exception as e:
            logger.warning(
                "wizard: lookup RAG sources falhou — segue sem enriquecimento",
                extra={"event": "wizard.lookup_sources_failed", "error_type": type(e).__name__},
            )

    # 3. Data tables (DuckDB)
    if data.table_ids:
        try:
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, name, urn, schema_json, row_count FROM data_tables "
                    "WHERE id = ANY($1::text[])",
                    data.table_ids,
                )
                for r in rows:
                    # Resumo do schema pra caber no prompt sem inflar tokens.
                    schema = r.get("schema_json") or "{}"
                    try:
                        parsed = json.loads(schema) if isinstance(schema, str) else schema
                        cols = parsed.get("columns") if isinstance(parsed, dict) else None
                        schema_summary = ", ".join(
                            f"{c.get('name')}:{c.get('type')}" for c in (cols or [])[:6]
                        ) or "(sem schema)"
                    except Exception:
                        schema_summary = "(schema não-parseável)"
                    result["data_tables"].append({
                        "id": r["id"],
                        "name": r["name"],
                        "urn": r.get("urn"),
                        "row_count": r.get("row_count"),
                        "schema_summary": schema_summary,
                    })
        except Exception as e:
            logger.warning(
                "wizard: lookup data_tables falhou — segue sem enriquecimento",
                extra={"event": "wizard.lookup_tables_failed", "error_type": type(e).__name__},
            )

    # 4. API endpoints — chave composta "conn_id:ep_id"
    if data.api_keys:
        try:
            pairs = []
            for k in data.api_keys:
                parts = k.split(":", 1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    pairs.append((parts[0], parts[1]))
            if pairs:
                pool = _get_pool()
                async with pool.acquire() as con:
                    # JOIN simples — busca todos endpoints + conn de uma vez.
                    rows = await con.fetch(
                        """
                        SELECT c.id AS conn_id, c.name AS conn_name, c.base_url,
                               e.id AS ep_id, e.name AS ep_name, e.method, e.path
                        FROM api_connectors c
                        JOIN api_endpoints e ON e.connector_id = c.id
                        WHERE e.id = ANY($1::text[])
                        """,
                        [p[1] for p in pairs],
                    )
                    for r in rows:
                        result["api_endpoints"].append({
                            "key": f"{r['conn_id']}:{r['ep_id']}",
                            "conn_id": r["conn_id"],
                            "conn_name": r["conn_name"],
                            "ep_id": r["ep_id"],
                            "ep_name": r["ep_name"],
                            "method": r["method"],
                            "url": f"{(r.get('base_url') or '').rstrip('/')}/{(r.get('path') or '').lstrip('/')}",
                        })
        except Exception as e:
            logger.warning(
                "wizard: lookup API endpoints falhou — segue sem enriquecimento",
                extra={"event": "wizard.lookup_apis_failed", "error_type": type(e).__name__},
            )

    return result


# ───────────────────────────────────────────────────────────────
# Regras de invocação de bindings (gerais — MCP, RAG, API, Tables)
# ───────────────────────────────────────────────────────────────
#
# Plataforma Skill-based: skill pode declarar QUALQUER combinação de
# bindings. O Workflow do SKILL.md precisa ter verbo imperativo nomeando
# cada binding usado, e Examples precisa rastrear a interação (Entrada →
# Ação → Resposta → Saída) — sem isso, LLM em runtime tende a alucinar.
#
# A motivação original foi MCP (bug Context7), mas o mesmo padrão de
# instrução vale pra RAG, API declarativa e Data Tables.


_PASSIVE_VERBS_BLOCKLIST = (
    '"enriquecimento", "incorpora", "usando o binding", "com apoio de", '
    '"a partir de", "se valendo de"'
)
_INTERNAL_PHRASES_BLOCKLIST = (
    '"template interno", "recursos internos", "conhecimento próprio", '
    '"base interna", "conhecimento prévio"'
)


def _common_binding_rules_header() -> str:
    """Bloco comum a QUALQUER binding declarado.

    Repete as proibições e o padrão de rastreabilidade — modelos open-weight
    tendem a "esquecer" instruções específicas a cada bloco, então comum
    primeiro + específicos depois funciona melhor que distribuído.
    """
    return (
        "REGRAS DE INVOCAÇÃO DE BINDINGS (CRÍTICAS — esta skill TEM bindings):\n\n"
        "Esta skill declara bindings (MCP, RAG, API ou Tabelas) que DEVEM ser "
        "documentados como FONTE PRIMÁRIA no Workflow e nos Examples. Regras gerais:\n\n"
        "**G1.** Workflow DEVE ter um passo numerado por binding declarado, com "
        "VERBO IMPERATIVO direto (`Chame`, `Consulte`, `Execute`, `Acione`, "
        "`Query`). Verbos passivos são INSUFICIENTES — modelos open-weight "
        "(gpt-oss, llama, qwen) ignoram silenciosamente sem verbo imperativo.\n"
        f"   VERBOS PASSIVOS PROIBIDOS no Workflow: {_PASSIVE_VERBS_BLOCKLIST}.\n\n"
        "**G2.** NÃO use as frases abaixo no Workflow — dizem ao LLM \"você é "
        "autônomo\" e ele ignora os bindings declarados:\n"
        f"   FRASES PROIBIDAS: {_INTERNAL_PHRASES_BLOCKLIST}.\n\n"
        "**G3.** A seção `## Examples` DEVE rastrear cada interação com binding "
        "ANTES do output final. Padrão obrigatório por exemplo:\n"
        "   ```\n"
        "   ### Exemplo N — <título>\n"
        "   **Entrada:** <input do usuário ou JSON>\n\n"
        "   **<Ação no binding>:** <chamada concreta — ver sub-bloco do binding>\n"
        "   **<Resposta do binding>:** <resumo do que voltou>\n\n"
        "   **Saída final:** <JSON do Output Contract baseado na resposta do binding>\n"
        "   ```\n"
        "   Exemplo que pula direto pra saída ensina o LLM em runtime a alucinar.\n\n"
        "**G4.** Quando há bindings declarados, NUNCA escreva frases como "
        "\"nenhuma fonte externa autorizada\", \"sem fontes externas\", "
        "\"toda informação vem de conhecimento interno\". A fonte autorizada "
        "SÃO os bindings declarados — frases negativas contradizem e fazem "
        "o LLM ignorar o binding."
    )


def _mcp_block(mcp_tools: list[dict]) -> str:
    """Sub-bloco específico de MCP. Cobre o caso original do bug Context7."""
    tool_names = ", ".join(f"`{t['name']}`" for t in mcp_tools)
    first = mcp_tools[0]
    first_name = first["name"]
    import re as _re
    _ops_match = _re.search(r"[a-zA-Z][a-zA-Z0-9_]*", str(first.get("operations") or ""))
    first_op = _ops_match.group(0) if _ops_match else "search"
    return (
        "[MCP] **Tools registradas:** " + tool_names + ". "
        "Use os NOMES EXATOS dessas tools em Workflow e Examples.\n"
        f"  - Verbo recomendado: **Chame** / **Invoque**.\n"
        f"  - Exemplo no Workflow: \"Chame a tool `{first_name}` com "
        f"`operation={first_op}` e `query=<entrada do usuário>` ANTES de gerar a "
        "resposta.\"\n"
        f"  - Exemplo no Examples: `**Chamada à tool:** `{first_name}` "
        f"operation=`{first_op}` query=`<...>``\n"
        "  - Quando NÃO há ## Evidence Policy no bloco obrigatório (skill só "
        "com MCP), a seção Evidence Policy deve dizer: \"_A única fonte "
        f"autorizada é o binding **{first_name}** declarado em ## Tool Bindings._\""
    )


def _rag_block(rag_sources: list[dict]) -> str:
    """Sub-bloco específico de RAG. Engine roda retrieval automático em
    RetrieveEvidence, mas Workflow precisa documentar pra coerência semântica
    (LLM precisa saber que evidências vão chegar)."""
    source_names = ", ".join(f"`{s['name']}`" for s in rag_sources)
    first_name = rag_sources[0]["name"]
    return (
        "[RAG] **Bases registradas:** " + source_names + ". "
        "Engine executa retrieval automaticamente em RetrieveEvidence — Workflow "
        "DEVE documentar a consulta pra coerência semântica.\n"
        f"  - Verbo recomendado: **Consulte** / **Recupere** / **Busque em**.\n"
        f"  - Exemplo no Workflow: \"Consulte as bases `{first_name}` com "
        "`query=<reformulação semântica da pergunta>` ANTES de gerar a resposta. "
        "Use APENAS evidências retornadas (não complete de cabeça).\"\n"
        "  - Exemplo no Examples: `**Consulta RAG:** query=`<...>``  →  "
        "`**Evidências recuperadas:** <resumo dos top-K chunks com score>`\n"
        "  - Resposta DEVE referenciar os chunks recuperados (citação por "
        "ordinal ou ID) — não inventar fatos sem suporte."
    )


def _api_block(endpoints: list[dict]) -> str:
    """Sub-bloco específico de API declarativa. Engine executa o endpoint
    sem LLM no caminho — mas Workflow precisa documentar pra LLM saber
    referenciar a resposta corretamente."""
    ep_names = ", ".join(f"`{ep['ep_name']}`" for ep in endpoints)
    first = endpoints[0]
    first_name = first["ep_name"]
    first_method = first["method"]
    return (
        "[API] **Endpoints declarativos registrados:** " + ep_names + ". "
        "Engine executa em modo DECLARATIVO (sem LLM no tool call) — Workflow "
        "DEVE documentar a execução pra LLM saber referenciar o resultado.\n"
        f"  - Verbo recomendado: **Execute** / **Acione**.\n"
        f"  - Exemplo no Workflow: \"Execute o endpoint `{first_name}` "
        f"({first_method}) com `<payload mapeado dos inputs>` ANTES de "
        "compor a resposta.\"\n"
        f"  - Exemplo no Examples: `**Execução do endpoint:** `{first_name}` "
        f"{first_method} body=`<...>``  →  `**Resposta da API:** "
        "<status_code + payload resumido>`\n"
        "  - Output Contract DEVE refletir campos do payload de resposta — não "
        "inventar campos que a API não retorna.\n"
        "  - Frontmatter do SKILL.md DEVE ter `execution_mode: declarative`."
    )


def _tables_block(data_tables: list[dict]) -> str:
    """Sub-bloco específico de Data Tables. LLM gera SQL, engine executa
    via DuckDB. Sem verbo imperativo, LLM responde de cabeça (tabela nunca
    é consultada)."""
    table_refs = ", ".join(f"`{t['urn']}`" for t in data_tables)
    first = data_tables[0]
    first_urn = first["urn"]
    first_name = first["name"]
    return (
        "[TABLES] **Tabelas registradas:** " + table_refs + ". "
        "LLM gera SQL, engine executa via DuckDB.\n"
        f"  - Verbo recomendado: **Consulte** / **Query** / **Execute SELECT em**.\n"
        f"  - Exemplo no Workflow: \"Consulte a tabela `{first_urn}` "
        f"({first_name}) via SQL com query como `SELECT ... FROM <tabela> WHERE "
        "...` ANTES de gerar a resposta. Use os nomes de colunas EXATOS do "
        "schema declarado.\"\n"
        f"  - Exemplo no Examples: `**SQL gerado:** `SELECT ... FROM "
        f"<tabela>...``  →  `**Resultado da query:** <linhas/contagem>`\n"
        "  - NÃO invente nomes de coluna — só os do schema_summary do bloco "
        "obrigatório."
    )


def _build_binding_invocation_rules(bindings: dict) -> str:
    """Monta o bloco de regras condicionais por tipo de binding declarado.

    Estrutura: header comum (regras G1-G4 que valem pra qualquer binding)
    + sub-blocos específicos por tipo presente. Retorna "" quando nenhum
    binding declarado (skill puramente de raciocínio — back-compat).
    """
    blocks: list[str] = []
    if bindings.get("mcp_tools"):
        blocks.append(_mcp_block(bindings["mcp_tools"]))
    if bindings.get("rag_sources"):
        blocks.append(_rag_block(bindings["rag_sources"]))
    if bindings.get("api_endpoints"):
        blocks.append(_api_block(bindings["api_endpoints"]))
    if bindings.get("data_tables"):
        blocks.append(_tables_block(bindings["data_tables"]))
    if not blocks:
        return ""
    return (
        "\n\n" + _common_binding_rules_header() +
        "\n\nSUB-BLOCOS POR TIPO DE BINDING DECLARADO:\n\n" +
        "\n\n".join(blocks)
    )


def _build_wizard_prompt(data: WizardSkillRequest, bindings: dict, exec_mode: str) -> tuple[str, str]:
    """Monta system + user prompts pro LLM gerar o SKILL.md.

    Tudo que antes ficava concatenado no frontend (mcpContext, apiContext,
    execContext) agora é construído aqui no servidor a partir de IDs
    estruturados — nomes humanos vêm do lookup, não de string passada
    pelo cliente. Mais robusto + testável.

    Returns:
        (system_prompt, user_prompt)
    """
    # Bloco rico das seções OBRIGATÓRIAS que o LLM precisa incluir, com YAML
    # pronto. LLM costuma respeitar instruções imperativas + exemplo concreto.
    #
    # MUDANÇA 2026-05-27 (bug user: "escolhi só RAG e Wizard inventou tools"):
    # Antes, quando o user NÃO selecionava MCP tools, `## Tool Bindings` ficava
    # fora deste bloco — mas o esqueleto do system_prompt principal listava a
    # seção como canônica. LLM completava criativamente com nomes inventados
    # (`knowledge_search`, `summarize_text`), causando C3_mcp_unmatched no
    # preflight e poluindo a skill. Agora a seção é SEMPRE incluída
    # explicitamente: com a lista real OU com declaração de vazio.
    obligatory_sections = []

    if bindings["mcp_tools"]:
        # Trunc 300 chars (não 100): 100 era o bug do "MCP Se[rver]" cortado no
        # meio do nome da tool. 300 alinha com build_openai_tools/runtime.py:575
        # e _build_system_prompt/engine.py:402 — única descrição completa que o
        # LLM vê em runtime.
        bindings_md = "\n".join(
            f"- `{t['id']}` ({t['name']}) — {(t.get('description') or '').strip()[:300]}"
            for t in bindings["mcp_tools"]
        )
        obligatory_sections.append(
            "## Tool Bindings\n" + bindings_md
        )
    else:
        # Lista explicitamente os recursos efetivamente disponíveis pra o LLM
        # não se sentir tentado a inventar tools. Se nem RAG, nem tables, nem
        # APIs, a frase deixa clara essa condição (skill pura de raciocínio).
        available = []
        if bindings.get("rag_sources"):
            available.append("RAG (Evidence Policy)")
        if bindings.get("data_tables"):
            available.append("Data Tables (## Data Tables)")
        if bindings.get("api_endpoints"):
            available.append("APIs declarativas (## API Bindings)")
        recursos = ", ".join(available) if available else "apenas raciocínio LLM (sem bindings externos)"
        obligatory_sections.append(
            "## Tool Bindings\n"
            "(Nenhuma ferramenta MCP foi selecionada para esta skill. Esta seção "
            "DEVE permanecer com a declaração abaixo — NÃO invente nomes de tools.)\n\n"
            f"_Esta skill não usa ferramentas MCP. Recursos disponíveis: {recursos}._"
        )

    if bindings["rag_sources"]:
        sources_yaml = "\n".join(
            f"  - {s['id']}   # {s['name']} ({s.get('confidentiality_label', 'internal')})"
            for s in bindings["rag_sources"]
        )
        # min_relevance opcional — só emite quando user setou explicitamente.
        # Quando ausente, o engine usa default 0.3 (engine.py:_DEFAULT_MIN_RELEVANCE).
        # Faixa válida [0..1] garantida pelo Pydantic (Field ge/le).
        threshold_yaml = ""
        if data.min_relevance is not None:
            threshold_yaml = f"\nmin_relevance: {data.min_relevance}"
        obligatory_sections.append(
            "## Evidence Policy\n```yaml\nsources:\n" + sources_yaml + threshold_yaml + "\n```"
        )

    if bindings["data_tables"]:
        # Tabelas viram exemplos no SKILL.md — LLM deve referenciar via URN.
        tables_md = "\n".join(
            f"- `{t['urn']}` ({t['name']}, ~{t.get('row_count', '?')} linhas): {t.get('schema_summary', '')}"
            for t in bindings["data_tables"]
        )
        obligatory_sections.append(
            "## Data Tables\n```yaml\ntables:\n" + "\n".join(
                f"  - urn: {t['urn']}\n    name: {t['name']}"
                for t in bindings["data_tables"]
            ) + "\n```\n\nReferências disponíveis:\n" + tables_md
        )

    if bindings["api_endpoints"]:
        api_yaml = "\n".join(
            f"  - id: {ep['ep_id']}\n    connector_id: {ep['conn_id']}\n"
            f"    name: {ep['ep_name']}\n    method: {ep['method']}\n    # URL: {ep['url']}"
            for ep in bindings["api_endpoints"]
        )
        # APIs também exigem frontmatter execution_mode: declarative
        obligatory_sections.append(
            "INCLUA no frontmatter YAML: `execution_mode: declarative`\n\n"
            "## API Bindings\n```yaml\nendpoints:\n" + api_yaml + "\n```"
        )

    # Execution Profile sempre presente
    obligatory_sections.append(
        "## Execution Profile\n" + _build_exec_profile_yaml(exec_mode)
    )

    # Onda 1 Output Shape: emite ## Output Shape quando user escolheu preset.
    # Sem preset, NÃO inclui a seção — engine cai em default ('digest') sem
    # poluir a skill com YAML opcional.
    if data.length_preset:
        obligatory_sections.append(
            "## Output Shape\n```yaml\n"
            f"length_preset: {data.length_preset}\n"
            "```"
        )

    # Regras anti-hallucination: explícitas pra evitar que o LLM invente
    # bindings que o user não escolheu. Sem isso, o LLM tende a "completar"
    # com tools genéricas como knowledge_search/summarize_text mesmo quando
    # o bloco obrigatório está vazio de MCP.
    #
    # Regra 6 (2026-05-27): valores numéricos de configuração (min_relevance,
    # max_age_days, etc) NÃO podem ser inventados em prosa. Antes desta regra,
    # o LLM citava "min_relevance (0.05)" em Workflow e Failure Modes mesmo
    # quando o user não tinha informado nenhum valor — gerava ilusão de que
    # a skill aplicava 0.05 em runtime, mas como o YAML estava sem a chave,
    # o engine usava default 0.30. Operador editava agente sem entender por
    # quê o threshold mostrado no painel não batia com a "documentação" da
    # skill.
    #
    # Bloco threshold_rule fica VAZIO quando o user não informou — a regra 6
    # base já cobre. Quando informou, reforça pra LLM usar o número exato em
    # eventuais menções pra preservar coerência interna do markdown.
    threshold_rule = ""
    if data.min_relevance is not None:
        threshold_rule = (
            f"\n7. O valor de min_relevance fornecido pelo operador é "
            f"`{data.min_relevance}`. Se citar esse threshold em prosa "
            "(Workflow/Failure Modes/etc), use EXATAMENTE esse número — "
            "nunca invente um valor diferente."
        )
    anti_halluc_rules = (
        "REGRAS ANTI-INVENÇÃO (CRÍTICAS):\n"
        "1. Use APENAS os bindings declarados no bloco SEÇÕES OBRIGATÓRIAS abaixo.\n"
        "2. NÃO invente nomes de tools MCP (ex: knowledge_search, summarize_text). "
        "Se não há MCP no bloco obrigatório, declare explicitamente \"sem tools MCP\".\n"
        "3. NÃO invente IDs de knowledge_sources em ## Evidence Policy. "
        "Use APENAS os IDs listados no bloco obrigatório.\n"
        "4. NÃO invente endpoints de API. Use APENAS os do bloco obrigatório.\n"
        "5. Workflow PODE descrever passos lógicos sem nomear tools concretas — "
        "use frases como \"consulta a base RAG\" em vez de citar tools inexistentes.\n"
        "6. NÃO invente valores numéricos de configuração (min_relevance, "
        "max_age_days, etc) em prosa. O valor concreto de threshold de evidência "
        "vive APENAS na chave `min_relevance` do YAML em ## Evidence Policy — "
        "e SOMENTE quando estiver no bloco SEÇÕES OBRIGATÓRIAS. Em Workflow e "
        "Failure Modes, cite o conceito sem número: use frases como "
        "\"score abaixo do `min_relevance` configurado\" ou "
        "\"threshold definido em Evidence Policy\". Se o bloco obrigatório NÃO "
        "trouxer min_relevance, NÃO cite número nenhum (o engine aplicará o "
        "default da plataforma)."
        + threshold_rule
    )

    # ─────────────────────────────────────────────────────────────────
    # Regras de invocação de bindings — gerais.
    #
    # Plataforma é Skill-based: uma skill pode declarar QUALQUER combinação
    # de bindings (MCP, RAG, API declarativa, Data Tables). O Workflow do
    # SKILL.md afeta diretamente como o LLM em runtime invoca/usa cada um.
    #
    # Motivação cruzada (não é só MCP):
    # - MCP: bug Context7 (2026-05-29) — Workflow passivo "enriquecimento com X
    #   usando o binding" → gpt-oss-120b ignorou a tool, alucinou resposta.
    # - RAG: engine faz retrieval automático em RetrieveEvidence, mas se o
    #   Workflow não documenta a consulta, LLM tende a ignorar as evidências
    #   recuperadas e responder de cabeça.
    # - API declarativa: engine executa endpoints sem LLM no caminho, mas o
    #   LLM recebe os resultados como contexto — Workflow precisa documentar
    #   a chamada pra LLM saber referenciar.
    # - Data Tables: LLM gera SQL que o engine executa via DuckDB. Sem
    #   Workflow com verbo imperativo, LLM responde de cabeça (a base nem é
    #   consultada).
    #
    # Padrão: cada tipo de binding ativa um sub-bloco específico (verbo
    # imperativo próprio + formato de exemplo). Regras COMUNS valem pra
    # qualquer binding (frases passivas proibidas, padrão de rastreabilidade
    # nos Examples, proibição de "nenhuma fonte externa").
    # ─────────────────────────────────────────────────────────────────
    binding_invocation_rules = _build_binding_invocation_rules(bindings)

    obligatory_block = (
        "\n\n=== SEÇÕES OBRIGATÓRIAS A INCLUIR NO SKILL.md ===\n"
        "Você DEVE incluir EXATAMENTE estes blocos no SKILL.md gerado. "
        "Preserve YAMLs fenced, IDs e comentários. "
        "NÃO adicione tools/sources/endpoints que NÃO estejam neste bloco:\n\n"
        + "\n\n---\n\n".join(obligatory_sections)
        + "\n=== FIM DAS SEÇÕES OBRIGATÓRIAS ==="
    ) if obligatory_sections else ""

    system_prompt = f"""Você é um arquiteto de skills para plataforma multi-agente.
Gere um SKILL.md completo seguindo a anatomia canônica.

{anti_halluc_rules}{binding_invocation_rules}

O SKILL.md deve conter EXATAMENTE esta estrutura:

---
id: urn:skill:{data.domain or 'geral'}:{data.kind}:SLUG_AQUI
version: 0.1.0
kind: {data.kind}
owner: equipe-ia
stability: alpha
---

# Nome do Skill

## Purpose
Declaração imperativa do que este agente faz e do que NÃO faz.

## Activation Criteria
Condições sob as quais este skill deve ser selecionado.

## Inputs
Schema tipado do envelope esperado em formato JSON Schema.

## Workflow
Sequência de passos do workflow. Para subagentes, linear. Para roteadores, DAG.

## Tool Bindings
Lista de tools MCP **estritamente** das fornecidas no bloco obrigatório.
Se o bloco obrigatório declara "sem tools MCP", reproduza essa declaração
sem listar tools inventadas.

## Output Contract
Schema tipado da saída esperada.

## Failure Modes
Enumeração de falhas e ação prescrita.

## Evidence Policy
Bases autorizadas e thresholds de evidência (quando aplicável).
Use APENAS os IDs de knowledge_sources do bloco obrigatório.

## Guardrails
Políticas de conteúdo, PII, jurisdição.

## Examples
Pares entrada/saída para avaliação. Os bindings (tools MCP, sources RAG,
endpoints API, tabelas) referenciados nos exemplos DEVEM bater com o bloco
obrigatório — sem invenções.
Quando esta skill tem QUALQUER binding declarado (MCP, RAG, API, Tabelas),
cada exemplo DEVE rastrear a interação com o binding (Entrada → Ação no
binding → Resposta do binding → Saída final) antes do output final —
exemplo que pula direto pra saída ensina o LLM em runtime a alucinar.

IMPORTANTE: NÃO inclua a seção `## Budget` (limites de tokens, tempo ou custo).
Restrições de budget devem ser definidas pelo operador depois, conscientemente —
gerar valores automáticos prejudica o desempenho do agente em runtime sem ganho
real (LLM não sabe o ROI/budget aceitável do caso de uso). Seção é opcional no
parser, então omitir é seguro.

Gere o SKILL.md completo em formato markdown. Seja específico e detalhado.{obligatory_block}"""

    return system_prompt, data.description


# Limite de tentativas de regeneração quando o validador detecta crítico.
# 1 retry é suficiente pra recuperar a maioria dos casos sem inflar latência;
# 2+ raramente ajuda (se LLM gerador errou 2x com instrução corretiva, é
# provável que o modelo subdimensionado pra a task).
_WIZARD_MAX_RETRIES = 1


def _build_retry_instruction(validation_result) -> str:
    """Constrói prompt extra pro LLM regenerar SKILL.md corrigindo as
    violações críticas detectadas pelo validador."""
    suggestions = validation_result.critical_suggestions()
    if not suggestions:
        return ""
    return (
        "\n\n[CORREÇÕES OBRIGATÓRIAS — sua SKILL.md anterior violou estas regras]\n"
        "Reescreva a SKILL.md aplicando as correções abaixo. NÃO ignore — "
        "estas falhas fazem a skill quebrar em runtime:\n\n"
        + "\n".join(suggestions)
        + "\n\nRegere a SKILL.md COMPLETA com as correções aplicadas."
    )


@router.post("/skill")
async def wizard_skill(data: WizardSkillRequest):
    """Wizard IA: gera SKILL.md canônico a partir de descrição + bindings estruturados.

    Wave Wizard UX (PR atual):
    - Aceita IDs estruturados (mcp_tool_ids, source_ids, table_ids, api_keys).
    - Backend resolve nomes humanos via lookup e monta prompt enriquecido.
    - Smart default: exec_mode inferido se vazio (RAG/API → standard, senão fast).
    - Retrocompat: clients antigos com só `description, kind, domain` continuam
      funcionando (apenas perdem o enriquecimento estruturado).

    Wave Validator (2026-05-29): após LLM gerar SKILL.md, parseia e valida
    contra regras G1-G4 + operations declaradas. Crítico → retry 1x com
    instrução corretiva. Warning → retorna no response pro frontend mostrar.
    """
    try:
        bindings = await _resolve_bindings_for_prompt(data)
        exec_mode = _infer_exec_mode(data)
        system_prompt, user_prompt = _build_wizard_prompt(data, bindings, exec_mode)

        # Wave Wizard Routing: usa task_type=reasoning (default) e roteamento global.
        provider, model, resolved_task = await _resolve_wizard_llm(data, "skill")
        llm = get_provider(provider, model=(model or None))

        # ── Geração inicial ──
        response = await llm.generate([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        skill_md = response["content"]

        # ── Validação pós-geração + retry com instrução corretiva ──
        from app.skill_parser.parser import parse_skill_md
        from app.skill_parser.wizard_validator import validate_generated_skill

        retries_used = 0
        validation_result = None
        try:
            parsed = parse_skill_md(skill_md)
            validation_result = validate_generated_skill(parsed, bindings)
        except Exception as _parse_err:
            # Parser não conseguiu ler a SKILL — não dá pra validar.
            # Loga e segue sem validação (não vamos bloquear por erro de parse
            # do nosso lado — operador ainda recebe a SKILL pra ajustar).
            logger.warning(
                "wizard_skill: parser falhou pós-geração — segue sem validação",
                extra={"event": "wizard.validation.parse_failed",
                       "error_type": type(_parse_err).__name__},
            )

        if validation_result is not None and not validation_result.ok and _WIZARD_MAX_RETRIES > 0:
            retry_instruction = _build_retry_instruction(validation_result)
            logger.info(
                "wizard_skill: validador detectou crítico, tentando regerar",
                extra={
                    "event": "wizard.validation.retry",
                    "critical_count": validation_result.critical_count,
                    "warning_count": validation_result.warning_count,
                    "rules_hit": sorted({v.rule for v in validation_result.violations}),
                },
            )
            try:
                retry_response = await llm.generate([
                    {"role": "system", "content": system_prompt + retry_instruction},
                    {"role": "user", "content": user_prompt},
                ])
                retry_skill_md = retry_response["content"]
                # Re-valida o retry — se também violar, mantém o RETRY (geralmente
                # melhor que o original) mas devolve warnings pro operador
                try:
                    parsed_retry = parse_skill_md(retry_skill_md)
                    retry_validation = validate_generated_skill(parsed_retry, bindings)
                    skill_md = retry_skill_md
                    validation_result = retry_validation
                    retries_used = 1
                except Exception:
                    # Retry quebrou o parser — mantém SKILL original (que ao
                    # menos parsea) e devolve validation_result original
                    logger.warning(
                        "wizard_skill: retry produziu SKILL com parse error — usando original",
                        extra={"event": "wizard.validation.retry_unparseable"},
                    )
            except Exception as _retry_err:
                logger.warning(
                    f"wizard_skill: retry falhou ({type(_retry_err).__name__}) — usando SKILL original",
                    extra={"event": "wizard.validation.retry_failed"},
                )

        # Resumo do que foi resolvido — UI pode mostrar pra confirmar.
        result = {
            "status": "ok",
            "skill_md": skill_md,
            "resolved": {
                "exec_mode": exec_mode,
                "mcp_count": len(bindings["mcp_tools"]),
                "rag_count": len(bindings["rag_sources"]),
                "table_count": len(bindings["data_tables"]),
                "api_count": len(bindings["api_endpoints"]),
                # Wave Wizard Routing: mostra qual LLM foi escolhido.
                "llm_provider": provider,
                "llm_model": model,
                "llm_task_type": resolved_task,
            },
        }
        # Validação: só inclui quando rodou. UI pode mostrar warnings/crítico
        # remanescentes pro operador revisar antes de salvar.
        if validation_result is not None:
            result["validation"] = validation_result.to_dict()
            result["validation"]["retries_used"] = retries_used
        return result
    except Exception as e:
        logger.exception("wizard_skill falhou")
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


@router.post("/refine")
async def wizard_refine(data: WizardRefineRequest):
    """Wizard IA: refina/melhora um campo ou conteúdo existente.

    Wave Wizard Routing: usa task_type=instruct (default) — refinamento é
    instruction-following, modelo menor (gpt-oss-20b por padrão) basta.
    """
    try:
        provider, model, _ = await _resolve_wizard_llm(data, "refine")
        llm = get_provider(provider, model=(model or None))
        response = await llm.generate([
            {"role": "system", "content": "Você é um especialista em refinamento de configurações de IA. Melhore o conteúdo conforme a instrução do usuário. Responda APENAS com o conteúdo melhorado, sem explicações adicionais."},
            {"role": "user", "content": f"Campo: {data.field}\n\nConteúdo atual:\n{data.current_content}\n\nInstrução de melhoria:\n{data.instruction}"},
        ])
        return {"status": "ok", "refined": response["content"]}
    except Exception as e:
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


@router.get("/models")
async def list_available_models():
    """Lista modelos disponíveis por provedor.

    Onda 7: cada modelo ganha flag `multimodal: bool`. Usado pelo routing
    pra decidir se input com imagem precisa cair no multimodal_fallback
    (modelos text-only não recebem images, falhariam silenciosamente).
    """
    # Azure OpenAI usa os MESMOS modelos do OpenAI público (Azure é apenas
    # uma forma diferente de hospedar/cobrar). O `id` é o nome do DEPLOYMENT
    # no Azure, que normalmente coincide com o nome do modelo, mas pode ser
    # customizado por quem provisionou o recurso.
    openai_models = [
        {"id": "gpt-4o", "name": "GPT-4o", "context": "128K", "tier": "flagship", "multimodal": True},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "context": "128K", "tier": "efficient", "multimodal": True},
        {"id": "gpt-4-turbo", "name": "GPT-4 Turbo", "context": "128K", "tier": "legacy", "multimodal": True},
        {"id": "gpt-4.1", "name": "GPT-4.1", "context": "1M", "tier": "flagship", "multimodal": True},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "context": "1M", "tier": "efficient", "multimodal": True},
        {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano", "context": "1M", "tier": "nano", "multimodal": False},
        {"id": "o4-mini", "name": "o4 Mini (reasoning)", "context": "200K", "tier": "reasoning", "multimodal": False},
        {"id": "o3", "name": "o3 (reasoning)", "context": "200K", "tier": "reasoning", "multimodal": False},
        {"id": "o3-mini", "name": "o3 Mini (reasoning)", "context": "200K", "tier": "reasoning", "multimodal": False},
        {"id": "o1", "name": "o1 (reasoning)", "context": "200K", "tier": "reasoning", "multimodal": True},
        {"id": "o1-mini", "name": "o1 Mini (reasoning)", "context": "128K", "tier": "reasoning", "multimodal": False},
    ]
    return {
        "azure": openai_models,
        "openai": openai_models,
        "maritaca": [
            {"id": "sabia-4", "name": "Sabiá-4", "context": "128K", "tier": "flagship", "multimodal": False},
            {"id": "sabia-3", "name": "Sabiá-3", "context": "32K", "tier": "flagship", "multimodal": False},
            {"id": "sabia-3-2025-01-15", "name": "Sabiá-3 (Jan/25)", "context": "32K", "tier": "flagship", "multimodal": False},
            {"id": "sabia-2-medium", "name": "Sabiá-2 Medium", "context": "16K", "tier": "efficient", "multimodal": False},
            {"id": "sabia-2-small", "name": "Sabiá-2 Small", "context": "8K", "tier": "small", "multimodal": False},
        ],
        "ollama": [
            {"id": "hf.co/Althayr/Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest", "name": "Gaia 4b", "context": "128K", "tier": "flagship", "multimodal": False},
            {"id": "gemma4:e4b", "name": "Gemma 4 4B", "context": "128K", "tier": "flagship", "multimodal": False},
            {"id": "gemma3:4b", "name": "Gemma 3 4B", "context": "128K", "tier": "efficient", "multimodal": False},
            {"id": "gemma3:1b", "name": "Gemma 3 1B", "context": "32K", "tier": "small", "multimodal": False},
            {"id": "gemma3:12b", "name": "Gemma 3 12B", "context": "128K", "tier": "flagship", "multimodal": False},
        ],
        # GPT-OSS — open-weight via hub interno. IDs alinhados ao formato aceito
        # pelo hub (OpenAI-compatible /v1/chat/completions). Multimodal=False
        # (open-weight atual não tem suporte oficial a image input). Reasoning=False
        # — usar reasoning específico cai nos modelos azure/o*.
        "gpt-oss-120b": [
            {"id": "openai/gpt-oss-120b", "name": "GPT-OSS-120B (open-weight)", "context": "128K", "tier": "open-weight", "multimodal": False},
        ],
        "gpt-oss-20b": [
            {"id": "openai/gpt-oss-20b", "name": "GPT-OSS-20B (open-weight)", "context": "128K", "tier": "open-weight", "multimodal": False},
        ],
    }
