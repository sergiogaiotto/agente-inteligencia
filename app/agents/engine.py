"""Motor de agentes três camadas — §4 (AOBD → AR → SA).

LangGraph StateGraph para cada camada. DeepAgent Harness com
auto-reflexão. Integração com FSM §15, protocolo A2A §7,
e runtime de evidência §14.

MELHORIA 2026-04-21: Trace enriquecido com detalhes de system prompt,
seções do SKILL.md, tool bindings, e log de execução estruturado.

MELHORIA 2026-04-21: Short-circuit para agentes pass-through em pipeline.
Agentes sem SKILL.md e com prompt genérico são ignorados na execução,
propagando o input diretamente ao próximo agente (0ms vs ~20s por nó).
"""

import uuid
import json
import time
import logging
from typing import TypedDict, Annotated, Sequence, Optional
from dataclasses import asdict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, END

from app.core.llm_providers import (
    canonical_provider,
    get_provider,
    is_llm_unreachable,
    is_llm_param_rejection,
)
from app.core.llm_breaker import breaker
from app.core.observability import get_langfuse_handler, tracker
from app.agents.conversation_memory import (
    build_history_messages as _build_history_messages,
    session_text_window as _session_text_window,
    context_enabled,
    normalize_context_mode as _cm_normalize,
)
from app.core.database import (
    agents_repo, skills_repo, interactions_repo, turns_repo,
    audit_repo, car_repo,
)
from app.a2a.protocol import (
    IntentDescriptor,
)
from app.agents.state_machine import (
    InteractionStateMachine, InteractionContext, State,
)
from app.evidence.runtime import retriever, reranker, evidence_checker
from app.skill_parser.decisions_schema import (
    build_decisions_directive,
    extract_decision_line,
    extract_decisions_schema,
    has_decision_line,
    preserve_decision_line,
    strip_decision_line,
)
from app.skill_parser.parser import parse_skill_md
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


# ═══════════════════════════════════════════════════
# Cache de topologia POR REQUISIÇÃO (25.2.0)
# ═══════════════════════════════════════════════════
# execute_pipeline re-consulta mesh_connections e agents dezenas de vezes por
# invoke (chain-resolver + cada gate de skip + display). A topologia NÃO muda
# durante um invoke. Um cache contextvar-scoped memoiza os lookups do caminho
# quente. Ativado só quando execute_pipeline o liga (toggle
# query_topology_cache_enabled); fora de pipeline o contextvar é None e os
# helpers passam direto (zero mudança de comportamento; retrocompat total com
# os testes que chamam os gates isolados).
import contextvars as _contextvars

_pipeline_topo = _contextvars.ContextVar("pipeline_topo", default=None)


async def _topo_mesh_out(source_id: str, limit: int = 100) -> list[dict]:
    """Arestas de saída de um agente, memoizadas por requisição de pipeline."""
    from app.core.database import mesh_repo
    cache = _pipeline_topo.get()
    if cache is None:
        return await mesh_repo.find_all(source_agent_id=source_id, limit=limit)
    m = cache["mesh"]
    if source_id not in m:
        # Busca com limite generoso (agente real tem poucas arestas) pra o
        # cache ser completo p/ todos os callers (uns usam limit 20, outros 50).
        m[source_id] = await mesh_repo.find_all(source_agent_id=source_id, limit=max(limit, 100))
    return m[source_id]


async def _topo_agent(agent_id: str) -> Optional[dict]:
    """Agente por id, memoizado por requisição de pipeline.

    Retorna uma CÓPIA RASA — `find_by_id` devolvia um dict fresco a cada
    chamada e o hot path MUTA o agente (ex.: `_run_llm_chain` troca
    `llm_provider`/`model` no fallback). Sem a cópia, a mutação vazaria para
    o objeto cacheado e corromperia lookups seguintes do mesmo agente.
    """
    from app.core.database import agents_repo
    cache = _pipeline_topo.get()
    if cache is None:
        return await agents_repo.find_by_id(agent_id)
    a = cache["agents"]
    if agent_id not in a:
        a[agent_id] = await agents_repo.find_by_id(agent_id)
    cached = a[agent_id]
    return dict(cached) if cached is not None else None


# ═══════════════════════════════════════════════════
# Roteamento rápido (26.0.0) — pula o LLM do router quando determinístico
# ═══════════════════════════════════════════════════
# Variáveis da classe-OUTPUT: uma expr condicional que as usa depende do
# output do agente anterior (o router). Se NENHUMA aresta de saída do entry
# depende do output, o draft do router é peso morto e pode ser pulado — a
# rota é 100% decidida por args selados + pergunta. Espelha CONDITIONAL_VARS_META.
_OUTPUT_CLASS_VARS = frozenset({
    "output", "output_lower", "output_norm", "output_length", "has_output", "final_state",
    "is_recommend", "is_refuse", "is_escalate",
    "contains_image", "contains_url", "contains_pdf", "lines_count",
    # Contrato de Decisão (35.19.0): `decision.<campo>` é extraído da linha
    # DECISAO: no OUTPUT do agente anterior — pular o LLM do router mataria a
    # linha e toda regra decision.* viraria falso-negativo silencioso.
    "decision",
})


def _expr_uses_output(expr: str) -> bool:
    """True se a expr condicional referencia QUALQUER variável derivada do
    output do agente anterior. Fail-safe: erro de parse ⇒ True (assume que
    depende, roda o LLM)."""
    if not expr or not expr.strip():
        return False
    try:
        from jinja2 import Environment, meta
        ast = Environment().parse("{{ " + expr + " }}")
        used = meta.find_undeclared_variables(ast)
    except Exception:
        return True
    return bool(used & _OUTPUT_CLASS_VARS)


async def _skill_emits_structured_target(skill_id: str | None) -> bool:
    """True se o skill do router declara roteamento ESTRUTURADO (emite o bloco
    ``{"target": ..., "inputs": {...}}`` — Fase B #316). Esse bloco é AUTORITATIVO
    sobre a expr em `_should_skip_conditional` (`_extract_routed_target`), mas só
    existe se o router RODAR. Fast-routing pula o LLM ⇒ o bloco some ⇒ a rota
    cairia na expr e poderia divergir. Detectar isso mantém a equivalência mesmo
    quando as arestas têm expr input-only. Fail-safe: sem skill / erro / contrato
    ausente ⇒ False (não é router estruturado conhecido)."""
    if not skill_id:
        return False
    try:
        row = await skills_repo.find_by_id(skill_id)
        raw = (row or {}).get("raw_content") or ""
        if not raw:
            return False
        contract = (parse_skill_md(raw).output_contract or "").lower()
    except Exception:
        return False
    # O contrato do roteador Fase B descreve o objeto {"target","inputs"}. Ambos
    # os termos ⇒ tratamos como roteador estruturado (recusa conservadora: no pior
    # caso perde só o speedup, nunca a correção da rota).
    return "target" in contract and "inputs" in contract


async def _entry_fast_routable(entry_id: str, entry_agent: dict | None = None) -> bool:
    """True se o ENTRY (router) pode ser pulado com segurança: TODAS as arestas
    de saída roteiam SÓ por args selados + pergunta + anexos (nunca pelo output
    do router). Qualquer aresta ambígua ⇒ False (fail-safe: roda o LLM).

    Também recusa se o router emite TARGET ESTRUTURADO (skill declara o bloco
    ``{"target","inputs"}``): esse override é autoritativo sobre a expr e só
    existe se o router rodar — pulá-lo mudaria a rota."""
    conns = await _topo_mesh_out(entry_id, limit=100)
    if not conns:
        return False  # sem arestas: o entry É o trabalho (single-agent)
    for c in conns:
        ct = c.get("connection_type")
        if ct not in ("conditional", "default"):
            return False  # sequential/parallel: não roteia por keyword
        if ct == "conditional":
            cfg = c.get("config") or "{}"
            try:
                cfg_dict = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
            except Exception:
                return False
            expr = ((cfg_dict or {}).get("expr") or "").strip()
            if not expr or _expr_uses_output(expr):
                return False  # sem expr OU depende do output ⇒ não pula
    # Guard do roteador estruturado (equivalência de rota): se o entry declara
    # emitir {"target","inputs"}, a decisão real vem do LLM, não da expr — não pula.
    ag = entry_agent if entry_agent is not None else (await _topo_agent(entry_id) or {})
    if await _skill_emits_structured_target(ag.get("skill_id")):
        return False
    return True


# ═══════════════════════════════════════════════════
# Helpers — Idioma de resposta (resolução em cascata)
# ═══════════════════════════════════════════════════

# Mapeia tag BCP-47 → label humano usado na instrução do system_prompt.
# Mantém formato natural ("português brasileiro" em vez de "pt-BR") pra LLM
# entender melhor — modelos open-weight respondem mais consistente com nome
# da língua do que tag técnica.
_LANGUAGE_LABELS = {
    "pt-BR": "português brasileiro (pt-BR)",
    "pt-PT": "português europeu (pt-PT)",
    "en-US": "inglês americano (en-US)",
    "en-GB": "inglês britânico (en-GB)",
    "es-ES": "espanhol (es-ES)",
    "es-MX": "espanhol mexicano (es-MX)",
    "fr-FR": "francês (fr-FR)",
    "de-DE": "alemão (de-DE)",
    "it-IT": "italiano (it-IT)",
    "ja-JP": "japonês (ja-JP)",
    "zh-CN": "chinês simplificado (zh-CN)",
}


def _resolve_response_language(agent: dict, settings) -> str:
    """Resolve idioma de resposta em cascata: agente > settings > hard fallback.

    Returns:
        BCP-47 tag (ex: "pt-BR") que o LLM deve usar pra responder. NUNCA
        vazia — sempre tem fallback final pra "pt-BR".

    A regra hard-coded "pt-BR" no fim é o último escudo caso platform
    settings esteja corrompido. Operador pode customizar via env
    DEFAULT_RESPONSE_LANGUAGE ou Settings UI.
    """
    agent_lang = (agent.get("response_language") or "").strip() if agent else ""
    if agent_lang:
        return agent_lang
    settings_lang = (getattr(settings, "default_response_language", "") or "").strip()
    if settings_lang:
        return settings_lang
    return "pt-BR"


def _build_response_language_directive(lang_tag: str, *, preserve_decision_line: bool = False) -> str:
    """Constrói bloco do system_prompt instruindo o LLM sobre idioma da resposta.

    Texto imperativo — modelos open-weight (gpt-oss-120b) tendem a espelhar
    idioma do contexto/evidência se não houver diretiva explícita. Esta
    diretiva força a resposta no idioma escolhido mesmo quando RAG ou tools
    MCP retornam conteúdo em outra língua, INCLUSIVE quando o LLM está
    preenchendo campos textuais de um JSON estruturado (Output Contract).

    Enumera os campos típicos onde modelos costumam copiar do tool result
    em vez de traduzir (title, content, snippet, summary, description) —
    open-weight precisa do gatilho lexical pra atravessar o impulso de
    passthrough.

    `preserve_decision_line` (Cond-C, 35.19.0): quando o agente declara
    `## Decisions`, a diretiva ganha a exceção da linha `DECISAO:` — sem ela,
    um agente en-US obedeceria "traduza TUDO" e emitiria `DECISION: yes`, que
    o parser rejeita e vira falso-negativo silencioso no gate (review
    2026-07-15). Sem contrato o texto é BYTE-IDÊNTICO ao de sempre.
    """
    label = _LANGUAGE_LABELS.get(lang_tag, lang_tag)
    base = (
        "[IDIOMA DA RESPOSTA]\n"
        f"Sempre responda em {label}, mesmo quando o contexto, evidências de "
        "RAG ou resultados de tools MCP estiverem em outros idiomas. Traduza "
        "ou adapte TODO conteúdo textual recuperado para o idioma alvo — "
        "INCLUSIVE quando estiver preenchendo campos de um JSON estruturado "
        "(ex: `title`, `content`, `snippet`, `summary`, `description`, `text`, "
        f"`body`). Títulos de artigos, manchetes e descrições devem aparecer "
        f"em {label}, nunca copiados crus do tool result. "
        "Preserve no idioma original APENAS: URLs, identificadores técnicos "
        "(IDs, slugs, chaves), código-fonte, e nomes de marcas/produtos/"
        "pessoas (ex: 'Uber', 'GPT-4', 'João Silva')."
    )
    if preserve_decision_line:
        base += (
            " EXCEÇÃO ADICIONAL: a linha final `DECISAO:` exigida pelo "
            "Contrato de Decisão é um token de máquina — copie a palavra "
            "DECISAO e os valores EXATAMENTE como declarados no contrato, "
            "SEM traduzi-los."
        )
    return base


def _build_response_language_closing(lang_tag: str, *, preserve_decision_line: bool = False) -> str:
    """Reminder curto colado ao FIM do system prompt (estratégia sanduíche).

    Modelos open-weight grudam no que está mais próximo da geração — quando
    o prompt cresce (Output Contract + MCP Tools catalog + Guardrails), a
    diretiva inicial perde força. Este reminder de 1 linha no fim refresca
    a instrução logo antes do LLM começar a gerar tokens.

    Mantém-se curto de propósito: o detalhe está na diretiva inicial; aqui
    só ancora a regra.

    `preserve_decision_line`: este reminder é a ÚLTIMA instrução antes da
    geração — justamente a que vence a atenção do modelo. Sem a exceção, o
    "traduza TUDO" mataria a linha `DECISAO:` do Contrato de Decisão (ver
    `_build_response_language_directive`). Sem contrato → byte-idêntico.
    """
    label = _LANGUAGE_LABELS.get(lang_tag, lang_tag)
    base = (
        "[LEMBRETE FINAL — IDIOMA]\n"
        f"Antes de gerar sua resposta: TODO texto que você produzir, "
        f"inclusive em campos JSON, deve estar em {label}. Traduza títulos "
        "e conteúdo do tool result; preserve apenas URLs, código e nomes "
        "próprios."
    )
    if preserve_decision_line:
        base += (
            " EXCEÇÃO: a linha final `DECISAO:` do Contrato de Decisão NÃO "
            "se traduz — palavra DECISAO e valores VERBATIM, como declarados."
        )
    return base


# ═══════════════════════════════════════════════════
# Helpers — Grounded-by-default (2026-06-06)
# ═══════════════════════════════════════════════════
# Princípio global: o conhecimento paramétrico do modelo NUNCA é usado para
# compor respostas — só evidências (anexos, RAG, tools). Layer A (estas funções)
# injeta a diretiva no prompt; Layer B (_grounding_guard) recusa no VerifyEvidence
# quando não há evidência. Escape hatch por agente: allow_general_knowledge.


def _build_grounding_directive() -> str:
    """Diretiva estrita de fundamentação — prependida ao system prompt.

    Modelos (sobretudo open-weight) tendem a "preencher lacunas" com
    conhecimento próprio quando a evidência é fraca/ausente. Esta diretiva
    imperativa corta o impulso: a resposta deve derivar SÓ do contexto
    fornecido. Complementa a regra "Nunca fabrique o Output Contract" da
    seção de Ferramentas. Suprimida quando o agente tem
    allow_general_knowledge (escape hatch de "solicitado CLARAMENTE").
    """
    return (
        "[FONTE DA RESPOSTA — APENAS EVIDÊNCIAS]\n"
        "Responda EXCLUSIVAMENTE com base nas evidências fornecidas neste "
        "contexto: documentos anexados, trechos de base de conhecimento (RAG) "
        "e resultados de ferramentas (MCP/APIs). É TERMINANTEMENTE PROIBIDO "
        "usar conhecimento geral ou paramétrico do modelo, suposições, ou "
        "qualquer informação que não esteja explicitamente nas evidências. Se "
        "as evidências forem insuficientes ou ausentes, diga isso CLARAMENTE e "
        "NÃO invente — peça o documento ou dado faltante. Nunca preencha "
        "lacunas com conhecimento próprio."
    )


def _build_mcp_tools_prompt_section(mcp_tools: list, openai_tools: list | None) -> str:
    """Seção '## Ferramentas Disponíveis (MCP)' do system prompt.

    39.2.0 (item 3 PR3): a instrução SEGUE O MODO efetivo de cada conector —
    antes ensinava operation/query SEMPRE, contradizendo as funções per-tool
    expostas no function spec (o LLM via `github_create_issue` no spec e "use
    operation/query" no prompt). O sinal é o que o BUILD realmente produziu
    (`_schema_origin == 'discovered_per_tool'`), então a decisão per-conector
    do 39.0.0 atravessa sozinha. Sem função per-tool → texto byte-idêntico ao
    legado. Módulo-level: testável puro (sem construir o engine)."""
    import re as _re
    per_fns = [
        f for f in (openai_tools or [])
        if isinstance(f, dict) and f.get("_schema_origin") == "discovered_per_tool"
    ]
    per_servers = {f.get("_mcp_server_tool") for f in per_fns}
    legacy_tools = [
        t for t in mcp_tools
        if (t.get("name", "tool") or "tool") not in per_servers
    ]

    tool_catalog_lines: list[str] = []
    for t in legacy_tools:
        raw_name = t.get("name", "tool") or "tool"
        fn_name = _re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name).strip('_')[:64]
        ops = t.get("operations", []) or []
        ops_str = ", ".join(ops) if ops else "(sem operações declaradas)"
        desc = (t.get("description") or "").strip()
        server = t.get("mcp_server", "")
        line = f"- **{raw_name}** (function `{fn_name}`, operações: {ops_str})"
        if desc:
            line += f"\n  {desc[:300]}"
        if server:
            line += f"\n  servidor MCP: {server}"
        tool_catalog_lines.append(line)
    for f in per_fns:
        fn = f.get("function") or {}
        line = (f"- **{fn.get('name', '?')}** (função per-tool — chame pelo "
                f"próprio nome; servidor: {f.get('_mcp_server_tool', '?')})")
        desc = (fn.get("description") or "").strip()
        if desc:
            line += f"\n  {desc[:300]}"
        tool_catalog_lines.append(line)
    tool_catalog = "\n".join(tool_catalog_lines)

    if per_fns and legacy_tools:
        how = (
            "**Como chamar**: as funções marcadas como per-tool são chamadas "
            "pelo PRÓPRIO nome, com os parâmetros do schema de cada uma — NÃO "
            "use `operation`/`query` nelas. Para as demais ferramentas, use o "
            "function call com `operation` (uma das operações listadas acima) "
            "e `query` (a consulta ou parâmetros em string). "
            "Aguarde o retorno antes de gerar sua resposta final."
        )
    elif per_fns:
        how = (
            "**Como chamar**: cada função acima é chamada pelo PRÓPRIO nome, "
            "com os parâmetros do schema dela — NÃO existe `operation`/`query` "
            "neste modo. Aguarde o retorno antes de gerar sua resposta final."
        )
    else:
        how = (
            "**Como chamar**: use o function call com `operation` (uma das operações "
            "listadas acima) e `query` (a consulta ou parâmetros em string). "
            "Aguarde o retorno antes de gerar sua resposta final."
        )

    return (
        "\n## Ferramentas Disponíveis (MCP)\n"
        "Você TEM function calls registrados para as ferramentas abaixo. "
        "**REGRA CRÍTICA**: se a solicitação do usuário puder ser atendida "
        "por uma destas ferramentas (ex: busca na web, consulta de documentação, "
        "extração de dados externos, pesquisa factual), **você DEVE chamar "
        "a ferramenta apropriada ANTES de gerar qualquer resposta**.\n\n"
        f"{tool_catalog}\n\n"
        "**Nunca fabrique o conteúdo do Output Contract.** "
        "Se o Output Contract pede um array `results`, esse array deve vir "
        "do retorno real da ferramenta — jamais de um `results: []` vazio "
        "inventado. Se nenhuma ferramenta se aplica, explique isso em texto "
        "e NÃO monte o JSON do Output Contract.\n\n"
        + how
    )


def _build_grounding_closing() -> str:
    """Reminder curto de fundamentação no fim do prompt (estratégia sanduíche)."""
    return (
        "[LEMBRETE FINAL — FUNDAMENTAÇÃO]\n"
        "Antes de responder: cada afirmação deve derivar de uma evidência "
        "fornecida. Se não há evidência que a sustente, declare a ausência em "
        "vez de recorrer ao conhecimento geral do modelo."
    )


# Mensagem de recusa quando a guarda de grounding dispara. Constante pra UI/teste
# poderem casar sem duplicar o texto.
GROUNDING_REFUSAL_REASON = (
    "Não há evidências para fundamentar uma resposta (nenhum documento anexado, "
    "base de conhecimento ou resultado de ferramenta), e este agente não está "
    "autorizado a usar conhecimento geral do modelo. Anexe um documento, vincule "
    "uma base de conhecimento, habilite uma ferramenta de busca, ou ative "
    "'Permitir conhecimento geral' na edição do agente."
)


def _grounding_guard(
    *,
    strict: bool,
    allow_general_knowledge: bool,
    has_evidences: bool,
    has_attachments: bool,
    has_pipeline_context: bool,
    has_tool_output: bool,
    draft: str = "",
) -> tuple[bool, str]:
    """Decide se a resposta deve ser RECUSADA por falta de fundamentação.

    Coração do princípio grounded-by-default (2026-06-06): quando `strict` e o
    agente não tem o escape hatch (`allow_general_knowledge`), a resposta só é
    permitida se houver ao menos UMA fonte de evidência — RAG, anexo, output de
    ferramenta (MCP/API) ou contexto de pipeline upstream. Sem nenhuma, a
    resposta só poderia vir do conhecimento paramétrico do modelo → recusa
    controlada (honesta) em vez de alucinação silenciosa.

    Drafts de erro do próprio sistema (começam com "⚠") são poupados: já são
    mensagens acionáveis, não respostas paramétricas.

    Returns:
        (deve_recusar, motivo). motivo == "" quando não recusa.
    """
    if not strict or allow_general_knowledge:
        return False, ""
    if draft and draft.lstrip().startswith("⚠"):
        return False, ""
    grounded = (
        has_evidences or has_attachments or has_pipeline_context or has_tool_output
    )
    if grounded:
        return False, ""
    return True, GROUNDING_REFUSAL_REASON


async def _has_tool_grounding(interaction_id: str | None) -> bool:
    """True se houve ≥1 invocação REAL de ferramenta (MCP tool_call ou API
    binding execution) nesta interação.

    Reconhece respostas fundamentadas só em tools (ex.: busca web sem RAG/anexo)
    para o _grounding_guard não recusá-las por engano. As invocações já foram
    persistidas pelo harness ANTES do VerifyEvidence. Best-effort: erro de query
    → False (a guarda erra para o lado seguro — recusa — em vez de liberar).
    """
    if not interaction_id:
        return False
    try:
        from app.core.database import tool_calls_repo
        if await tool_calls_repo.find_all(interaction_id=interaction_id, limit=1):
            return True
    except Exception:
        pass
    try:
        from app.core.database import binding_executions_repo
        if await binding_executions_repo.find_all(interaction_id=interaction_id, limit=1):
            return True
    except Exception:
        pass
    return False


def _pipeline_should_self_retrieve(
    *,
    has_pipeline_context: bool,
    declared_sources: bool,
    skip_evidence: bool,
) -> bool:
    """Decide se um agente DENTRO de um pipeline deve consultar suas PRÓPRIAS
    bases (RAG), em vez de só herdar o texto do upstream.

    Fix 2026-06-06 (RAG em pipeline): historicamente QUALQUER agente com
    `pipeline_context` pulava o retrieval — então um especialista downstream
    ficava CEGO às KBs que declarou em `evidence_policy.sources`. Bug relatado:
    "router → Retenção" devolvia evidência vazia apesar das KBs corretas.

    Regra (opt-in ESTRITO, zero regressão): só auto-recupera quando há contexto
    de pipeline E o agente DECLARA sources (lista populada — sinal explícito de
    que tem KBs próprias) E o retrieval não foi explicitamente pulado. Pipelines
    sem declaração seguem idênticos ao comportamento antigo; `sources=None`
    (legado) e `sources=[]` (bloqueado) NÃO contam como declaração.

    `declared_sources` deve ser pré-computado pelo chamador como
    ``isinstance(allowed_sources, list) and len(allowed_sources) > 0``.
    """
    return has_pipeline_context and declared_sources and not skip_evidence


def _no_evidence_diagnostic(*, sources_ignored: list | None) -> dict:
    """Diagnóstico honesto para o caso "zero evidência" no fim da interação.

    Quando o agente DECLARA fontes em ``## Evidence Policy`` (lista populada) mas
    o RAG foi PULADO — porque ``require_evidence`` está desligado ou o
    ``Execution Profile`` inferido é ``fast`` —, essas fontes são silenciosamente
    ignoradas. É um footgun clássico: o especialista responde "sem evidência"
    apesar de KBs corretas e populadas, e o operador recebe o conselho GENÉRICO
    ("registre bases") que não corresponde à causa real. Aqui, quando o chamador
    sinaliza fontes ignoradas, devolvemos um aviso ACIONÁVEL apontando a causa
    (require_evidence off) e a correção. Função pura → testável isoladamente.
    """
    if sources_ignored:
        return {
            "level": "warning",
            "text": (
                f"As {len(sources_ignored)} fonte(s) declaradas em ## Evidence Policy "
                "NÃO foram consultadas porque 'Exigir evidência' (require_evidence) está "
                "desligado neste agente. Ligue 'Exigir evidência' para fundamentar as "
                "respostas nessas bases."
            ),
        }
    return {
        "level": "info",
        "text": "Nenhuma evidência encontrada. Registre bases de conhecimento em Evidência para habilitar RAG.",
    }


# ═══════════════════════════════════════════════════
# Helpers — Pre-check de configuração do LLM provider
# ═══════════════════════════════════════════════════


def _resolve_provider_config(provider: str, settings) -> tuple[str, Optional[str]]:
    """Verifica se o provider tem configuração suficiente pra ser invocado.

    Returns:
        (api_key_efetiva, missing_reason)
        - api_key_efetiva: valor a passar adiante (pode ser sentinel como
          'ollama' ou 'not-needed').
        - missing_reason: None quando OK; string com motivo curto pra exibir
          ao user quando bloqueia.

    Onda 7 Wave 4 + bug 2026-05-27 (workspace mostrava "API Key do provedor
    'gpt-oss-120b' não configurada" mesmo com URL + key='not-needed' setadas):
    - "openai"/"azure": ambos consomem azure_openai_api_key (legacy alias).
    - "ollama": dispensa key real, sentinel 'ollama' é OK.
    - "gpt-oss-20b"/"gpt-oss-120b": hub interno autentica via rede privada/
      mTLS; 'not-needed' (e mesmo vazio) é aceito. O que IMPORTA é a URL.
    - Outros: provider desconhecido → bloqueia com missing_reason.

    Placeholders ('sk-your', 'mrt-your', 'change', 'your-...') também
    bloqueiam — significam que ninguém configurou de verdade.
    """
    PLACEHOLDER_PREFIXES = ("sk-your", "your-", "mrt-your", "change", "placeholder")

    if provider in ("openai", "azure"):
        api_key = (settings.azure_openai_api_key or "").strip()
        if not api_key or api_key.startswith(PLACEHOLDER_PREFIXES):
            return "", "API Key do Azure OpenAI não está configurada"
        return api_key, None

    if provider == "openai_public":
        # PR #194 (2026-05-29): OpenAI público real (api.openai.com). Key
        # separada de Azure pra operador comparar latência/custo.
        api_key = (settings.openai_public_api_key or "").strip()
        if not api_key or api_key.startswith(PLACEHOLDER_PREFIXES):
            return "", "API Key do OpenAI público não está configurada"
        return api_key, None

    if provider == "maritaca":
        api_key = (settings.maritaca_api_key or "").strip()
        if not api_key or api_key.startswith(PLACEHOLDER_PREFIXES):
            return "", "API Key da Maritaca não está configurada"
        return api_key, None

    if provider == "ollama":
        # Ollama dispensa key real — 'ollama' é o sentinel default.
        return settings.ollama_api_key or "ollama", None

    if provider == "gpt-oss-20b":
        # GPT-OSS: a chave NÃO é a barreira (proxy interno autentica via rede
        # privada/mTLS). URL é o que importa — sem URL, agente não chama nada.
        if not (settings.oss20b_url or "").strip():
            return "", "URL do GPT-OSS-20B não está configurada"
        return settings.oss20b_api_key or "not-needed", None

    if provider == "gpt-oss-120b":
        if not (settings.oss120b_url or "").strip():
            return "", "URL do GPT-OSS-120B não está configurada"
        return settings.oss120b_api_key or "not-needed", None

    return "", f"Provider '{provider}' desconhecido — sem mapeamento de configuração"


# ═══════════════════════════════════════════════════
# Helpers — Cadeia de resiliência LLM em runtime
# ═══════════════════════════════════════════════════

async def _runtime_llm_candidates(agent: dict, settings) -> list[tuple[str, str]]:
    """Cadeia de resiliência LLM em runtime, na ordem pedida pelo operador:
    1) modelo escolhido para o Agente (já resolvido via task_type/primário
       antes desta chamada);
    2) Modelo Primário da plataforma (``primary_provider``/``primary_model``);
    3) Multimodal Fallback (modelo "sempre disponível", default azure/gpt-4o).

    Dedup por (provider, model) exato preservando ordem — no caso comum em que
    agente == primário (ex.: tudo gpt-oss-120b) os passos 1 e 2 colapsam e a
    cadeia vira ``[gpt-oss-120b, azure/gpt-4o]``, sem dobrar timeout no mesmo
    hub inalcançável.

    Pré-filtra candidatos sem configuração mínima (ex.: GPT-OSS sem URL) pra não
    "tentar" — e quebrar com AttributeError — um provider que nunca responderia.
    O candidato #0 (do agente) sempre entra: já foi validado pelo gate de
    ``_resolve_provider_config`` no caller antes de chegarmos aqui.
    """
    raw: list[tuple[str, str]] = []
    ap = (agent.get("llm_provider") or "").strip()
    am = (agent.get("model") or "").strip()
    if ap and am:
        raw.append((ap, am))
    pp = (getattr(settings, "primary_provider", "") or "").strip()
    pm = (getattr(settings, "primary_model", "") or "").strip()
    if pp and pm:
        raw.append((pp, pm))
    try:
        from app.llm_routing import load_routing as _load_routing
        routing = await _load_routing()
        fb = (routing.get("multimodal_fallback") or "").strip()
        if "/" in fb:
            fp, fm = fb.split("/", 1)
            fp, fm = fp.strip(), fm.strip()
            if fp and fm:
                raw.append((fp, fm))
    except Exception as e:
        logger.warning(f"_runtime_llm_candidates: load_routing falhou: {e}")

    seen: set = set()
    out: list[tuple[str, str]] = []
    for i, (p, m) in enumerate(raw):
        key = (p.lower(), m.lower())
        if key in seen:
            continue
        if i > 0:
            # candidatos de contingência só entram se tiverem config mínima
            _, missing = _resolve_provider_config(p, settings)
            if missing:
                logger.info(
                    f"_runtime_llm_candidates: pulando {p}/{m} (sem config: {missing})"
                )
                continue
        seen.add(key)
        out.append((p, m))
    return out


async def _fallback_show_in_trace() -> bool:
    """Wrapper — lê o checkbox 'exibir aviso de contingência no painel'
    (canônico em ``app.llm_routing``). NÃO afeta observabilidade/LOGs (que
    registram o fallback sempre); só controla a NOTA visível pro usuário."""
    try:
        from app.llm_routing import fallback_show_in_trace
        return await fallback_show_in_trace()
    except Exception:
        return True


def _collect_token_usage(result: dict, agent: dict, agent_id: str) -> dict:
    """Coleta usage por chamada LLM e calcula tokens da interação.

    Extraído de ``execute_interaction`` sem mudança de comportamento — a cadeia
    de resiliência só chama isto quando houve resposta real (``result`` não-None).

    IMPORTANTE: somar input_tokens ingenuamente conta o histórico/system prompt
    N vezes (cada chamada da reflexão/tool-loop reenvia tudo). Convenção:
        input  = input da ÚLTIMA chamada (tamanho final do prompt)
        output = SOMA dos outputs (cada geração é única)
        total  = input + output
        calls  = quantas chamadas LLM aconteceram nesta interação
    """
    per_call: list[dict] = []
    for _m in (result.get("messages") or []):
        um = getattr(_m, "usage_metadata", None) or {}
        if um:
            per_call.append({
                "input": int(um.get("input_tokens") or 0),
                "output": int(um.get("output_tokens") or 0),
            })
            continue
        rm = getattr(_m, "response_metadata", None) or {}
        tu = (rm.get("token_usage") or rm.get("usage") or {}) if isinstance(rm, dict) else {}
        if tu:
            per_call.append({
                "input": int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0),
                "output": int(tu.get("completion_tokens") or tu.get("output_tokens") or 0),
            })
    if per_call:
        tin_last = per_call[-1]["input"]
        tout_sum = sum(c["output"] for c in per_call)
        tin_billed_sum = sum(c["input"] for c in per_call)  # útil para custo/billing
        tokens = {
            "input": tin_last,
            "output": tout_sum,
            "total": tin_last + tout_sum,
            "calls": len(per_call),
            "input_billed_sum": tin_billed_sum,
            "total_billed": tin_billed_sum + tout_sum,
        }
        # Cap de tokens — defesa LLM04 contra runaway loops e abuso de custo.
        # Não é interrupção retroativa (já consumiu) — sinaliza no trace e em log
        # para que ratelimit/quotas externas possam agir.
        from app.core.config import get_settings as _gs
        _cap = _gs().interaction_max_tokens
        _billed = tin_billed_sum + tout_sum
        if _cap and _billed > _cap:
            logger.warning(
                f"Token cap ultrapassado: agent={agent_id} billed={_billed} cap={_cap} calls={len(per_call)}"
            )
            tokens["cap_exceeded"] = True
            tokens["cap"] = _cap
        return tokens
    # Diagnóstico: nenhuma das messages tinha usage_metadata nem
    # response_metadata.token_usage / .usage. Provavelmente provider fora do
    # padrão LangChain (Maritaca/Sabia-4 reporta diferente). Loga shape pra
    # inspeção; follow-up: fallback via tiktoken.
    try:
        _shapes = []
        for _m in (result.get("messages") or [])[:5]:
            _shapes.append({
                "cls": type(_m).__name__,
                "has_usage_meta": bool(getattr(_m, "usage_metadata", None)),
                "rm_keys": sorted(list(getattr(_m, "response_metadata", {}) or {}).keys())[:8],
            })
        logger.info(
            f"Tokens=0 (provider={agent.get('llm_provider')} model={agent.get('model')}): "
            f"messages_shape={json.dumps(_shapes, ensure_ascii=False)[:500]}"
        )
    except Exception:
        pass
    return {"input": 0, "output": 0, "total": 0, "calls": 0, "input_billed_sum": 0, "total_billed": 0}


# ─── Cache in-process de providers FORA (falha de alcance) ─────────────────
# A cadeia de resiliência reordena candidatos para não queimar o timeout de
# novo num hub sabidamente morto (a mesma informação que o popover "Modelos em
# uso" mostra ao operador). Providers marcados NUNCA são removidos da cadeia —
# só vão para o fim; com TTL curto, a recuperação é detectada na 1ª tentativa
# após expirar. Sem isso, cada interação pagava ~60s de retries no primário
# morto antes de cair no fallback (incidente Aurora, 2026-07-02).
_LLM_DOWN_TTL_SECONDS = 90.0
_llm_down_at: dict[str, float] = {}


def _mark_llm_down(provider: str) -> None:
    _llm_down_at[provider] = time.monotonic()


def _mark_llm_up(provider: str) -> None:
    _llm_down_at.pop(provider, None)


def _llm_marked_down(provider: str) -> bool:
    t = _llm_down_at.get(provider)
    if t is None:
        return False
    if time.monotonic() - t > _LLM_DOWN_TTL_SECONDS:
        _llm_down_at.pop(provider, None)
        return False
    return True


async def _run_llm_chain(
    candidates: list[tuple[str, str]],
    agent: dict,
    run_attempt,
    agent_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Executa a cadeia de resiliência: tenta cada candidato em ordem até um
    responder. Orquestração PURA (sem construir harness/state) pra ser testável
    isolada do ``execute_interaction`` pesado — segue a convenção do projeto
    (cf. ``tests/test_platform_primary_model.py``).

    Args:
        candidates: lista ordenada ``[(provider, model), ...]`` (já deduplicada
            por ``_runtime_llm_candidates``). Índice 0 = modelo escolhido.
        agent: dict do agente; ESTE helper seta ``agent['llm_provider']`` e
            ``agent['model']`` em cada tentativa, então ao retornar eles refletem
            o ÚLTIMO candidato tentado (o que respondeu, em caso de sucesso).
        run_attempt: ``async (provider, model) -> result`` — constrói/roda a
            tentativa (harness+graph no caller). Pode levantar exceção.
        agent_id: pra logs.

    Returns:
        ``(result, attempted)``. ``result`` é ``None`` quando TODOS os candidatos
        foram inalcançáveis (cadeia esgotada). ``attempted`` é a lista
        ``["provider/model", ...]`` tentada, em ordem.

    Raises:
        Propaga QUALQUER exceção que NÃO seja de alcance (``is_llm_unreachable``
        False) — ex.: 401/404/429. O caller mapeia pra mensagem acionável. Só
        falhas de "não responde" (conexão/timeout/URL ausente) disparam fallback.
    """
    # Candidatos recém-marcados FORA vão pro fim da fila (nunca são pulados de
    # vez): se o primário está morto há segundos, a resposta vem do fallback
    # imediatamente em vez de re-pagar o timeout. `_chosen` do caller usa a
    # lista ORIGINAL, então a nota de contingência continua correta.
    # Deferral = marcador local (por-processo, defer-após-1, fix Aurora) OU
    # circuito ABERTO no breaker cross-worker (33.1.0): um provider que a frota
    # já viu cair é preterido AQUI mesmo que ESTE worker nunca tenha falhado nele.
    _deferred = []
    for _cand_p, _cand_m in candidates:
        _is_down = _llm_marked_down(_cand_p) or await breaker.is_open(
            canonical_provider(_cand_p)
        )
        _deferred.append(_is_down)
    down = [c for c, d in zip(candidates, _deferred) if d]
    if down and len(down) < len(candidates):
        alive = [c for c, d in zip(candidates, _deferred) if not d]
        logger.info(
            "agent.llm.chain.reordered",
            extra={
                "event": "agent.llm.chain.reordered",
                "agent_id": agent_id,
                "deferred": [f"{p}/{m}" for p, m in down],
            },
        )
        candidates = alive + down

    attempted: list[str] = []
    result = None
    for ci, (cand_p, cand_m) in enumerate(candidates):
        agent["llm_provider"] = cand_p
        agent["model"] = cand_m
        attempted.append(f"{cand_p}/{cand_m}")
        try:
            result = await run_attempt(cand_p, cand_m)
        except Exception as attempt_exc:
            if is_llm_param_rejection(attempt_exc) and agent.get("reasoning_effort"):
                # O modelo do candidato rejeitou um PARÂMETRO opcional (ex.:
                # azure/gpt-4o com reasoning_effort → 400 "Unrecognized request
                # argument"). Re-tenta UMA vez o MESMO candidato sem o parâmetro
                # — sem isso o 400 propagava e derrubava a interação inteira
                # justamente no fallback (incidente Aurora, 2026-07-02). O strip
                # fica no dict em memória (não persiste no agente).
                logger.warning(
                    "agent.llm.param_rejected_stripped",
                    extra={
                        "event": "agent.llm.param_rejected_stripped",
                        "agent_id": agent_id,
                        "provider": cand_p,
                        "model": cand_m,
                        "stripped": ["reasoning_effort"],
                        "error": str(attempt_exc)[:200],
                    },
                )
                agent["reasoning_effort"] = None
                try:
                    result = await run_attempt(cand_p, cand_m)
                    _mark_llm_up(cand_p)
                    await breaker.record_success(canonical_provider(cand_p))
                    break
                except Exception as retry_exc:
                    attempt_exc = retry_exc
            if not is_llm_unreachable(attempt_exc):
                # Não é "não responder" (404/401/etc) → propaga pro except
                # externo, que mapeia pra mensagem acionável específica.
                raise
            _mark_llm_down(cand_p)
            # Alimenta o breaker cross-worker: só chegamos aqui em falha de
            # ALCANCE (o `raise` acima barrou 4xx/param). A frota inteira passa a
            # preterir/pular este provider após cb_failure_threshold falhas.
            await breaker.record_failure(canonical_provider(cand_p))
            has_next = ci + 1 < len(candidates)
            # SEMPRE registra em observabilidade + LOG (independente do checkbox
            # show_in_trace, que só controla a NOTA visível na UI).
            logger.warning(
                "agent.llm.fallback",
                extra={
                    "event": "agent.llm.fallback",
                    "agent_id": agent_id,
                    "attempt_index": ci,
                    "failed_provider": cand_p,
                    "failed_model": cand_m,
                    "next_in_chain": (
                        f"{candidates[ci + 1][0]}/{candidates[ci + 1][1]}"
                        if has_next else None
                    ),
                    "error_type": type(attempt_exc).__name__,
                },
                exc_info=attempt_exc,
            )
            result = None
            if has_next:
                continue
            # cadeia esgotada — todos os modelos inalcançáveis
            logger.error(
                "agent.llm.chain.exhausted",
                extra={
                    "event": "agent.llm.chain.exhausted",
                    "agent_id": agent_id,
                    "attempted": attempted,
                },
                exc_info=attempt_exc,
            )
            break
        # sucesso nesta tentativa — encerra a cadeia
        _mark_llm_up(cand_p)
        await breaker.record_success(canonical_provider(cand_p))
        break
    return result, attempted


# ═══════════════════════════════════════════════════
# Helpers — Structured Output (Wave atual)
# ═══════════════════════════════════════════════════


def _extract_json_schema_from_contract(contract: str) -> dict | None:
    """Extrai um JSON Schema do bloco fenced ```json ... ``` em ## Output Contract.

    O parser de SKILL.md mantém o conteúdo bruto da seção em
    `parsed.output_contract`. Esta função procura o primeiro bloco
    fenced JSON dentro e parseia.

    Returns:
        dict (JSON Schema) ou None se não encontrar bloco ou JSON inválido.

    Aceita variações:
    - ```json ... ``` (com hint de linguagem)
    - ``` ... ``` (sem hint) — tenta parsear como JSON
    - JSON cru sem fence (raro) — tenta parsear o contract inteiro
    """
    if not contract or not contract.strip():
        return None
    import re as _re
    # 1. Bloco fenced ```json ... ```
    m = _re.search(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n```", contract)
    candidate = m.group(1).strip() if m else contract.strip()
    try:
        schema = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as e:
        # Antes silencioso (return None). Sem isso, ## Output Contract com
        # JSON quebrado fazia o runtime mandar prompt cru pro LLM sem
        # response_format — operador via resposta sem estrutura e não
        # entendia o porquê. Agora vai pro errors.log com preview do
        # que tentamos parsear.
        logger.warning(
            "engine.output_contract_json_invalid",
            extra={
                "event": "engine.json_invalid",
                "section": "Output Contract",
                "candidate_preview": candidate[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return None
    # JSON Schema válido tem que ser objeto com pelo menos type ou properties.
    if not isinstance(schema, dict):
        logger.warning(
            "engine.output_contract_not_object",
            extra={
                "event": "engine.schema_invalid",
                "section": "Output Contract",
                "actual_type": type(schema).__name__,
            },
        )
        return None
    if not (schema.get("type") or schema.get("properties") or schema.get("$ref")):
        logger.warning(
            "engine.output_contract_empty_schema",
            extra={
                "event": "engine.schema_invalid",
                "section": "Output Contract",
                "keys": list(schema.keys())[:10],
            },
        )
        return None
    return schema


# ═══════════════════════════════════════════════════
# LangGraph State
# ═══════════════════════════════════════════════════

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], lambda x, y: list(x) + list(y)]
    current_agent: str
    agent_kind: str
    iteration: int
    max_iterations: int
    context: dict
    envelope: dict
    skill_data: dict
    metadata: dict


# ═══════════════════════════════════════════════════
# DeepAgent Harness — loop de raciocínio §4
# ═══════════════════════════════════════════════════

class DeepAgentHarness:
    """Harness para execução profunda com auto-reflexão e MCP tool calling."""

    def __init__(self, agent_config: dict, max_iterations: int = 3, mcp_tools: list = None, interaction_id: str = "", skill_md: str = ""):
        self.config = agent_config
        self.max_iterations = max_iterations
        # Onda B (schema-aware): skill_md (raw markdown) é passado pra
        # build_openai_tools que vai ler ## Inputs e expor schema custom
        # pro LLM em vez do {operation, query} fixo. None/"" preserva
        # comportamento legacy.
        self.skill_md = skill_md or ""
        # interaction_id propagado pelo FSM (state_machine.run_intake) para que
        # cada tool_call seja persistida com FK válida em tool_calls.interaction_id.
        # Sem isso a métrica "MCP TOOLS" do painel de rastreabilidade não consegue
        # contar invocações reais (ficaria zerada mesmo com tools chamadas).
        self.interaction_id = interaction_id or ""
        # temperature pode vir como None/str/float — normaliza para float com fallback
        try:
            _temp = float(agent_config.get("temperature") if agent_config.get("temperature") is not None else 0.7)
        except (TypeError, ValueError):
            _temp = 0.7
        self.provider = get_provider(
            agent_config.get("llm_provider", "openai"),
            model=agent_config.get("model"),
            temperature=_temp,
            reasoning_effort=(agent_config.get("reasoning_effort") or None),
        )
        self.mcp_tools = mcp_tools or []
        self.openai_tools = []
        if self.mcp_tools:
            from app.mcp.runtime import build_openai_tools
            # Onda B: passa skill_md pra build_openai_tools respeitar ## Inputs
            # quando declarado. Resolve causa raiz dos bugs Context7 no NL path.
            self.openai_tools = build_openai_tools(self.mcp_tools, skill_md=self.skill_md)
        # Wave Structured Output (PR atual): se SKILL.md tem Output Contract
        # com JSON Schema E provider suporta response_format, cacheia o
        # response_format aqui pra reusar em todas as chamadas ainvoke.
        # None quando: contract ausente, schema malformado, provider sem
        # suporte, ou tools MCP presentes (json_schema + tools no OpenAI
        # podem conflitar em alguns providers — privilegiamos tools por ser
        # path principal de produção). Caller cai em fallback (prompt-only).
        self._response_format = self._build_response_format()

    def _build_response_format(self) -> dict | None:
        """Extrai JSON Schema do Output Contract e monta payload pro provider.

        Heurística:
        - Se provider declara supports_structured_output=False → None.
        - Se há MCP tools → None (evita conflito tools + json_schema; ver
          comentário no __init__).
        - Se output_contract não tem bloco ```json ... ``` parseável → None.

        Returns:
            dict no formato OpenAI {"type":"json_schema","json_schema":{...,"strict":true}}
            ou None pra fallback no prompt-injection.
        """
        if not getattr(self.provider, "supports_structured_output", False):
            return None
        if self.openai_tools:
            # Tools MCP + json_schema podem brigar em alguns providers (Azure
            # 2024-08 aceita; Maritaca/OSS desconhecido). Por segurança,
            # privilegia tools. Skill ainda valida via ContractValidator no fim.
            return None
        skill = self.config.get("_parsed_skill", {}) or {}
        contract = skill.get("output_contract") or ""
        schema = _extract_json_schema_from_contract(contract)
        if not schema:
            return None
        # OpenAI exige name casando com ^[a-zA-Z0-9_-]+$. O `title` do schema vem
        # cru do SKILL.md (operador escreve "Saída da Categorizar Imagem" com
        # espaço e acento) e quebrava o request com 400. Centralizamos a
        # sanitização em sanitize_schema_name pra evitar a divergência.
        from app.core.text_utils import (
            coerce_to_openai_strict_schema,
            sanitize_schema_name,
        )
        name = sanitize_schema_name(schema.get("title"), fallback="SkillOutput")
        # Strict mode da OpenAI exige `required` listando TODAS as keys de
        # `properties` e `additionalProperties: false` em cada objeto. Skills
        # raramente declaram isso manualmente — coercionamos o schema antes
        # de enviar para evitar 400 "Invalid schema for response_format".
        strict_schema = coerce_to_openai_strict_schema(schema)
        return {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "schema": strict_schema,
                "strict": True,
            },
        }

    def _apply_response_format(self, llm):
        """Aplica self._response_format ao LLM via .bind(...) se habilitado.

        Tolerante a falha: se LangChain rejeitar o kwarg, segue sem bind
        (caller cai em fallback prompt-only). Esse cenário acontece em
        versões antigas do langchain-openai ou providers sem suporte.
        """
        if not self._response_format:
            return llm
        try:
            return llm.bind(response_format=self._response_format)
        except Exception as e:
            logger.warning(
                "structured_output: bind(response_format=...) falhou — fallback prompt-only",
                extra={
                    "event": "engine.structured_output.bind_failed",
                    "error_type": type(e).__name__,
                },
            )
            return llm

    def _build_system_prompt(self) -> str:
        """Constrói system prompt a partir do SKILL.md carregado.

        A seção de Ferramentas Disponíveis é colocada DEPOIS do Output Contract
        e inclui catálogo explícito com o nome exato (sanitizado) que o LLM
        deve usar no function call — isso evita que o modelo priorize fabricar
        o shape do Output Contract em vez de invocar a ferramenta.

        Idioma da resposta é prependido como diretiva curta antes do system
        prompt do agente — modelos open-weight tendem a espelhar idioma do
        contexto/evidências (busca Tavily em inglês → resposta em inglês)
        se não houver instrução explícita. Resolução em cascata:
        agent.response_language > settings.default_response_language > pt-BR.
        """
        from app.core.config import get_settings as _get_settings_lang
        skill = self.config.get("_parsed_skill", {})
        parts = []
        # Contrato de Decisão lido ANTES das diretivas de idioma: com ## Decisions
        # declarado, ambas ganham a exceção "linha DECISAO verbatim" (colisão
        # idioma×contrato — review 2026-07-15). Sem contrato, as diretivas são
        # byte-idênticas às de sempre (reprodutibilidade).
        _decisions = skill.get("_decisions_schema")
        # Idioma — DIRETIVA prependida (antes do system_prompt do agent) pra
        # ter precedência forte na atenção do LLM.
        _lang = _resolve_response_language(self.config, _get_settings_lang())
        parts.append(_build_response_language_directive(_lang, preserve_decision_line=bool(_decisions)))
        # Grounded-by-default — diretiva estrita de fundamentação. Gated por
        # settings.grounding_strict E pela ausência do escape hatch do agente
        # (allow_general_knowledge). Quando o agente PODE usar conhecimento geral,
        # não injeta — libera o modelo. Enforcement real (recusa) vive no
        # _grounding_guard do execute_interaction (defesa em profundidade).
        _gk_allowed = bool(self.config.get("allow_general_knowledge") or 0)
        _grounding_on = bool(getattr(_get_settings_lang(), "grounding_strict", True))
        _inject_grounding = _grounding_on and not _gk_allowed
        if _inject_grounding:
            parts.append(_build_grounding_directive())
        # Output Shape — diretiva de TAMANHO da resposta. Quando skill declara
        # length_preset em ## Output Shape, engine injeta limite explícito.
        # Sem declaração, usa default ('digest' = 1500 chars) — comportamento
        # back-compat suave (skills antigas continuam ~mesmo tamanho).
        _shape = skill.get("_output_shape_parsed") or {}
        _preset = _shape.get("length_preset")
        if _preset:
            from app.skill_parser.output_shape import build_directive as _len_directive
            parts.append(_len_directive(_preset))
        parts.append(
            self.config.get("system_prompt", "Você é um agente inteligente."),
        )
        if skill.get("purpose"):
            parts.append(f"\n## Purpose\n{skill['purpose']}")
        if skill.get("workflow"):
            parts.append(f"\n## Workflow\n{skill['workflow']}")
        if skill.get("output_contract"):
            parts.append(f"\n## Output Contract\n{skill['output_contract']}")
        if skill.get("guardrails"):
            parts.append(f"\n## Guardrails\n{skill['guardrails']}")
        # Contrato de Decisão (Cond-C, 35.19.0): skill declara ## Decisions →
        # a plataforma SELA a instrução da linha `DECISAO:` no prompt. O gate
        # condicional (`decision.<campo>`) só enxerga o que esta linha anuncia —
        # substitui o 'escalar=sim' in output_lower combinado por telepatia.
        # (`_decisions` foi lido no topo do método, antes das diretivas de idioma.)
        if _decisions:
            parts.append(build_decisions_directive(_decisions))
        if self.mcp_tools:
            parts.append(_build_mcp_tools_prompt_section(
                self.mcp_tools, getattr(self, "openai_tools", None),
            ))
        if _inject_grounding:
            parts.append(_build_grounding_closing())
        parts.append(_build_response_language_closing(_lang, preserve_decision_line=bool(_decisions)))
        return "\n".join(parts)

    def _should_force_tool_call(self) -> bool:
        """Detecta se o SKILL.md pede invocação explícita de tool no Workflow.

        Heurística: procura na seção Workflow por verbos de invocação
        acoplados ao nome de uma das tools MCP disponíveis, ou por
        palavras-chave fortes ("chame", "conecte ao", "consulte via"…).
        """
        if not self.mcp_tools:
            return False
        skill = self.config.get("_parsed_skill", {}) or {}
        workflow = (skill.get("workflow") or "").lower()
        if not workflow:
            return False
        # palavras-chave imperativas + nomes de tools
        invoke_verbs = ("conectar ao", "chame", "chamar", "consulte", "consultar",
                         "execute", "executar", "use a ferramenta", "via mcp",
                         "buscar via", "search via", "fetch via")
        if any(v in workflow for v in invoke_verbs):
            return True
        for t in self.mcp_tools:
            name = (t.get("name") or "").lower()
            # token básico do nome (ex: "tavily" de "Tavily MCP Server")
            primary = name.split()[0] if name else ""
            if primary and primary in workflow:
                return True
        return False

    def _needs_reflection(self, response: AIMessage, state: AgentState) -> bool:
        """Decide se vale uma rodada extra de reflexão.

        Objetivo de desempenho: evitar loops de reflexão quando a primeira
        resposta já está suficientemente boa.
        """
        if state.get("iteration", 0) + 1 >= state.get("max_iterations", 1):
            return False

        skill = self.config.get("_parsed_skill", {}) or {}
        exec_mode = (skill.get("_execution_mode") or "standard").lower()
        if exec_mode == "fast":
            return False

        content = (getattr(response, "content", "") or "").strip()
        if not content:
            return True

        # Heurística de "resposta provavelmente ruim"
        if len(content) < 40:
            return True

        # Se o skill exige contrato JSON explícito, tenta validar minimamente.
        output_contract = (skill.get("output_contract") or "").lower()
        expects_json = "json" in output_contract or '"type"' in output_contract
        if expects_json and content.startswith("{"):
            try:
                json.loads(content)
            except Exception:
                return True

        # Em standard, só reflete em casos claros de baixa qualidade.
        if exec_mode == "standard":
            return False

        # Em rigorous, executa reflexão apenas quando há sinal de risco.
        guardrails = (skill.get("guardrails") or "").strip()
        if guardrails and "não" in content.lower() and len(content) < 120:
            return True
        return False

    def _choose_tool_strategy(self) -> str:
        """Decide a estratégia de tool execution para o modelo atual.

        Mantém o contrato canônico: dev declara Tool Bindings em SKILL.md,
        plataforma faz funcionar independente do modelo. Estratégias:

        - 'native'   — modelo suporta `tools` parameter (OpenAI-compat).
                       Caminho preferido. Performance e confiabilidade máximas.
        - 'prompted' — modelo não suporta nativo mas segue instruções bem.
                       Injeta schemas no system prompt e parseia JSON do output.
                       ~20-30% mais tokens, ~5-10% JSON malformado tolerado.
        - 'none'     — modelo sem nativo e sem capacidade prompted (modelos
                       minúsculos). Agent perde tools nesta rodada + audit warning.

        Sem tools registradas → 'none' direto (não precisa de strategy).
        """
        if not self.openai_tools:
            return "none"
        from app.core.llm_capabilities import supports_native_tools, supports_prompted_tools
        provider = self.config.get("llm_provider", "")
        model = self.config.get("model", "")
        if supports_native_tools(provider, model):
            return "native"
        if supports_prompted_tools(provider, model):
            return "prompted"
        return "none"

    async def _audit_tool_strategy_degraded(self, strategy: str, reason: str = ""):
        """Grava em audit_log quando a estratégia de tools degrada do nativo.

        Visibilidade operacional: quem opera enxerga que aquele agent está
        rodando subótimo (prompted custa mais tokens; none perde tools).
        Best-effort — falha não bloqueia agent.
        """
        try:
            from app.core.database import audit_repo
            await audit_repo.create({
                "entity_type": "agent",
                "entity_id": self.config.get("id", ""),
                "action": "tool_strategy_degraded",
                "details": json.dumps({
                    "strategy": strategy,  # 'prompted' | 'none'
                    "provider": self.config.get("llm_provider", ""),
                    "model": self.config.get("model", ""),
                    "tool_count": len(self.openai_tools),
                    "reason": reason or "modelo sem function calling nativo",
                    "interaction_id": self.interaction_id,
                }, ensure_ascii=False),
            })
        except Exception as e:
            logger.warning(f"audit tool_strategy_degraded falhou: {e}")

    async def reason(self, state: AgentState) -> AgentState:
        """Nó de raciocínio com suporte a tool calling MCP.

        Roteia entre 3 estratégias baseado em llm_capabilities (canônico):
        - native:   bind_tools() — caminho preferido
        - prompted: injeta schemas no prompt, parseia JSON do output
        - none:     sem tools (audit warning)
        """
        tool_strategy = self._choose_tool_strategy()
        # Caminho prompted: trata em método dedicado (sem bind_tools)
        if tool_strategy == "prompted":
            return await self._reason_prompted(state)

        system = self._build_system_prompt()
        messages = [SystemMessage(content=system)] + list(state["messages"])

        llm = self.provider.get_langchain_llm()
        handler = get_langfuse_handler(trace_name=f"agent_{self.config.get('id', 'x')}")
        callbacks = [handler] if handler else []

        # Audit quando estratégia 'none' e há tools registradas (degradação)
        if tool_strategy == "none" and self.openai_tools and state.get("iteration", 0) == 0:
            await self._audit_tool_strategy_degraded(
                "none",
                "modelo sem capability mínima — tools registradas serão ignoradas nesta rodada",
            )

        # tool_choice="required" força invocação de QUALQUER função na
        # primeira chamada quando a SKILL mostra claramente a intenção
        # (Workflow com verbo de invocação ou nome da tool). Sem isso o
        # LLM frequentemente prefere fabricar o shape do Output Contract.
        # Se só uma tool está registrada, força ESSA tool especificamente.
        force_tool = self._should_force_tool_call()
        iteration = state.get("iteration", 0)
        first_pass = iteration == 0

        if self.openai_tools and tool_strategy == "native":
            if force_tool and first_pass:
                if len(self.openai_tools) == 1:
                    tool_name_forced = self.openai_tools[0]["function"]["name"]
                    tool_choice = {"type": "function", "function": {"name": tool_name_forced}}
                    logger.info(f"MCP tool_choice=forced to '{tool_name_forced}' (first pass)")
                else:
                    tool_choice = "required"
                    logger.info("MCP tool_choice='required' (first pass, multiple tools)")
                llm_with_tools = llm.bind_tools(self.openai_tools, tool_choice=tool_choice)
            else:
                llm_with_tools = llm.bind_tools(self.openai_tools)
        else:
            # strategy='none' → invocação plain sem tools. Aplica
            # structured output (no-op quando _response_format é None, ex:
            # quando há tools — ver _build_response_format).
            llm_with_tools = self._apply_response_format(llm)

        try:
            response = await llm_with_tools.ainvoke(messages, config={"callbacks": callbacks})
        except Exception as e:
            # Alguns modelos (Ollama com Gemma/Llama pequenos, etc) não suportam
            # tool calling no formato OpenAI. Detecta pela mensagem do provider
            # e refaz a chamada SEM tools — o agente perde acesso a MCP nesta
            # rodada mas consegue responder em modo texto.
            err_str = str(e).lower()
            no_tools_signals = ("does not support tools", "tools not supported", "tool_choice", "function_call is not supported")
            if any(s in err_str for s in no_tools_signals) and self.openai_tools:
                logger.warning(f"LLM '{self.config.get('model','?')}' não suporta tools — refazendo sem MCP. Erro original: {str(e)[:200]}")
                # Fallback sem tools: bom momento pra aplicar response_format
                # se disponível (no caminho normal _response_format=None quando
                # há tools, mas neste fallback elas foram removidas).
                response = await self._apply_response_format(llm).ainvoke(messages, config={"callbacks": callbacks})
                # Curto-circuita: sem tools não há tool_calls a processar.
                md = dict(state.get("metadata") or {})
                md["reflect_recommended"] = self._needs_reflection(response, state)
                return {**state, "messages": [response], "iteration": state.get("iteration", 0) + 1, "metadata": md}
            raise
        if self.openai_tools:
            _tc = getattr(response, "tool_calls", None) or []
            logger.info(
                f"LLM response: tool_calls={len(_tc)} "
                f"content_len={len(getattr(response, 'content', '') or '')}"
            )

        if self.openai_tools and hasattr(response, 'tool_calls') and response.tool_calls:
            from langchain_core.messages import ToolMessage
            from app.mcp.runtime import execute_tool_call, resolve_per_tool_call

            current_messages = messages + [response]
            max_tool_rounds = 5
            # Depois da primeira chamada (que foi forced se aplicável), o
            # modelo precisa poder gerar resposta final livremente —
            # rebindamos SEM tool_choice para as rodadas subsequentes.
            llm_with_tools_auto = llm.bind_tools(self.openai_tools)

            for round_n in range(max_tool_rounds):
                if not hasattr(response, 'tool_calls') or not response.tool_calls:
                    break

                for tc in response.tool_calls:
                    tool_name = tc.get("name", "")
                    tool_args = tc.get("args", {})
                    tool_id = tc.get("id", f"call_{round_n}")

                    logger.info(f"MCP Tool Call [round={round_n}]: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:200]})")

                    _tc_start = time.time()
                    _tc_status = "completed"
                    try:
                        result_text = await execute_tool_call(
                            tool_name, tool_args, self.mcp_tools, timeout=30,
                            openai_tools=self.openai_tools,
                        )
                    except Exception as _tc_err:
                        _tc_status = "failed"
                        result_text = f"[tool error] {_tc_err}"
                        logger.warning(f"MCP Tool Call failed [{tool_name}]: {_tc_err}")
                    _tc_latency_ms = round((time.time() - _tc_start) * 1000, 2)

                    logger.info(f"MCP Tool Result [round={round_n}, {len(result_text)}B]: {result_text[:200]}")

                    # Persiste invocação real em tool_calls (best-effort). Resolve o registry
                    # entry pelo name pra capturar mcp_server e tool_id. Falha de persistência
                    # não bloqueia o agente — só loga warning.
                    try:
                        from app.core.database import tool_calls_repo
                        _matched = next((t for t in self.mcp_tools if t.get("name") == tool_name), None)
                        if _matched is None:
                            # F3 per-tool: a função tem o nome do TOOL real; acha
                            # o servidor via metadata p/ não perder o link de telemetria.
                            _pt = resolve_per_tool_call(tool_name, self.openai_tools)
                            if _pt and _pt.get("server_tool"):
                                _matched = next((t for t in self.mcp_tools if t.get("name") == _pt["server_tool"]), None)
                        await tool_calls_repo.create({
                            "id": str(uuid.uuid4()),
                            "tool_name": tool_name,
                            "mcp_server": (_matched or {}).get("mcp_server", ""),
                            "input_data": json.dumps(tool_args, ensure_ascii=False, default=str)[:5000],
                            "output_data": (result_text or "")[:5000],
                            "latency_ms": _tc_latency_ms,
                            "cost_usd": 0.0,
                            "interaction_id": self.interaction_id or "",
                            "tool_id": (_matched or {}).get("id", "") or "",
                            "status": _tc_status,
                        })
                    except Exception as _persist_err:
                        logger.warning(f"Falha ao persistir tool_call '{tool_name}': {_persist_err}")

                    current_messages.append(ToolMessage(
                        content=result_text,
                        tool_call_id=tool_id,
                    ))

                response = await llm_with_tools_auto.ainvoke(current_messages, config={"callbacks": callbacks})
                current_messages.append(response)

            md = dict(state.get("metadata") or {})
            md["reflect_recommended"] = self._needs_reflection(response, state)
            return {**state, "messages": [response], "iteration": state.get("iteration", 0) + 1, "metadata": md}

        md = dict(state.get("metadata") or {})
        md["reflect_recommended"] = self._needs_reflection(response, state)
        return {**state, "messages": [response], "iteration": state.get("iteration", 0) + 1, "metadata": md}

    async def _reason_prompted(self, state: AgentState) -> AgentState:
        """Loop de raciocínio com prompted_tools (modelo sem function calling nativo).

        Estratégia:
        1. Adiciona schemas das tools ao system prompt como instrução
        2. Modelo responde texto livre com blocos <tool_call>{...}</tool_call>
        3. Parser tolerante extrai chamadas válidas (descarta JSON malformado)
        4. Cada chamada vira execute_tool_call() (mesma infra do nativo)
        5. Resultado volta como HumanMessage formatada (modelo não entende role tool
           sem suporte nativo)
        6. Re-invoca até no max_tool_rounds OU resposta sem mais tool_calls

        Limpa blocos <tool_call> do conteúdo final para não vazar ao usuário.
        """
        from app.agents.prompted_tools import (
            build_prompted_tools_system,
            parse_tool_calls,
            strip_tool_calls,
            format_tool_result_message,
        )
        from app.mcp.runtime import execute_tool_call, resolve_per_tool_call
        from langchain_core.messages import HumanMessage

        # Audit visível: estratégia degradada do nativo
        if state.get("iteration", 0) == 0:
            await self._audit_tool_strategy_degraded(
                "prompted",
                "modelo sem function calling nativo — usando schemas em system prompt + parse JSON",
            )

        # System prompt: o original + instrução com schemas das tools
        system_base = self._build_system_prompt()
        system_full = system_base + "\n\n" + build_prompted_tools_system(self.openai_tools)
        messages = [SystemMessage(content=system_full)] + list(state["messages"])

        llm = self.provider.get_langchain_llm()
        handler = get_langfuse_handler(trace_name=f"agent_{self.config.get('id', 'x')}")
        callbacks = [handler] if handler else []

        # Invocação plain (sem bind_tools — modelo não suporta)
        response = await llm.ainvoke(messages, config={"callbacks": callbacks})

        current_messages = messages + [response]
        max_tool_rounds = 5
        any_tool_executed = False

        for round_n in range(max_tool_rounds):
            content = getattr(response, "content", "") or ""
            tool_calls = parse_tool_calls(content)
            if not tool_calls:
                break

            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc["arguments"]
                logger.info(
                    f"PROMPTED Tool Call [round={round_n}]: {tool_name}"
                    f"({json.dumps(tool_args, ensure_ascii=False)[:200]})"
                )

                _tc_start = time.time()
                _tc_status = "completed"
                try:
                    result_text = await execute_tool_call(
                        tool_name, tool_args, self.mcp_tools, timeout=30,
                        openai_tools=self.openai_tools,
                    )
                except Exception as _tc_err:
                    _tc_status = "failed"
                    result_text = f"[tool error] {_tc_err}"
                    logger.warning(f"PROMPTED Tool Call failed [{tool_name}]: {_tc_err}")
                _tc_latency_ms = round((time.time() - _tc_start) * 1000, 2)
                any_tool_executed = True

                # Persiste invocação (mesma infra do nativo)
                try:
                    from app.core.database import tool_calls_repo
                    _matched = next(
                        (t for t in self.mcp_tools if t.get("name") == tool_name), None,
                    )
                    if _matched is None:
                        # F3 per-tool: resolve o servidor via metadata da função.
                        _pt = resolve_per_tool_call(tool_name, self.openai_tools)
                        if _pt and _pt.get("server_tool"):
                            _matched = next((t for t in self.mcp_tools if t.get("name") == _pt["server_tool"]), None)
                    await tool_calls_repo.create({
                        "id": str(uuid.uuid4()),
                        "tool_name": tool_name,
                        "input_data": json.dumps(tool_args, ensure_ascii=False)[:5000],
                        "output_data": (result_text or "")[:5000],
                        "latency_ms": _tc_latency_ms,
                        "cost_usd": 0.0,
                        "interaction_id": self.interaction_id or "",
                        "tool_id": (_matched or {}).get("id", "") or "",
                        "status": _tc_status,
                        "agent_id": self.config.get("id", ""),
                    })
                except Exception as _persist_err:
                    logger.warning(f"persist tool_call (prompted) falhou: {_persist_err}")

                # Resultado volta como HumanMessage (modelo não entende role=tool sem nativo)
                current_messages.append(HumanMessage(
                    content=format_tool_result_message(tool_name, result_text),
                ))

            # Re-invoca para próxima rodada de raciocínio
            response = await llm.ainvoke(current_messages, config={"callbacks": callbacks})
            current_messages.append(response)

        # Limpa blocos <tool_call> da resposta final para não vazar ao usuário
        if any_tool_executed:
            final_text = strip_tool_calls(getattr(response, "content", "") or "")
            response = type(response)(content=final_text) if final_text else response

        md = dict(state.get("metadata") or {})
        md["reflect_recommended"] = self._needs_reflection(response, state)
        md["tool_strategy"] = "prompted"
        return {
            **state,
            "messages": [response],
            "iteration": state.get("iteration", 0) + 1,
            "metadata": md,
        }

    async def reflect(self, state: AgentState) -> AgentState:
        """Nó de reflexão — avalia e refina."""
        if state["iteration"] >= state["max_iterations"]:
            return state
        last = state["messages"][-1] if state["messages"] else None
        if not last:
            return state

        prompt = (
            "Avalie criticamente sua última resposta considerando o Output Contract e Guardrails do skill. "
            "Se satisfatória, responda 'SATISFATÓRIO'. Caso contrário, forneça versão refinada."
        )
        messages = list(state["messages"]) + [HumanMessage(content=prompt)]
        llm = self.provider.get_langchain_llm()
        response = await llm.ainvoke(messages)

        if "SATISFATÓRIO" in response.content.upper():
            return state
        return {**state, "messages": [response], "iteration": state["iteration"] + 1}

    def should_continue(self, state: AgentState) -> str:
        if state["iteration"] >= state["max_iterations"]:
            return "end"
        last = state["messages"][-1] if state["messages"] else None
        if last and "SATISFATÓRIO" in getattr(last, "content", "").upper():
            return "end"
        if not (state.get("metadata") or {}).get("reflect_recommended", False):
            return "end"
        return "reflect"

    def build_graph(self) -> StateGraph:
        g = StateGraph(AgentState)
        g.add_node("reason", self.reason)
        g.add_node("reflect", self.reflect)
        g.set_entry_point("reason")
        g.add_conditional_edges("reason", self.should_continue, {"reflect": "reflect", "end": END})
        g.add_edge("reflect", "reason")
        return g.compile()


# ═══════════════════════════════════════════════════
# AOBD — Agente Orquestrador de Business Domain §4.1
# ═══════════════════════════════════════════════════

class AOBDOrchestrator:
    """Interpreta intenção, consulta CAR, delega ao AR."""

    def __init__(self, agent_config: dict):
        self.config = agent_config
        self.provider = get_provider(agent_config.get("llm_provider", "openai"), model=agent_config.get("model"))

    async def interpret_intent(self, user_input: str) -> IntentDescriptor:
        """Produz IntentDescriptor estruturado a partir de texto natural."""
        prompt = f"""Analise a solicitação abaixo e produza um IntentDescriptor JSON com campos:
domain, process_candidate, entities (dict), constraints (dict), urgency (normal|high|critical), actor.

Solicitação: {user_input}

Responda APENAS com JSON válido."""

        response = await self.provider.generate([
            {"role": "system", "content": "Interpretador de intenção. Responda apenas JSON."},
            {"role": "user", "content": prompt},
        ])
        try:
            content = response["content"].strip()
            if "```" in content:
                import re
                m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
                if m: content = m.group(1)
            data = json.loads(content)
            return IntentDescriptor(**{k: data.get(k, "") for k in IntentDescriptor.__dataclass_fields__})
        except Exception:
            return IntentDescriptor(domain=self.config.get("domain", ""), process_candidate=user_input[:100])

    async def route_to_ar(self, intent: IntentDescriptor) -> Optional[dict]:
        """Consulta CAR e seleciona roteador. §6 matching híbrido."""
        entries = await car_repo.find_all(domain=intent.domain, status="active", limit=20)
        if not entries:
            all_routers = await agents_repo.find_all(kind="router", status="active", limit=20)
            if all_routers:
                return all_routers[0]
            return None

        best = None
        best_score = -1
        for entry in entries:
            keywords = json.loads(entry.get("activation_keywords", "[]"))
            score = sum(1 for kw in keywords if kw.lower() in intent.process_candidate.lower())
            score += entry.get("success_rate", 0)
            if score > best_score:
                best_score = score
                best = entry

        if best:
            agent = await agents_repo.find_by_id(best.get("skill_urn", "")) if best else None
            if not agent:
                routers = await agents_repo.find_all(kind="router", domain=intent.domain, status="active", limit=1)
                agent = routers[0] if routers else None
            return agent
        return None


# ═══════════════════════════════════════════════════
# Short-circuit — detecção de pass-through
# ═══════════════════════════════════════════════════

_GENERIC_PROMPT_MARKERS = [
    "você é um agente inteligente",
    "you are an intelligent agent",
    "você é um assistente",
    "you are an assistant",
    "você é um agente especializado",
    "you are a specialized agent",
]


def _is_passthrough(agent: dict) -> bool:
    """Detecta se o agente é um nó pass-through em pipeline.

    Condições (todas verdadeiras):
    1. Sem SKILL.md vinculado (skill_id vazio/nulo)
    2. System prompt ausente, vazio ou genérico

    Prompt é considerado genérico se:
    - Comprimento < 50 caracteres, ou
    - Comprimento < 200 caracteres E contém marcador genérico conhecido

    Retorna True se o agente deve ser ignorado no pipeline (sem chamada LLM).
    """
    # Tem skill vinculado → não é pass-through
    if agent.get("skill_id"):
        return False

    sp = (agent.get("system_prompt") or "").strip()

    # Sem prompt ou muito curto → pass-through
    if not sp or len(sp) < 50:
        return True

    # Prompt curto com marcadores genéricos → pass-through
    sp_lower = sp.lower()
    if len(sp) < 200:
        for marker in _GENERIC_PROMPT_MARKERS:
            if marker in sp_lower:
                return True

    return False


# ═══════════════════════════════════════════════════
# Executor Unificado — integra AOBD → AR → SA com FSM
# ═══════════════════════════════════════════════════

def _extract_inputs_from_text(text: str | None) -> dict:
    """Extrai um dict de `inputs` embutido no TEXTO (ex.: saída do roteador) para
    alimentar o binding declarativo (Fase B — Slice 1, 2026-06-07).

    Procura, nesta ordem: (1) bloco cercado ```json {...}```; (2) primeiro objeto
    {...} que parseie como dict. Se o objeto tiver a chave "inputs" (dict), usa-a
    (formato {"target": ..., "inputs": {...}} que o roteador emite). Retorna {}
    se nada parsear — tolerante por design (a prosa do roteador não atrapalha).
    """
    import re as _re

    t = text or ""
    candidates: list[str] = []
    fenced = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, _re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    inline = _re.search(r"(\{.*\})", t, _re.DOTALL)
    if inline:
        candidates.append(inline.group(1))
    for c in candidates:
        try:
            d = json.loads(c)
        except Exception:
            continue
        if isinstance(d, dict):
            return d["inputs"] if isinstance(d.get("inputs"), dict) else d
    return {}


async def _run_declarative_as_interaction(
    *, agent: dict, parsed_skill, user_input: str, session_id: str | None,
    sealed_inputs: dict | None = None,
) -> dict:
    """Roda uma SKILL declarativa (## API Bindings / ## Data Tables) pelo engine
    declarativo e adapta o retorno para o shape de `execute_interaction`.

    Fase A (2026-06-07): permite que uma SA declarativa rode seus bindings quando
    alcançada por um SR/AOBD via `execute_pipeline` — antes só funcionava no invoke
    direto / rota `/chat` (modo Agente). O caller (execute_pipeline/rota) é dono da
    sessão → `register_interaction=False` evita a interaction órfã "(declarativo)".

    NOTA Fase B (pendente): os inputs vêm do texto (JSON literal; senão
    {"question": <texto>}). A extração de parâmetros do NL (ex.: `cep`) para o
    binding é a Fase B — aqui o binding JÁ dispara; com inputs estruturados, roda.
    """
    import ast as _ast
    from app.agents.declarative_engine import execute_declarative

    msg = (user_input or "").strip()
    inputs: dict = {}
    # 1) JSON/dict puro (invoke estruturado, ou roteador que emite só o objeto)
    if msg.startswith("{") and msg.endswith("}"):
        for _loader in (json.loads, _ast.literal_eval):
            try:
                d = _loader(msg)
            except Exception:
                continue
            if isinstance(d, dict):
                inputs = d["inputs"] if isinstance(d.get("inputs"), dict) else d
                break
    # 2) bloco estruturado embutido na prosa do roteador (Fase B — Slice 1):
    #    ```json {"target": ..., "inputs": {...}}``` → extrai os inputs.
    if not inputs:
        inputs = _extract_inputs_from_text(msg)
    # 3) fallback legado: texto puro vira {"question": <texto>}
    if not inputs and msg:
        inputs = {"question": msg}

    # Envelope param SELADO (out-of-band): args marcados `x-uso: param` no ## Inputs
    # viajam FORA da prosa e o caller é SOBERANO — sobrepõem o que o roteador upstream
    # (AOBD/SR) tenha emitido no bloco {"inputs": ...}. Garante o valor determinístico,
    # intacto, mesmo atrás de uma cadeia de roteadores LLM.
    if sealed_inputs:
        inputs = {**inputs, **sealed_inputs}

    # Rastro da execução (gap do seeding Aurora, 2026-07-01): invoke direto e
    # pipeline chegam aqui SEM dono da sessão — com register_interaction=False e
    # nenhuma persistência local, a execução declarativa não aparecia em
    # /history nem em /agents/{id}/invocations. O /chat do workspace NÃO passa
    # por aqui (branch is_declarative própria persiste no handler); aqui o
    # engine é o dono: interaction + turno de entrada antes, turno de saída
    # depois. Best-effort — falha de DB não bloqueia a execução dos bindings.
    from app.agents.state_machine import _maybe_redact

    interaction_id = (session_id or "").strip() or str(uuid.uuid4())
    next_turn = 1
    persisted = False
    try:
        existing = await interactions_repo.find_by_id(interaction_id)
        if existing:
            old_turns = await turns_repo.find_all(interaction_id=interaction_id, limit=500)
            next_turn = max((int(t.get("turn_number") or 0) for t in old_turns), default=0) + 1
            await interactions_repo.update(interaction_id, {"state": "Intake"})
            # LGPD-2 (35.14.7, achado de auditoria #3): PARIDADE com o ramo
            # `existing` do run_intake do FSM (state_machine.py) — a cadeia
            # declarativa é uma via de criação DUPLICADA e o stamp do reuso (35.14.6)
            # só tocou o FSM. Sem isto, uma conversa de pipeline com agente-raiz
            # DECLARATIVO, reusando sessão nascida sem pivô, sobrevivia ao
            # forget_customer (casa por customer_hash). first-writer-wins.
            from app.core.interaction_access import (
                interaction_customer_hash_for_creation, stamp_interaction_customer_hash)
            _chash_reuse = interaction_customer_hash_for_creation()
            if _chash_reuse:
                await stamp_interaction_customer_hash(interaction_id, _chash_reuse)
        else:
            # Dono na CRIAÇÃO (35.4.0) — paridade com o run_intake do FSM.
            from app.core.interaction_access import (
                interaction_owner_for_creation, interaction_customer_hash_for_creation)
            _owner = interaction_owner_for_creation()
            _chash = interaction_customer_hash_for_creation()  # LGPD-2: pivô do esquecimento
            await interactions_repo.create({
                "id": interaction_id,
                "title": _maybe_redact(msg)[:80].strip() or (agent.get("name") or "agent")[:80],
                "agent_id": agent.get("id", ""),
                "channel": "api",
                "journey_id": "",
                "state": "Intake",
                **({"owner_user_id": _owner} if _owner else {}),
                **({"customer_hash": _chash} if _chash else {}),
            })
        from app.core.interaction_access import turn_customer_hash_fragment
        await turns_repo.create({
            "id": str(uuid.uuid4()),
            "turn_number": next_turn,
            "user_text_redacted": _maybe_redact(msg),
            "interaction_id": interaction_id,
            **turn_customer_hash_fragment(),  # LGPD-2 por-turno (35.15.0)
        })
        persisted = True
    except Exception as e:
        logger.warning("Declarativo (via cadeia): falha ao persistir interaction %s: %s", interaction_id, e)

    # session_id=interaction_id: o finalize interno do execute_declarative
    # (state final + ended_at) atualiza a MESMA row criada acima.
    decl = await execute_declarative(
        agent=agent, skill_parsed=parsed_skill, inputs=inputs,
        context=None, session_id=interaction_id, dry_run=False,
        register_interaction=False,
    )

    ctx_dict = decl.get("context") or {}
    has_overflow = any("excede max_bytes" in str(e or "") for e in (decl.get("errors") or []))
    if has_overflow and decl.get("api_response") is not None:
        ar = decl.get("api_response")
        output_text = ar if isinstance(ar, str) else json.dumps(ar, ensure_ascii=False, indent=2)
    elif "resposta" in ctx_dict:
        r = ctx_dict["resposta"]
        output_text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, indent=2)
    elif decl.get("api_response") is not None:
        ar = decl.get("api_response")
        output_text = ar if isinstance(ar, str) else json.dumps(ar, ensure_ascii=False, indent=2)
    else:
        output_text = decl.get("output", "") or ""

    executed = decl.get("bindings_executed") or []
    errors = decl.get("errors") or []
    any_success = any(200 <= b.get("status", 0) < 300 for b in executed)
    diag_level = "success" if (any_success and not errors) else ("warning" if any_success else "danger")
    diag_text = (
        f"Modo declarativo (via cadeia): {len(executed)} binding(s) executado(s)"
        + (f" · {len(errors)} aviso(s)/erro(s)" if errors else "")
    )

    if persisted:
        try:
            from app.core.interaction_access import turn_customer_hash_fragment
            await turns_repo.create({
                "id": str(uuid.uuid4()),
                "turn_number": next_turn + 1,
                "output_text_redacted": _maybe_redact(output_text),
                "interaction_id": interaction_id,
                # FIN-3: caminho declarativo não gasta LLM → tokens 0 legítimo.
                "tokens_used": 0,
                "latency_ms": float(decl.get("duration_ms") or 0),
                **turn_customer_hash_fragment(),  # LGPD-2 por-turno (35.15.0)
            })
        except Exception as e:
            logger.warning("Declarativo (via cadeia): falha ao persistir turno de saída %s: %s", interaction_id, e)

    return {
        "interaction_id": decl.get("interaction_id") or interaction_id,
        "agent_id": agent.get("id", ""),
        "output": output_text,
        "final_state": decl.get("final_state", "completed"),
        "evidence_score": 0.0,
        "transitions": [],
        "duration_ms": decl.get("duration_ms"),
        "status": "completed",
        "mode": "declarative",
        "errors": errors,
        "trace": {
            "total_steps": len(executed),
            "evidence_count": 0,
            "evidence_sources": [],
            "diagnostics": [{"level": diag_level, "text": diag_text}],
            "agent_name": agent.get("name", ""),
            "agent_kind": agent.get("kind", ""),
            "agent_model": "(declarativo)",
            "agent_provider": "declarative",
            "agent_version": agent.get("version", "1.0.0"),
            "agent_domain": agent.get("domain", ""),
            "bindings_executed": executed,
        },
    }


def _verify_autopass(
    is_pipeline_step, skip_evidence, exec_profile: str, v2_enabled: bool
) -> bool:
    """True → fase VerifyEvidence auto-passa SEM rodar verifier.

    `is_pipeline_step` é o SINAL (bool ou string) de que a interação roda dentro
    de um pipeline. O caller passa `pipeline_step or bool(pipeline_context)`:
    o flag explícito cobre o downstream cujo upstream foi fast-routed (contexto
    vazio) sem deixar de ser step de pipeline; a string do contexto cobre o
    caminho legado. `not is_pipeline_step` funciona igual p/ bool/str/None.

    Steps de pipeline (is_pipeline_step truthy) pulam o verifier — EXCETO no
    profile `rigorous` COM Verifier v2 LIGADO (decisão 2026-07-04: auditoria
    por step só onde o operador pediu rigor; cada julgamento custa +1 chamada
    LLM por step). Com v2 OFF o step de pipeline auto-passa como sempre —
    sem isso, o step rigorous cairia nos ramos LEGACY (judge sem persistência
    + risco de Refuse no meio do pipeline) pagando custo SEM gerar auditoria.
    `skip_evidence` (require_evidence off / profile fast) sempre auto-passa.
    """
    if skip_evidence:
        return True
    if not is_pipeline_step:
        return False
    return exec_profile != "rigorous" or not v2_enabled


def _apply_experiment_overrides(agent: dict,
                                config_overrides: dict | None) -> dict:
    """Seam de EXPERIMENTO (44.0.0, PR3a do arco Otimização de Prompt/Skill).

    Aplica overrides EFÊMEROS do texto livre por cima da config carregada:
    'system_prompt' (do agente) e 'skill_purpose' (## Purpose da skill).
    Allowlist por chaves fixas — seções SELADAS (Decisions/Inputs/contratos)
    nunca são aceitas (a rota valida; aqui é defesa em profundidade).

    CÓPIA DEFENSIVA do dict do agente: _topo_agent pode vir do cache de
    topologia — um experimento NÃO pode envenenar a config viva das próximas
    execuções (mesmo racional da cópia do live-resolve do task_type).
    skill_purpose sem skill (skill_data vazio) é no-op: não há seção
    ## Purpose a renderizar. Nada é persistido, nunca."""
    if not config_overrides:
        return agent
    agent = dict(agent)
    if config_overrides.get("system_prompt") is not None:
        agent["system_prompt"] = str(config_overrides["system_prompt"])
    skill_data = agent.get("_parsed_skill") or {}
    if config_overrides.get("skill_purpose") is not None and skill_data:
        skill_data = dict(skill_data)
        skill_data["purpose"] = str(config_overrides["skill_purpose"])
        agent["_parsed_skill"] = skill_data
    return agent


async def execute_interaction(
    agent_id: str,
    user_input: str,
    session_id: str = None,
    channel: str = "api",
    journey: str = "",
    attachments: list = None,
    pipeline_context: str = None,
    context_mode: str = "auto",
    grounding_strict: Optional[bool] = None,
    retrieval_query: Optional[str] = None,
    sealed_inputs: dict | None = None,
    # Auditoria (24.10.0): id do pipeline dono desta execução — vai pras
    # verifications (agregação por pipeline na página Qualidade). None fora
    # de pipeline.
    pipeline_id: str | None = None,
    # Roteamento rápido (26.0.0): sinal EXPLÍCITO de "sou step de pipeline"
    # p/ o gate do verifier. Necessário porque com fast-routing o entry é
    # pulado e o downstream recebe pipeline_context="" (vazio) — a veracidade
    # do texto deixaria de sinalizar step de pipeline e o verifier dispararia
    # indevidamente. execute_pipeline passa True nos downstream (i>0).
    pipeline_step: bool = False,
    # Postura de auditoria (26.1.0): 'inherit' (default) preserva o gate atual;
    # 'sync'/'async'/'disabled' são escolha do dono do pipeline. Ver o gate do
    # verifier abaixo. `master_interaction_id` é passado nos downstream p/ o
    # dispatch async gravar a verification DIRETO no master (evita linha órfã
    # quando a consolidação re-aponta e deleta as filhas).
    audit_posture: str = "inherit",
    master_interaction_id: str | None = None,
    # Dono na CRIAÇÃO (35.4.0): interaction nasce com owner_user_id (ver
    # execute_pipeline). Aqui cobre o invoke de agente avulso quando o caller
    # optar por passar; None = comportamento atual (stamp pós-execução).
    owner_user_id: str | None = None,
    # customer_ref (35.9.0, LGPD-2): id do cliente-final → hash na interaction
    # (pivô do esquecimento). None = sem pivô (comportamento atual).
    customer_ref: str | None = None,
    # customer_hash (35.14.2): o HASH já pronto — usado pelo worker do 202, que
    # NÃO recebe mais o ref cru (a PII não é persistida no job). Vence customer_ref.
    customer_hash: str | None = None,
    # Passo ANINHADO de pipeline (35.14.5, achado de auditoria): quando
    # execute_pipeline chama esta função para cada agente da cadeia, o dono/
    # customer_hash-na-criação JÁ foram setados por execute_pipeline no ContextVar
    # e devem FLUIR daqui pro run_intake — a filha não recebe owner/customer nos
    # args (default None). O set incondicional (35.14.4) os zerava, órfãnando
    # master+filhas (regressão do IDOR #595 e do LGPD-2 #601). Com True, a filha
    # NÃO toca o ContextVar (herda o do pai); os 4 call sites TOP-LEVEL (rotas,
    # workspace, catálogo, harness) usam False e mantêm o reset anti-herança.
    inherit_creation_context: bool = False,
    # Seam de EXPERIMENTO (44.0.0, PR3a do arco Otimização): overrides
    # EFÊMEROS do texto livre ({'system_prompt': str, 'skill_purpose': str})
    # — o harness avalia uma variante de prompt SEM mutar agente/skill vivos
    # (seria racy com produção) e sem persistir nada. None = sem efeito.
    config_overrides: dict | None = None,
) -> dict:
    """Execução completa de uma interação pela FSM §15.

    `sealed_inputs` (envelope param selado): dict de args determinísticos do caller
    (marcados `x-uso: param`), repassado ao engine declarativo SEM passar pela prosa.
    Soberano sobre o que um roteador upstream emita. None = sem envelope (legado).

    `context_mode` (2026-06-06 — memória de conversa): controla se o histórico
    da sessão é reinjetado no LLM. 'none' = stateless (legado); 'auto' (default)
    = reconstrói a janela por camada (router médio / aobd leve / subagent off).
    Só age quando há `session_id`. Ver `app.agents.conversation_memory`.

    `grounding_strict` (2026-06-06 — grounded-by-default): None (default) lê
    settings.grounding_strict; True/False força explicitamente. Quando efetivo
    e o agente não tem allow_general_knowledge, o VerifyEvidence RECUSA respostas
    sem nenhuma evidência (anexo/RAG/tool/pipeline). Ver `_grounding_guard`.
    Harness/golden e recipes pinam False para reprodutibilidade (caminho legado).

    `retrieval_query` (2026-06-06 — RAG em pipeline): query LIMPA usada na busca
    de evidências. None (default) → usa `user_input`. Em pipeline, `user_input`
    do subagente já vem prefixado com "## Contexto do agente anterior… / ##
    Solicitação original…"; passar a pergunta ORIGINAL aqui evita poluir o BM25/
    vetorial com o texto do upstream. Ver `execute_pipeline`.
    """
    start = time.time()
    # Dono/titular na CRIAÇÃO — set INCONDICIONAL (35.14.4, achado de auditoria):
    # setar SÓ quando truthy deixava um loop sequencial na MESMA task (harness/
    # evaluator, batch, A2A) herdar silenciosamente o owner/customer_hash da
    # operação ANTERIOR quando a atual os omitia. Setar sempre (None limpa) fecha
    # a herança. Os setters normalizam None → contexto limpo.
    #
    # EXCEÇÃO (35.14.5, achado de auditoria de estado-integrado): num passo
    # ANINHADO de pipeline (`inherit_creation_context=True`, só execute_pipeline
    # o passa), o ContextVar JÁ foi setado pelo pai com o dono/customer_hash reais
    # e a filha é chamada SEM esses args (None) — resetar aqui zerava master+filhas
    # (owner=NULL, customer_hash=NULL): regressão do IDOR #595 e do LGPD-2 #601.
    # Herdar (não tocar) preserva o pivô do pai; o reset anti-herança segue valendo
    # em TODA chamada top-level (rotas/workspace/catálogo/harness → False).
    if not inherit_creation_context:
        from app.core.interaction_access import (
            set_interaction_owner_for_creation, set_interaction_customer_hash_for_creation,
            set_interaction_customer_for_creation)
        set_interaction_owner_for_creation(owner_user_id)
        if customer_hash:  # 35.14.2: hash já pronto (worker do 202)
            set_interaction_customer_hash_for_creation(customer_hash)
        else:  # LGPD-2: hasheia o ref (ou limpa com None)
            set_interaction_customer_for_creation(customer_ref)
    agent = await _topo_agent(agent_id)
    if not agent:
        raise ValueError(f"Agente '{agent_id}' não encontrado.")

    # Onda 7 Wave 4: live resolve do LLM via task_type. Snapshot do save é
    # apenas valor inicial; execução real consulta routing settings AGORA
    # (mudanças em /settings → Roteamento LLM refletem imediato em todos
    # os agentes que usam o task_type alterado, sem necessidade de re-save).
    # Detecta imagem em attachments e roteia pro multimodal_fallback se
    # o modelo da task for text-only.
    if agent.get("task_type"):
        try:
            from app.llm_routing import resolve_llm_for_task, detect_image_in_attachments
            has_image = detect_image_in_attachments(attachments)
            r_provider, r_model = await resolve_llm_for_task(
                agent["task_type"], has_image=has_image
            )
            # Cópia defensiva — não mutar o dict do repositório.
            agent = dict(agent)
            agent["llm_provider"] = r_provider
            agent["model"] = r_model
            logger.info(
                f"Onda 7 routing: agent={agent_id[:8]} task={agent['task_type']} "
                f"has_image={has_image} → {r_provider}/{r_model}"
            )
        except Exception as e:
            logger.warning(
                f"resolve_llm_for_task falhou ({type(e).__name__}: {e}); "
                f"usando snapshot do agent ({agent.get('llm_provider')}/{agent.get('model')})"
            )

    # Fallback ao Modelo Primário da plataforma: se nem task_type (Onda 7) NEM
    # snapshot próprio do agent resolveram o LLM, usa platform_settings
    # primary_provider/primary_model. Cobre o caso "agent legacy criado
    # sem provider explícito" e o cenário operacional "quero que tudo use
    # gpt-oss-120b por padrão" sem editar cada agent.
    from app.core.config import get_settings as _get_settings_primary
    _cur_settings = _get_settings_primary()
    _primary_p = (_cur_settings.primary_provider or "").strip()
    _primary_m = (_cur_settings.primary_model or "").strip()
    if _primary_p and _primary_m and (not agent.get("llm_provider") or not agent.get("model")):
        agent = dict(agent)
        if not agent.get("llm_provider"):
            agent["llm_provider"] = _primary_p
        if not agent.get("model"):
            agent["model"] = _primary_m
        logger.info(
            f"Modelo Primário aplicado: agent={agent_id[:8]} → {_primary_p}/{_primary_m}"
        )

    mesh_chain = await _resolve_mesh_chain(agent_id, agent)

    skill_data = {}
    skill_raw = None
    if agent.get("skill_id"):
        skill_row = await skills_repo.find_by_id(agent["skill_id"])
        if skill_row and skill_row.get("raw_content"):
            parsed = parse_skill_md(skill_row["raw_content"])
            skill_raw = skill_row
            skill_data = {
                "purpose": parsed.purpose,
                "workflow": parsed.workflow,
                "output_contract": parsed.output_contract,
                "guardrails": parsed.guardrails,
                "evidence_policy": parsed.evidence_policy,
                "tool_bindings": parsed.tool_bindings,
                "activation_criteria": parsed.activation_criteria,
                "inputs": parsed.inputs,
                "failure_modes": parsed.failure_modes,
                "delegations": parsed.delegations,
                "budget": parsed.budget,
                "examples": parsed.examples,
                "_name": parsed.name,
                "_kind": parsed.frontmatter.kind,
                "_version": parsed.frontmatter.version,
                "_stability": parsed.frontmatter.stability,
                "_urn": parsed.frontmatter.id,
                "_execution_mode": parsed.execution_mode,
                "_api_bindings_count": len(getattr(parsed, "api_bindings_parsed", []) or []),
                # Onda 6 Wave 2: evidence_policy estruturado (sources/limits/cite_sources)
                "_evidence_policy_parsed": getattr(parsed, "evidence_policy_parsed", {}) or {},
                # Onda 1 Output Shape: length_preset + max_chars (engine usa
                # pra injetar diretiva no system_prompt + truncate hard).
                "_output_shape_parsed": getattr(parsed, "output_shape_parsed", {}) or {},
                # Contrato de Decisão (Cond-C, 35.19.0): {campo: [valores]} da
                # seção ## Decisions — o engine injeta a diretiva selada no
                # system_prompt e o gate condicional lê `decision.<campo>`.
                "_decisions_schema": extract_decisions_schema(skill_row["raw_content"]),
            }

    agent["_parsed_skill"] = skill_data

    # ── Seam de EXPERIMENTO (44.0.0, PR3a do arco Otimização) ──
    agent = _apply_experiment_overrides(agent, config_overrides)
    skill_data = agent["_parsed_skill"]

    # ── Execution Profile: determina modo de execução ──
    exec_profile = skill_data.get("_execution_mode", "standard")

    # ── Modo declarativo (Fase A 2026-06-07): SA com execution_mode=declarative
    # roda o engine de API Bindings (sem LLM) — INCLUSIVE quando alcançada por
    # um SR/AOBD via execute_pipeline (antes só funcionava no invoke direto/rota).
    # `parsed` é o skill já parseado (com api_bindings_parsed), carregado acima.
    # Fail-open: qualquer erro no dispatch cai no caminho LLM (não derruba a cadeia).
    if exec_profile == "declarative":
        try:
            return await _run_declarative_as_interaction(
                agent=agent, parsed_skill=parsed, user_input=user_input,
                session_id=session_id, sealed_inputs=sealed_inputs,
            )
        except Exception as _decl_e:
            logger.warning(
                "declarative.dispatch_failed_fallback_llm",
                extra={
                    "event": "declarative.dispatch",
                    "agent_id": agent_id,
                    "error_type": type(_decl_e).__name__,
                    "error_msg": str(_decl_e)[:200],
                },
                exc_info=True,
            )

    attachment_context = ""
    attachment_meta = []
    if attachments:
        for att in attachments:
            # 2026-06-01: enriquece metadata de cada anexo com `purpose`,
            # `routed_to`, `extracted_chars` e `category` para o card
            # "Anexos" do painel Rastreabilidade. UI lê esses campos para
            # mostrar objetivo humanizado (Análise visual / Texto extraído
            # / etc.) sem precisar inferir do tipo MIME no client.
            _type = att.get("type", "") or ""
            _content = att.get("content", "") or ""
            _extracted_chars = len(_content)
            if _type.startswith("image/"):
                _category = "image"
                # Imagem vai ao LLM como CONTEÚDO MULTIMODAL (image_url base64)
                # quando o modelo resolvido é multimodal — ver
                # _build_user_message_content. Em modelo text-only é descartada
                # (log mesh.vision.image_dropped_text_only_model). Por isso o
                # texto do markitdown ("ImageSize: LxA") NÃO entra como conteúdo
                # (puro ruído) — ver guarda em attachment_context abaixo.
                _routed_to = "vision"
                _purpose = "Análise visual (vision)"
            elif _extracted_chars > 0:
                # Texto disponível — ou foi UTF-8 puro no upload, ou
                # markitdown converteu PDF/DOCX/PPTX/XLSX pra markdown.
                # Heurística: text/* → inline; demais com content → markdown.
                if _type.startswith("text/"):
                    _category = "document"
                    _routed_to = "inline_text"
                    _purpose = "Texto inline"
                else:
                    _category = "document"
                    _routed_to = "markdown_extracted"
                    _purpose = "Texto extraído (markitdown)"
            else:
                # Sem texto e não é imagem — markitdown falhou ou tipo
                # não suportado. Anexo ainda referenciado no histórico
                # mas conteúdo não chegou ao LLM.
                _category = "document"
                _routed_to = "unprocessed"
                _purpose = "Não processado"
            attachment_meta.append({
                "name": att.get("name", ""),
                "type": _type,
                "size": att.get("size", 0),
                "category": _category,
                "purpose": _purpose,
                "routed_to": _routed_to,
                "extracted_chars": _extracted_chars,
            })
            # Imagem NÃO entra como texto (markitdown só dá "ImageSize: LxA",
            # ruído) — vai como image_url multimodal (ver
            # _build_user_message_content). Documentos seguem como texto extraído.
            if _content and _category != "image":
                attachment_context += f"\n\n## Arquivo Anexo: {att.get('name','arquivo')}\n```\n{_content[:5000]}\n```"

    ctx = InteractionContext(agent_id=agent_id, journey=journey, channel=channel)
    fsm = InteractionStateMachine(ctx)
    # Propaga session_id para FSM reusar interaction existente (2026-06-01).
    # Antes ficava só na assinatura de execute_interaction e era descartado
    # — cada call criava uma sessão nova mesmo com session_id fornecido,
    # fragmentando conversas no workspace (user reportou sidebar com várias
    # entries quando esperava uma só).
    await fsm.run_intake(user_input, agent_id, journey, channel, session_id=session_id)

    # ── Memória de conversa (2026-06-06) ───────────────────────
    # Reconstrói o histórico da sessão e o reinjeta no seed do grafo (abaixo).
    # `run_intake` JÁ persistiu o turno atual (no Intake), então excluímos
    # turnos >= ctx.next_user_turn pra não duplicar o input corrente. Janela por
    # camada (router médio / aobd leve / subagent off). Fail-open: erro → [].
    history_messages: list = []
    context_meta = {"mode": _cm_normalize(context_mode), "turns_used": 0, "chars": 0}
    if session_id and context_enabled(context_mode):
        try:
            history_messages = await _build_history_messages(
                session_id,
                agent.get("kind", "subagent"),
                context_mode,
                before_turn=fsm.ctx.next_user_turn,
            )
        except Exception as _hist_exc:  # histórico é opcional, nunca derruba
            logger.warning(
                "context.history_build_failed",
                extra={
                    "event": "context.injected",
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "error_type": type(_hist_exc).__name__,
                    "error_msg": str(_hist_exc)[:200],
                },
            )
            history_messages = []
        if history_messages:
            context_meta["turns_used"] = len(history_messages)
            context_meta["chars"] = sum(
                len(getattr(m, "content", "") or "") for m in history_messages
            )
            ctx.metadata["context_injected"] = dict(context_meta)
            logger.info(
                "context.injected",
                extra={
                    "event": "context.injected",
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "agent_kind": agent.get("kind", ""),
                    "mode": context_meta["mode"],
                    "turns_used": context_meta["turns_used"],
                    "chars": context_meta["chars"],
                },
            )

    # ── Prompt-injection guard (LLM01) ─────────────────────────
    # Aplicado ANTES do policy_check para bloquear payloads adversariais
    # antes de qualquer side-effect (retrieve, LLM, tool call). Score
    # médio (warn) só anota em metadata; score alto (block) força Refuse.
    from app.core.config import get_settings as _gs_pg
    _pg_settings = _gs_pg()
    guard_blocked = False
    guard_reason = ""
    if _pg_settings.prompt_guard_enabled:
        from app.core.prompt_guard import detect as _pg_detect
        guard_result = _pg_detect(
            user_input,
            block_threshold=_pg_settings.prompt_guard_block_threshold,
            warn_threshold=_pg_settings.prompt_guard_warn_threshold,
        )
        if guard_result.score > 0:
            ctx.metadata["prompt_guard"] = guard_result.to_dict()
        if guard_result.blocked:
            guard_blocked = True
            guard_reason = (
                f"Tentativa de prompt injection bloqueada "
                f"(score={guard_result.score:.2f}, padrões: {len(guard_result.matched_patterns)})."
            )
            await audit_repo.create({
                "entity_type": "interaction",
                "entity_id": ctx.interaction_id,
                "action": "prompt_injection_blocked",
                "details": json.dumps(guard_result.to_dict()),
            })

    # ── Onda 4a: PolicyCheck via OPA (substitui o stub de "passa se prompt_guard não bloqueou") ──
    # Quando OPA_ENABLED=true, decisão vai para o motor de políticas Rego. Quando
    # false, comportamento original (allow=true se prompt_guard não bloqueou).
    if _pg_settings.opa_enabled:
        from app.core import opa_client
        opa_input = {
            "prompt_injection": {
                "score": float(ctx.metadata.get("prompt_guard", {}).get("score", 0.0)),
            },
            "rate_limit": {"exceeded": False},  # rate_limit middleware já barra antes; defesa em profundidade
            "user": {"status": "active"},  # iteração futura: pegar user real da session
        }
        opa_decision = await opa_client.evaluate("interaction", "allow", opa_input)
        opa_reasons = await opa_client.evaluate_value("interaction", "reasons", opa_input) or []
        policy_ok_raw = opa_decision["allow"]
        if not policy_ok_raw and opa_reasons:
            guard_reason = guard_reason or f"Política de acesso negou a solicitação ({', '.join(opa_reasons)})."
        elif not policy_ok_raw:
            guard_reason = guard_reason or "Política de acesso negou a solicitação."
    else:
        policy_ok_raw = not guard_blocked

    policy_ok = await fsm.run_policy_check({"allowed": policy_ok_raw, "tools": [], "budget": {}})
    if not policy_ok:
        motivo = guard_reason or "Política de acesso negou a solicitação."
        await fsm.run_refuse(motivo, "Reformule a solicitação ou contate o administrador.")
        await fsm.run_log_and_close()
        return await _build_result(ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta, agent=agent, skill_data=skill_data)

    skip_evidence = (
        not agent.get("require_evidence", True)
        or agent.get("require_evidence") == 0
        or exec_profile == "fast"
    )
    # Onda 6 Wave 2: skill pode declarar evidence_policy.sources pra restringir
    # quais fontes essa skill consulta. None = legacy (todas autorizadas);
    # [] = bloqueia tudo; populada = filtro estrito.
    _ev_policy = (skill_data.get("_evidence_policy_parsed") or {})
    _allowed_sources = _ev_policy.get("sources")  # None | list

    # ── Fix 2026-06-06 (RAG em pipeline) ──────────────────────────────────────
    # ANTES: QUALQUER agente com pipeline_context PULAVA o retrieval e usava só o
    # TEXTO do upstream como "evidência" — então um especialista downstream ficava
    # CEGO às suas próprias bases: as KBs declaradas em evidence_policy.sources
    # NUNCA eram consultadas em pipeline. Bug relatado: "router → Retenção"
    # devolvia {tips:[], source_chunks:[]} apesar das KBs corretas e populadas.
    # AGORA: se o agente DECLARA sources (lista populada) — opt-in explícito de que
    # tem KBs próprias — ele consulta seus KBs MESMO em pipeline, somando o
    # resultado ao contexto do upstream (que já vem embutido em user_input).
    # Opt-in ESTRITO por sources populada → pipelines sem declaração ficam
    # IDÊNTICOS (zero regressão). sources=None (legado) e [] (bloqueado) seguem o
    # caminho antigo (skip / retorno vazio do retriever).
    _declares_sources = isinstance(_allowed_sources, list) and len(_allowed_sources) > 0
    _pipeline_own_rag = _pipeline_should_self_retrieve(
        has_pipeline_context=bool(pipeline_context),
        declared_sources=_declares_sources,
        skip_evidence=bool(skip_evidence),
    )

    # Footgun honesto (2026-07-17): a skill DECLARA fontes em ## Evidence Policy,
    # porém o retrieval será pulado (require_evidence off ou profile 'fast') → as
    # fontes serão silenciosamente ignoradas. Registra p/ o _build_result emitir
    # um aviso ACIONÁVEL (ver _no_evidence_diagnostic) em vez do genérico.
    if _declares_sources and skip_evidence:
        ctx.metadata["evidence_sources_ignored"] = list(_allowed_sources)

    evidences = []
    if (pipeline_context and not _pipeline_own_rag) or skip_evidence:
        await fsm.run_retrieve_evidence([])
        enriched_input = user_input if not attachment_context else f"{user_input}{attachment_context}"
    else:
        # Spans separados para retrieve e rerank — facilita identificar gargalo
        # (no Onda 3, search vai virar busca vetorial e o rerank um cross-encoder real).
        # `retrieval_query` (pipeline) = pergunta ORIGINAL limpa; fora de pipeline
        # = user_input. Evita poluir BM25/vetorial com o texto do upstream.
        _search_query = retrieval_query or user_input
        with _tracer.start_as_current_span("evidence.retrieve") as _span_r:
            _span_r.set_attribute("evidence.top_n", 5)
            _span_r.set_attribute("evidence.pipeline_own_rag", _pipeline_own_rag)
            if _allowed_sources is not None:
                _span_r.set_attribute("evidence.allowed_sources_count", len(_allowed_sources))
            evidences = await retriever.search(
                _search_query,
                top_n=5,
                allowed_source_ids=_allowed_sources,
            )
            _span_r.set_attribute("evidence.retrieved_count", len(evidences))
        with _tracer.start_as_current_span("evidence.rerank") as _span_rr:
            _span_rr.set_attribute("evidence.input_count", len(evidences))
            evidences = await reranker.rerank(_search_query, evidences, top_n=5)
            _span_rr.set_attribute("evidence.output_count", len(evidences))
        await fsm.run_retrieve_evidence([asdict(e) if hasattr(e, '__dataclass_fields__') else e for e in evidences])

        evidence_context = "\n".join(
            f"[E{i+1}] {e.snippet_text}" for i, e in enumerate(evidences)
        ) if evidences else "Nenhuma evidência encontrada nas bases autorizadas."

        enriched_input = f"{user_input}{attachment_context}\n\n## Evidências Disponíveis\n{evidence_context}"

    from app.core.config import get_settings
    settings = get_settings()
    provider = agent.get("llm_provider", "azure")
    api_key, missing_reason = _resolve_provider_config(provider, settings)

    if missing_reason:
        draft = (
            f"⚠ {missing_reason}.\n\n"
            f"Acesse Configurações → Plataforma e configure '{provider}'.\n"
            f"Modelo selecionado: {agent.get('model', '?')}"
        )
        await fsm.run_draft_answer(draft)
        await fsm.run_verify_evidence({"ok": True, "confidence": 1.0})
        await fsm.run_recommend(draft)
        await fsm.run_log_and_close()
        return await _build_result(ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta, agent=agent, skill_data=skill_data)

    mcp_tools = []
    mcp_tools_detail = []
    # Tools declaradas no SKILL.md que NÃO resolvem no Tools Registry. Antes
    # eram silenciosamente descartadas — usuário via "Sem ferramentas MCP" no
    # painel sem entender que a skill referencia tools inexistentes (ex:
    # Tavily MCP referenciado mas nome não bate com /tools). Resultado:
    # LLM alucina o output em vez de chamar a tool real. Agora a lista
    # vai pro execution_log com warning explícito.
    mcp_tools_unmatched: list[str] = []
    mcp_tools_declared_count = 0
    try:
        if skill_data.get("tool_bindings"):
            from app.mcp.runtime import parse_tool_bindings, match_with_registry
            from app.core.database import tools_repo
            parsed_bindings = parse_tool_bindings(skill_data["tool_bindings"])
            if parsed_bindings:
                mcp_tools_declared_count = len(parsed_bindings)
                enriched = await match_with_registry(parsed_bindings, tools_repo)
                # match_with_registry devolve TODAS as parsed_tools com `db_id`
                # setado quando casou no Registry. Filtra aqui pra mcp_tools
                # conter só as efetivamente invocáveis.
                mcp_tools = [t for t in enriched if t.get("db_id")]
                mcp_tools_unmatched = [t.get("name", "?") for t in enriched if not t.get("db_id")]
                mcp_tools_detail = [{"name": t.get("name",""), "server": t.get("mcp_server",""), "ops": t.get("operations",[])} for t in mcp_tools]
                logger.info(
                    "mcp_tools.resolved",
                    extra={
                        "event": "mcp.tools.resolved",
                        "declared": mcp_tools_declared_count,
                        "resolved_names": [t.get("name") for t in mcp_tools],
                        "unmatched_names": mcp_tools_unmatched,
                    },
                )

                # ── Onda 4a: gate de invocação por sensitivity ──────────
                # Quando OPA_ENABLED=true, filtra mcp_tools para apenas as que a
                # política tool_invocation aprovou. Tool com sensitivity=null assume "low".
                if _pg_settings.opa_enabled:
                    from app.core import opa_client
                    # role default = "operator" — em iteração futura virá de session real
                    user_role = "operator"
                    is_trusted = bool(ctx.metadata.get("trusted_context", False))
                    allowed_tools = []
                    for t in mcp_tools:
                        d = await opa_client.evaluate("tool_invocation", "allow", {
                            "tool": {
                                "name": t.get("name", ""),
                                "sensitivity": t.get("sensitivity") or "low",
                                "requires_trusted_context": bool(t.get("requires_trusted_context", False)),
                            },
                            "user": {"role": user_role},
                            "context": {"is_trusted": is_trusted},
                        })
                        if d["allow"]:
                            allowed_tools.append(t)
                        else:
                            logger.warning(f"Tool '{t.get('name')}' bloqueada por OPA (sensitivity={t.get('sensitivity')})")
                    if len(allowed_tools) != len(mcp_tools):
                        logger.info(f"OPA filtro: {len(mcp_tools)} → {len(allowed_tools)} tools liberadas")
                    mcp_tools = allowed_tools
                    mcp_tools_detail = [{"name": t.get("name",""), "server": t.get("mcp_server",""), "ops": t.get("operations",[])} for t in mcp_tools]

        # Execution Profile + reflexão adaptativa:
        # fast=1, standard=2 (2ª rodada apenas se heurística sinalizar), rigorous=3 idem.
        _max_iter = 1 if exec_profile == "fast" else (2 if exec_profile == "standard" else 3)
        # Onda B: passa raw markdown da SKILL pra harness — build_openai_tools
        # vai ler ## Inputs e expor schema custom pro LLM (em vez de fixed
        # {operation, query}). skill_raw é o row do skills_repo carregado em
        # ~linha 1047. Fallback "" quando SKILL ausente (back-compat).
        skill_md_for_engine = (skill_raw or {}).get("raw_content") if skill_raw else ""

        # Onda B.2: pre-descobre tool.inputSchema dos servidores MCP (em paralelo).
        # Tools sem ## Inputs explícito ganham schema real do server ao invés
        # de fallback {operation, query}. Defensivo: timeout 5s, falhas silenciosas
        # mantêm fluxo. Cacheado por endpoint em _MCP_TOOLS_LIST_CACHE — segunda
        # interação na mesma sessão é grátis.
        if mcp_tools:
            try:
                from app.mcp.runtime import pre_discover_input_schemas
                await pre_discover_input_schemas(mcp_tools, timeout=5.0)
            except Exception as _pd_exc:
                logger.warning(f"pre_discover_input_schemas falhou: {_pd_exc}")

        # ── Cadeia de resiliência LLM (modelo do agente → prioritário →
        #    fallback) ───────────────────────────────────────────────────────
        # Pedido do operador: "1) usar o modelo escolhido para o Agente; 2) se
        # não responder, chamar o modelo prioritário; 3) se não responder, usar
        # o fallback". "Não responder" = conexão/timeout/inalcançável
        # (is_llm_unreachable, compartilhado com o wizard). Erros de request/
        # config (401/404) NÃO disparam fallback: propagam pro except externo,
        # que dá mensagem acionável pro operador corrigir.
        #
        # Cada tentativa reconstrói harness+graph (o provider é fixado no
        # __init__ do harness) com um state fresco. O dedup em
        # _runtime_llm_candidates colapsa o caso comum agente==primário
        # (ex.: tudo gpt-oss-120b → [gpt-oss-120b, azure/gpt-4o]).
        _candidates = await _runtime_llm_candidates(agent, settings)
        if not _candidates:
            _candidates = [(provider, agent.get("model") or "")]
        _chosen_p, _chosen_m = _candidates[0]

        async def _run_attempt(_cand_p: str, _cand_m: str):
            # agent['llm_provider']/['model'] já foram setados por _run_llm_chain
            # antes desta chamada — o harness lê do agent. Cada tentativa
            # reconstrói harness+graph (provider é fixado no __init__) e usa
            # um state fresco.
            harness = DeepAgentHarness(
                agent, max_iterations=_max_iter, mcp_tools=mcp_tools,
                interaction_id=ctx.interaction_id, skill_md=skill_md_for_engine or "",
            )
            graph = harness.build_graph()
            state = {
                # Memória de conversa: histórico recente (já escopado por camada)
                # ANTES do turno atual. Lista vazia quando context_mode='none',
                # sem session_id ou camada off → seed byte-idêntico ao legado.
                "messages": [
                    *history_messages,
                    HumanMessage(content=_build_user_message_content(
                        enriched_input, attachments, _cand_p, _cand_m)),
                ],
                "current_agent": agent_id,
                "agent_kind": agent.get("kind", "subagent"),
                "iteration": 0,
                "max_iterations": _max_iter,
                "context": {},
                "envelope": {},
                "skill_data": skill_data,
                "metadata": {},
            }
            return await graph.ainvoke(state)

        result, _attempted = await _run_llm_chain(
            _candidates, agent, _run_attempt, agent_id
        )
        # Mantém `provider` sincronizado com o último candidato tentado (usado
        # nas mensagens de erro do except externo).
        provider = agent.get("llm_provider", provider)

        if result is None:
            # Todos os modelos da cadeia inalcançáveis — draft acionável. O
            # evento já foi logado (chain.exhausted); registramos também em
            # metadata pra observabilidade, independente do checkbox.
            ctx.metadata["llm_fallback"] = {
                "chosen_provider": _chosen_p,
                "chosen_model": _chosen_m,
                "attempted": _attempted,
                "degraded": True,
                "all_failed": True,
                "show_in_trace": await _fallback_show_in_trace(),
            }
            draft = (
                "⚠ Todos os modelos da cadeia de resiliência estão inacessíveis "
                f"agora (tentei: {', '.join(_attempted)}). Se eles rodam no hub "
                "interno (GPT-OSS), conecte-se à VPN/rede corporativa; ou ajuste "
                "o Roteamento LLM em Configurações para um provedor hospedado "
                "(ex.: Azure/OpenAI)."
            )
        else:
            draft = result["messages"][-1].content if result["messages"] else ""
            # Respondeu por contingência (candidato != o escolhido): SEMPRE
            # registra em observabilidade + LOG; show_in_trace só decide se a
            # NOTA aparece pro usuário no painel de Rastreabilidade.
            _used_p = agent.get("llm_provider", "")
            _used_m = agent.get("model", "")
            if (_used_p, _used_m) != (_chosen_p, _chosen_m):
                _show = await _fallback_show_in_trace()
                ctx.metadata["llm_fallback"] = {
                    "chosen_provider": _chosen_p,
                    "chosen_model": _chosen_m,
                    "used_provider": _used_p,
                    "used_model": _used_m,
                    "attempted": _attempted,
                    "degraded": True,
                    "show_in_trace": _show,
                }
                logger.warning(
                    "agent.llm.fallback.recovered",
                    extra={
                        "event": "agent.llm.fallback.recovered",
                        "agent_id": agent_id,
                        "chosen": f"{_chosen_p}/{_chosen_m}",
                        "used": f"{_used_p}/{_used_m}",
                        "attempts": len(_attempted),
                        "show_in_trace": _show,
                    },
                )
            ctx.metadata["tokens"] = _collect_token_usage(result, agent, agent_id)
    except Exception as llm_err:
        err_str = str(llm_err)
        # A cadeia muta agent['llm_provider'] por candidato — sincroniza o rótulo
        # das mensagens com o candidato que REALMENTE falhou (antes aparecia
        # "gpt-oss-20b/gpt-4o": provider antigo + modelo novo, confundindo o
        # troubleshooting).
        provider = agent.get("llm_provider", provider)
        if "404" in err_str or "not found" in err_str.lower():
            draft = f"⚠ Modelo '{agent.get('model', '?')}' não encontrado no provedor '{provider}'. Verifique o nome do modelo em Configurações ou edite o agente."
        elif "401" in err_str or "auth" in err_str.lower() or "invalid api key" in err_str.lower():
            draft = f"⚠ API Key do '{provider}' inválida ou expirada. Atualize em Configurações → Plataforma."
        elif "429" in err_str or "rate limit" in err_str.lower():
            draft = f"⚠ Limite de requisições atingido no '{provider}'. Aguarde alguns segundos e tente novamente."
        elif "timeout" in err_str.lower() or "timed out" in err_str.lower():
            draft = f"⚠ Timeout na chamada ao '{provider}'. O modelo demorou demais para responder."
        else:
            draft = f"⚠ Erro ao chamar LLM ({provider}/{agent.get('model','?')}): {err_str[:200]}"
        logger.warning(f"LLM error for agent {agent_id}: {err_str}")

    # Onda 1 Output Shape: truncate hard pós-LLM quando skill declara
    # length_preset. Mesmo com diretiva no system_prompt, modelos podem
    # exceder o limite — last-resort enforcement. Truncated_by_preset
    # vira sinal no execution_log + dimensão format_compliance do Verifier.
    _truncated_by_preset = False
    _preset_applied = ""
    _shape_in_skill = (skill_data or {}).get("_output_shape_parsed") or {}
    _preset_for_truncate = _shape_in_skill.get("length_preset")
    if _preset_for_truncate and draft and not draft.startswith("⚠"):
        # Pula truncate em drafts de erro (já são curtos e informativos).
        from app.skill_parser.output_shape import enforce_truncate as _enforce_truncate
        new_draft, was_truncated = _enforce_truncate(draft, _preset_for_truncate)
        if was_truncated:
            draft = _preserve_decision_line(
                original=draft,
                truncated=new_draft,
                schema=(skill_data or {}).get("_decisions_schema"),
            )
            _truncated_by_preset = True
            _preset_applied = _preset_for_truncate
            logger.warning(
                "output_shape.truncated",
                extra={
                    "event": "output_shape.truncated",
                    "agent_id": agent.get("id", ""),
                    "preset": _preset_for_truncate,
                    "original_len": len(new_draft) + 100,  # aprox.
                },
            )
    # Expõe pro Verifier/UI consultarem
    ctx.metadata["output_truncated_by_preset"] = _truncated_by_preset
    ctx.metadata["output_preset_applied"] = _preset_applied

    await fsm.run_draft_answer(draft)

    # Verificação multi-dim (Verifier v2) — capturada para retornar no result.
    # None quando nenhum verifier roda (pipeline, fast skip, fallback heurístico).
    verification = None

    # Threshold de evidência: lê min_relevance do ## Evidence Policy da skill
    # (parser já extrai pra _evidence_policy_parsed.min_relevance). Quando
    # ausente, default 0.0 desde PR #238 (antes era 0.3 — comportamento
    # histórico). Mudança a pedido do operador: filtro mínimo de qualidade
    # vira opt-in. Skills que querem filtrar evidências fracas devem
    # declarar `min_relevance: 0.3` (ou outro valor) no ## Evidence Policy.
    # Faixa válida [0..1] garantida pelo parser. Single source of truth pros
    # 3 caminhos heurísticos abaixo (production_async, v2_fallback, standard).
    _DEFAULT_MIN_RELEVANCE = 0.0
    _ev_policy_for_threshold = (skill_data.get("_evidence_policy_parsed") or {}) if skill_data else {}
    _raw_mr = _ev_policy_for_threshold.get("min_relevance")
    _min_relevance = float(_raw_mr) if isinstance(_raw_mr, (int, float)) else _DEFAULT_MIN_RELEVANCE
    _min_relevance_source = "skill" if isinstance(_raw_mr, (int, float)) else "default"
    # Exposto no result.trace pra audit/UI mostrar "Threshold: 0.15 (skill)"
    ctx.metadata["evidence_min_relevance"] = _min_relevance
    ctx.metadata["evidence_min_relevance_source"] = _min_relevance_source

    # ── Grounded-by-default (2026-06-06) — guarda anti-conhecimento paramétrico ──
    # ANTES de qualquer verificação: se strict e o agente não pode usar
    # conhecimento geral, a resposta PRECISA de fundamentação (RAG / anexo /
    # output de tool / contexto de pipeline). Sem nenhuma fonte, a resposta só
    # poderia vir do conhecimento do modelo → recusa controlada honesta em vez
    # de alucinação silenciosa. Esta guarda SOBREPÕE o auto-pass de
    # `skip_evidence` (require_evidence=0) — a porta dos fundos que deixava o
    # verifier aprovar respostas sem evidência. `grounding_strict` (param) força
    # explicitamente; None lê settings.grounding_strict.
    _grounding_strict_eff = (
        bool(getattr(settings, "grounding_strict", True))
        if grounding_strict is None else bool(grounding_strict)
    )
    _gk_allowed = bool(agent.get("allow_general_knowledge") or 0)
    # Causa 2 (2026-06-06): router é DISPATCHER — sua saída é decisão de
    # triagem/roteamento (consumida como contexto pelo especialista downstream),
    # NUNCA a resposta final fundamentada ao usuário. Logo é ISENTO da recusa de
    # grounding: senão a triagem "recusa por falta de evidência" e a recusa vira
    # o pipeline_context do especialista (lixo no prompt). subagent/aobd seguem
    # ESTRITOS — quem entrega a resposta fundamentada é o especialista. Aplicado
    # no call site (não em `_grounding_guard`, que é função pura testada) reusando
    # o mesmo caminho do escape hatch (allow_general_knowledge).
    _router_grounding_exempt = (str(agent.get("kind") or "").lower() == "router")
    _grounding_exempt = _gk_allowed or _router_grounding_exempt
    # Anexo de IMAGEM conta como grounding quando vai a um modelo MULTIMODAL
    # (a imagem É a evidência do SA de visão). Desde o #310 a imagem não entra
    # mais em attachment_context (texto) — vai como image_url —, então sem isto o
    # grounded-by-default (#301) recusaria TODO SA de imagem por "falta de
    # evidência". Só conta quando o modelo resolvido é multimodal: se cair em
    # text-only a imagem é descartada → segue recusando (correto).
    _has_image_grounding = _image_is_grounding(
        attachments, agent.get("llm_provider", ""), agent.get("model", "")
    )
    _has_attach_grounding = bool(attachment_context) or _has_image_grounding
    # Só consultamos tool grounding (query no DB) quando a guarda PODE disparar:
    # strict ligado, sem escape hatch/isenção e sem nenhuma outra fonte (RAG/
    # anexo/pipeline). Caso contrário a decisão já está selada e a query seria
    # desperdício — e superfície de erro desnecessária no caminho feliz.
    _grounding_could_refuse = (
        _grounding_strict_eff and not _grounding_exempt
        and not evidences and not _has_attach_grounding and not pipeline_context
    )
    _has_tool_grounding_flag = (
        await _has_tool_grounding(ctx.interaction_id)
        if _grounding_could_refuse else False
    )
    _refuse_ungrounded, _grounding_reason = _grounding_guard(
        strict=_grounding_strict_eff,
        allow_general_knowledge=_grounding_exempt,
        has_evidences=bool(evidences),
        has_attachments=_has_attach_grounding,
        has_pipeline_context=bool(pipeline_context),
        has_tool_output=_has_tool_grounding_flag,
        draft=draft,
    )
    ctx.metadata["grounding"] = {
        "strict": _grounding_strict_eff,
        "allow_general_knowledge": _gk_allowed,
        "router_exempt": _router_grounding_exempt,
        "refused": _refuse_ungrounded,
        "has_evidence": bool(evidences),
        "has_attachment": _has_attach_grounding,
        "has_image_grounding": _has_image_grounding,
        "has_tool_output": _has_tool_grounding_flag,
        "has_pipeline_context": bool(pipeline_context),
    }

    # Postura de auditoria (26.1.0): um step `rigorous`+v2 é auditado de forma
    # SÍNCRONA independentemente da postura do pipeline. O rigor é opt-in POR
    # AGENTE (sinal mais específico) — nem `disabled` nem `async` podem rebaixá-lo
    # (removê-lo / jogá-lo pro background), só `sync`/`inherit` o mantêm síncrono.
    _rigorous_locked = (exec_profile == "rigorous" and _pg_settings.verifier_v2_enabled)

    if _refuse_ungrounded:
        logger.warning(
            "grounding.refused",
            extra={
                "event": "grounding.refused",
                "agent_id": agent_id,
                "agent_kind": agent.get("kind"),
                "interaction_id": ctx.interaction_id,
                "has_evidence": bool(evidences),
                "has_attachment": _has_attach_grounding,
                "has_tool_output": _has_tool_grounding_flag,
                "has_pipeline_context": bool(pipeline_context),
            },
        )
        await fsm.run_verify_evidence({"ok": False, "confidence": 0.0})
    elif audit_posture == "disabled" and not _rigorous_locked:
        # Auditoria DESLIGADA por escolha do dono do pipeline → auto-passa sem
        # juiz. NÃO desliga um step `rigorous`+v2 (`_rigorous_locked`): o rigor é
        # opt-in POR AGENTE (sinal mais específico que o default do pipeline) — a
        # postura larga não pode remover silenciosamente a auditoria de crédito/
        # fraude. Rigorous cai no cascade → auditoria SÍNCRONA (segurança preservada).
        await fsm.run_verify_evidence({"ok": True, "confidence": 1.0})
    elif audit_posture == "sync" and _pg_settings.verifier_v2_enabled:
        # Auditoria SÍNCRONA por escolha do dono: roda o juiz v2 em CADA step,
        # mesmo os standard (que herdariam auto-pass). Persiste em verifications
        # (1:1 com a interaction). Paga +1 chamada LLM por step — é a escolha.
        from app.verifier import verifier as _verifier
        try:
            verification = await _verifier.verify(
                draft=draft, evidences=evidences,
                output_contract=skill_data.get("output_contract"),
                guardrails=skill_data.get("guardrails", ""),
                user_question=user_input, profile=exec_profile,
                turn_id=None, interaction_id=ctx.interaction_id,
                llm_provider_name=agent.get("llm_provider"), llm_model=agent.get("model"),
                agent_id=agent_id, pipeline_id=pipeline_id,
            )
            await fsm.run_verify_evidence({
                "ok": verification.ok, "confidence": verification.confidence,
                "risk_high": verification.risk_high,
                "fraud_suspected": verification.fraud_suspected,
                **_decision_signals(draft),
            })
        except Exception as _e:
            logger.warning(
                f"Verifier v2 (audit sync) falhou ({type(_e).__name__}: {_e}); heurística")
            verification = None
            avg_score = (sum(e.relevance_score for e in evidences) / len(evidences)) if evidences else 0.5
            await fsm.run_verify_evidence({"ok": avg_score >= _min_relevance, "confidence": avg_score})
    elif audit_posture == "async" and _pg_settings.verifier_v2_enabled and not _rigorous_locked:
        # Auditoria ASSÍNCRONA por escolha do dono: dispatch em background (não
        # bloqueia a resposta). NÃO rebaixa `rigorous`+v2 (`_rigorous_locked`): esse
        # cai no cascade → auditoria SÍNCRONA (o judge decide ANTES da resposta).
        # Grava NO MASTER (`master_interaction_id`) p/ não
        # virar linha órfã quando a consolidação re-aponta+deleta as filhas — o
        # entry (master ainda None) usa o próprio ctx.interaction_id, que VIRA o
        # master e sobrevive. FSM segue com heurística rasa (judge é pós-fato).
        from app.verifier.async_dispatcher import dispatch as _dispatch_async
        _audit_iid = master_interaction_id or ctx.interaction_id
        _dispatch_async(
            draft=draft, evidences=evidences,
            output_contract=skill_data.get("output_contract") or "",
            guardrails=skill_data.get("guardrails") or "",
            user_question=user_input, profile=exec_profile,
            interaction_id=_audit_iid,
            max_concurrent=_pg_settings.verifier_max_concurrent_jobs,
            agent_id=agent_id, pipeline_id=pipeline_id,
        )
        avg_score = (sum(e.relevance_score for e in evidences) / len(evidences)) if evidences else 0.5
        await fsm.run_verify_evidence({"ok": avg_score >= _min_relevance, "confidence": avg_score})
    elif _verify_autopass(
        (pipeline_step or bool(pipeline_context)), skip_evidence, exec_profile,
        _pg_settings.verifier_v2_enabled,
    ):
        # Steps de pipeline auto-passam SEM verifier — EXCETO rigorous com
        # v2 ON (decisão 2026-07-04: auditoria por step só onde o operador
        # pediu rigor; cada julgamento custa +1 chamada LLM por step).
        await fsm.run_verify_evidence({"ok": True, "confidence": 1.0})
    elif (
        _pg_settings.verifier_v2_enabled
        and _pg_settings.verifier_production_async
        and not (pipeline_step or bool(pipeline_context))
    ):
        # `not pipeline_context`: step rigorous de pipeline NÃO entra no
        # sampling async — o judge async persistiria DEPOIS da consolidação
        # (que re-aponta e deleta as interactions filhas) → linha órfã, e o
        # snapshot no pipeline_steps ficaria vazio. Step rigorous cai no ramo
        # SÍNCRONO abaixo, que persiste antes da consolidação.
        # ─── Production sample async (§14.2) ──
        # Não bloqueia a resposta: amostra rate% das interações para judge
        # em background. Tasks pendentes drenadas no shutdown (lifespan).
        # FSM segue com heurística rasa (avg evidence score) — judge é
        # observabilidade pós-fato, não decisão de runtime.
        # verification permanece None: result do engine não vê o judge async.
        from app.verifier.async_dispatcher import dispatch as _dispatch_async, should_sample
        if should_sample(ctx.interaction_id, _pg_settings.verifier_production_sample_rate):
            _dispatch_async(
                draft=draft,
                evidences=evidences,
                output_contract=skill_data.get("output_contract") or "",
                guardrails=skill_data.get("guardrails") or "",
                user_question=user_input,
                profile=exec_profile,
                interaction_id=ctx.interaction_id,
                max_concurrent=_pg_settings.verifier_max_concurrent_jobs,
                agent_id=agent_id,
                pipeline_id=pipeline_id,
            )
        avg_score = (sum(e.relevance_score for e in evidences) / len(evidences)) if evidences else 0.5
        await fsm.run_verify_evidence({"ok": avg_score >= _min_relevance, "confidence": avg_score})
    elif _pg_settings.verifier_v2_enabled:
        # ─── Verifier v2 (§14.2 — judge multi-dim + ContractValidator) ──
        # Roda em todos os profiles exceto fast-com-pipeline. Com ou sem evidências.
        # Persiste em `verifications` table.
        from app.verifier import verifier as _verifier
        try:
            verification = await _verifier.verify(
                draft=draft,
                evidences=evidences,
                output_contract=skill_data.get("output_contract"),
                guardrails=skill_data.get("guardrails", ""),
                user_question=user_input,
                profile=exec_profile,
                turn_id=None,  # turn é criado em LogAndClose; verifier persiste sem turn_id
                interaction_id=ctx.interaction_id,
                # Wave Contract Retry: passa o LLM ativo pra Verifier poder
                # re-chamar com instrução de correção se ContractValidator
                # marcar compliant=false. Sem isso, retry fica desabilitado.
                llm_provider_name=agent.get("llm_provider"),
                llm_model=agent.get("model"),
                # Auditoria (24.10.0): dono do julgamento.
                agent_id=agent_id,
                pipeline_id=pipeline_id,
            )
            await fsm.run_verify_evidence({
                "ok": verification.ok,
                "confidence": verification.confidence,
                "risk_high": verification.risk_high,
                "fraud_suspected": verification.fraud_suspected,
                **_decision_signals(draft),
            })
        except Exception as _e:
            logger.warning(f"Verifier v2 falhou ({type(_e).__name__}: {_e}); fallback para heurística")
            verification = None
            avg_score = (sum(e.relevance_score for e in evidences) / len(evidences)) if evidences else 0.5
            await fsm.run_verify_evidence({"ok": avg_score >= _min_relevance, "confidence": avg_score})
    elif exec_profile == "rigorous" and evidences:
        # Legacy: rigorous + evidences → EvidenceChecker monolítico (Onda 0)
        verification = await evidence_checker.verify(draft, evidences, skill_data.get("guardrails", ""))
        await fsm.run_verify_evidence({
            "ok": verification.ok,
            "confidence": verification.confidence,
            "risk_high": verification.risk_high,
            "fraud_suspected": verification.fraud_suspected,
            **_decision_signals(draft),
        })
    elif evidences:
        # Standard: verificação heurística (sem chamada LLM)
        avg_score = sum(e.relevance_score for e in evidences) / len(evidences)
        await fsm.run_verify_evidence({
            "ok": avg_score >= _min_relevance,
            "confidence": avg_score,
        })
    else:
        if skill_data.get("evidence_policy") and exec_profile == "rigorous":
            await fsm.run_verify_evidence({"ok": False, "confidence": 0.0})
        else:
            # Zero evidências RAG. O 0.8 histórico incondicional fazia o trace
            # exibir "evid 0.8" mesmo com retrieval VAZIO — mascarou o
            # diagnóstico do E2E Pulsar (Evidence Policy com UUID inexistente
            # parecia "80% fundamentado"). Agora o score diz a verdade:
            # - anexo/tool output presente → 0.8 (grounding legítimo não-RAG;
            #   o Cockpit define fundamentado como "evidence_score > 0: RAG,
            #   anexo ou tool" — zerar aqui penalizaria esses turnos);
            # - nada → 0.0 (fingerprint honesto de resposta sem fonte).
            # ok=True preserva o fluxo (Recommend) nos dois casos.
            _nonrag_grounding = _has_attach_grounding or _has_tool_grounding_flag
            await fsm.run_verify_evidence(
                {"ok": True, "confidence": 0.8 if _nonrag_grounding else 0.0}
            )

    if ctx.current_state == State.RECOMMEND:
        await fsm.run_recommend(draft)
    elif ctx.current_state == State.REFUSE:
        # Quando a recusa veio da guarda de grounding, usa o motivo acionável
        # (peça documento / habilite tool / ative conhecimento geral). Senão,
        # o motivo legado de evidência insuficiente.
        _refuse_reason = (
            _grounding_reason if _refuse_ungrounded
            else "Evidência insuficiente para recomendação segura."
        )
        await fsm.run_refuse(_refuse_reason)
    elif ctx.current_state == State.ESCALATE:
        await fsm.run_escalate("Risco alto detectado.")

    await fsm.run_log_and_close()

    trace = tracker.create_trace(
        name=f"interaction_{ctx.interaction_id}",
        metadata={"agent_id": agent_id, "kind": agent.get("kind"), "state_final": ctx.current_state.value},
    )
    if trace:
        tracker.log_generation(trace, "response", user_input, ctx.final_output, agent.get("model", "gpt-4o"))
        tracker.flush()

    return await _build_result(
        ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta,
        agent=agent, skill_data=skill_data, mcp_tools_detail=mcp_tools_detail,
        mcp_tools_declared_count=mcp_tools_declared_count,
        mcp_tools_unmatched=mcp_tools_unmatched,
        verification=verification,
    )


def _decision_signals(draft: str) -> dict:
    """#684 (Fatia F): sinais de decisão (policy_refusal/needs_escalation) para a
    FSM, atrás da flag `verifier_signals_drive_fsm`. Retorna ``{}`` quando OFF →
    a FSM cai nos defaults False (mapeamento de estado INALTERADO em produção).
    Ligar a flag faz recusa/escalonamento redigidos pelo agente virarem estado
    Refuse/Escalate (em vez de ficarem invisíveis em Recommend)."""
    from app.core.config import get_settings as _gs_ds
    if not getattr(_gs_ds(), "verifier_signals_drive_fsm", False):
        return {}
    from app.verifier.runtime import detect_decision_signals
    policy_refusal, needs_escalation = detect_decision_signals(draft)
    return {"policy_refusal": policy_refusal, "needs_escalation": needs_escalation}


def _serialize_verification(v) -> dict | None:
    """Converte VerificationResult (verifier.runtime ou evidence.runtime) em dict
    serializável. Duck-typing sobre os atributos para tolerar ambas as formas:
    a legacy só tem ok/confidence/issues/risk_high; a nova tem dimensions etc.
    Campos ausentes viram default sensato (lista vazia / dict vazio / None).
    """
    if v is None:
        return None
    return {
        "ok": bool(getattr(v, "ok", False)),
        "confidence": float(getattr(v, "confidence", 0.0) or 0.0),
        "dimensions": getattr(v, "dimensions", {}) or {},
        "unsupported_claims": list(getattr(v, "unsupported_claims", []) or []),
        "contract_compliant": bool(getattr(v, "contract_compliant", True)),
        "contract_errors": list(getattr(v, "contract_errors", []) or []),
        "judge_model": str(getattr(v, "judge_model", "") or ""),
        "duration_ms": int(getattr(v, "duration_ms", 0) or 0),
        # Custo REAL do juiz (instrumentação de TCO): o cockpit soma isto por
        # step p/ a linha "Juiz/verificador" virar MEDIDA (não estimada).
        "judge_tokens": int(getattr(v, "judge_tokens", 0) or 0),
        "judge_cost_usd": float(getattr(v, "judge_cost_usd", 0.0) or 0.0),
        "risk_high": bool(getattr(v, "risk_high", False)),
        # Wave Contract Retry: expõe na resposta HTTP pra UI mostrar
        # "✓ Verifier corrigiu a saída via retry" e operador auditar.
        "contract_retried": bool(getattr(v, "contract_retried", False)),
        "contract_original_errors": list(getattr(v, "contract_original_errors", []) or []),
    }


async def _build_result(
    ctx: InteractionContext, start_time: float,
    mesh_chain: list = None, attachments: list = None,
    agent: dict = None, skill_data: dict = None,
    mcp_tools_detail: list = None,
    mcp_tools_declared_count: int = 0,
    mcp_tools_unmatched: list = None,
    verification=None,
) -> dict:
    """Constrói resultado enriquecido com detalhes de execução.

    Async pra puxar contagens reais de invocações de tool_calls e
    binding_executions filtradas por interaction_id — as métricas
    `mcp_tools.length` e `api_tools_count` no painel de rastreabilidade
    refletem chamadas EXECUTADAS, não apenas tools/bindings declarados."""
    agent = agent or {}
    skill_data = skill_data or {}
    output = ctx.final_output or ""

    if output.startswith("{") and '"type"' in output:
        try:
            parsed = json.loads(output)
            if parsed.get("type") == "refusal":
                output = f"⚠ Recusa controlada: {parsed.get('reason', '')}\n\nPróximo passo: {parsed.get('next_step', '')}"
            elif parsed.get("type") == "escalation":
                output = f"🔺 Escalação: {parsed.get('reason', '')}"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    duration = round((time.time() - start_time) * 1000, 2)
    total_steps = len(ctx.transition_log)
    final = ctx.current_state.value
    evidence_count = len(ctx.evidences) if ctx.evidences else 0
    evidence_sources = list({e.get("source_name", e.get("snippet_text", "")[:30]) for e in ctx.evidences}) if ctx.evidences else []
    # Detalhe por chunk pra debug — user precisa ver o conteúdo recuperado
    # quando o score agrega fica baixo e a FSM cai em Refuse, pra distinguir
    # "base não cobre o tema" de "embedder mal calibrado". Limita a 500 chars
    # por snippet pra evitar inflar o trace.
    evidence_detail = [
        {
            "ordinal": i + 1,
            "score": round(float(e.get("relevance_score") or 0), 4),
            "source": e.get("source_name") or "",
            "knowledge_source_id": e.get("knowledge_source_id") or "",
            "snippet_id": e.get("snippet_id") or "",
            "text_preview": (e.get("snippet_text") or "")[:500],
            "text_full_len": len(e.get("snippet_text") or ""),
        }
        for i, e in enumerate(ctx.evidences or [])
    ]

    # Métricas de invocações reais — querya tool_calls e binding_executions
    # filtradas por interaction_id. Best-effort: erro de DB devolve listas vazias
    # (UI fica com 0 mas não quebra o response).
    #
    # api_tools_invoked: lista detalhada das execuções de bindings declarativos
    # (paridade com mcp_tools_invoked). UI usa pra drilldown no card API TOOLS.
    # Cada item tem binding_id, call_id (vincula com api_call_logs), status_code,
    # latency, attempts e flag is_compensation.
    mcp_tools_invoked: list = []
    api_tools_invoked: list = []
    api_tools_invoked_count: int = 0
    try:
        from app.core.database import tool_calls_repo, binding_executions_repo
        _tc_rows = await tool_calls_repo.find_all(interaction_id=ctx.interaction_id, limit=200)
        mcp_tools_invoked = [
            {
                "name": r.get("tool_name") or "",
                "server": r.get("mcp_server") or "",
                "status": r.get("status") or "completed",
                "latency_ms": float(r.get("latency_ms") or 0),
            }
            for r in _tc_rows
        ]
        _be_rows = await binding_executions_repo.find_all(interaction_id=ctx.interaction_id, limit=200)
        api_tools_invoked = [
            {
                "binding_id": r.get("binding_id") or "",
                "call_id": r.get("call_id") or "",
                "status_code": int(r.get("status_code") or 0),
                "latency_ms": float(r.get("latency_ms") or 0),
                "attempts": int(r.get("attempts") or 1),
                "error": (r.get("error") or "")[:200] if r.get("error") else "",
                "is_compensation": bool(r.get("is_compensation") or False),
                "skipped_by_breaker": bool(r.get("skipped_by_breaker") or False),
            }
            for r in _be_rows
        ]
        api_tools_invoked_count = len(_be_rows)
    except Exception as _metric_err:
        logger.warning(f"Falha ao carregar métricas de invocação (interaction={ctx.interaction_id}): {_metric_err}")

    diagnostics = []
    if final == "Recommend":
        diagnostics.append({"level": "success", "text": "Recomendação entregue com evidência verificada"})
    elif final == "Refuse":
        diagnostics.append({"level": "warning", "text": "Recusa controlada — evidência insuficiente ou conflito de política"})
    elif final == "Escalate":
        diagnostics.append({"level": "danger", "text": "Escalado para supervisão humana — risco alto detectado"})

    # Threshold efetivo aplicado ao verifier — vem do ctx.metadata setado
    # antes da fase Verify. Diagnóstico cita o threshold pra user entender
    # POR QUE caiu em Refuse (e poder ajustar min_relevance na skill).
    # PR #238: default 0.0 (era 0.3); alinha com _DEFAULT_MIN_RELEVANCE acima.
    _diag_min_relevance = float(ctx.metadata.get("evidence_min_relevance") or 0.0)
    _diag_threshold_source = ctx.metadata.get("evidence_min_relevance_source") or "default"
    _diag_threshold_label = f"{_diag_min_relevance:.2f} ({_diag_threshold_source})"

    if evidence_count == 0:
        diagnostics.append(
            _no_evidence_diagnostic(
                sources_ignored=ctx.metadata.get("evidence_sources_ignored")
            )
        )
    elif ctx.evidence_score < _diag_min_relevance:
        diagnostics.append({
            "level": "warning",
            "text": (
                f"Score de evidência baixo ({ctx.evidence_score:.2f}) — abaixo do "
                f"threshold {_diag_threshold_label}. Ajuste `min_relevance` em "
                f"## Evidence Policy da skill se quiser aceitar evidência mais fraca, "
                f"ou cure a base pra cobrir o tema."
            ),
        })
    elif ctx.evidence_score >= 0.7:
        diagnostics.append({"level": "success", "text": f"Evidência forte (score {ctx.evidence_score:.2f}). Boa cobertura pelas bases autorizadas."})

    if duration > 10000:
        diagnostics.append({"level": "warning", "text": f"Latência alta ({duration:.0f}ms). Considere modelo mais rápido ou reduzir max_iterations."})
    elif duration < 3000:
        diagnostics.append({"level": "success", "text": f"Resposta rápida ({duration:.0f}ms)."})

    # Onda 1 Output Shape: dimensão format_compliance — quando engine truncou
    # o draft por exceder length_preset declarado, sinaliza no diagnóstico.
    # Operador vê e ajusta (preset maior ou Workflow mais conciso na skill).
    _truncated = bool(ctx.metadata.get("output_truncated_by_preset"))
    _preset = ctx.metadata.get("output_preset_applied") or ""
    if _truncated and _preset:
        from app.skill_parser.output_shape import LENGTH_PRESETS
        cfg = LENGTH_PRESETS.get(_preset, {})
        max_chars = cfg.get("max_chars")
        label = cfg.get("label", _preset)
        diagnostics.append({
            "level": "warning",
            "text": (
                f"Format compliance: resposta excedeu o limite e foi truncada "
                f"(preset '{label}', máximo {max_chars} chars). LLM gerou texto mais "
                "longo que o declarado em ## Output Shape. Ajuste o preset pra "
                "tamanho maior, ou refine o Workflow da skill pra ser mais conciso."
            ),
        })

    verification_dict = _serialize_verification(verification)

    # ── RAGAS heurístico (Onda atual): decomposição visível do Score de
    # confiança em 4 métricas estilo RAGAS, sem chamada LLM extra. As 2
    # primeiras (context_*) sempre disponíveis; as 2 últimas (faithfulness,
    # answer_relevancy) vêm do MultiDimJudge quando rodou — em fast profile
    # ficam None com hint pra UI mostrar placeholder.
    from app.verifier.ragas_metrics import compute_heuristic_ragas
    ragas_metrics = compute_heuristic_ragas(
        evidences=ctx.evidences or [],
        verification=verification_dict,
        threshold=_diag_min_relevance,
    )

    if verification_dict:
        dims = verification_dict.get("dimensions") or {}
        fact = (dims.get("factuality") or {}).get("score")
        if verification_dict.get("ok") and isinstance(fact, (int, float)) and fact >= 4:
            diagnostics.append({"level": "success", "text": f"Verifier: factuality {fact:.1f}, ok"})
        elif not verification_dict.get("ok"):
            failed = []
            for dim_name in ("factuality", "completeness", "tone_adherence"):
                d = dims.get(dim_name) or {}
                s = d.get("score")
                if isinstance(s, (int, float)) and s < 3:
                    failed.append(f"{dim_name}={s:.1f}")
            if not verification_dict.get("contract_compliant"):
                failed.append("contract_violation")
            if failed:
                diagnostics.append({"level": "warning", "text": f"Verifier: {', '.join(failed)}"})
        if verification_dict.get("unsupported_claims"):
            n = len(verification_dict["unsupported_claims"])
            diagnostics.append({"level": "warning", "text": f"{n} claim(s) sem suporte de evidência"})

    skill_detail = {}
    if skill_data:
        skill_detail = {
            "name": skill_data.get("_name", ""),
            "urn": skill_data.get("_urn", ""),
            "kind": skill_data.get("_kind", ""),
            "version": skill_data.get("_version", ""),
            "stability": skill_data.get("_stability", ""),
            "purpose": (skill_data.get("purpose") or "")[:500],
            "activation_criteria": (skill_data.get("activation_criteria") or "")[:300],
            "workflow": (skill_data.get("workflow") or "")[:500],
            "tool_bindings": (skill_data.get("tool_bindings") or "")[:500],
            "output_contract": (skill_data.get("output_contract") or "")[:500],
            "guardrails": (skill_data.get("guardrails") or "")[:300],
            "failure_modes": (skill_data.get("failure_modes") or "")[:300],
            "evidence_policy": (skill_data.get("evidence_policy") or "")[:200],
            "delegations": (skill_data.get("delegations") or "")[:200],
            "budget": (skill_data.get("budget") or "")[:200],
            "inputs": (skill_data.get("inputs") or "")[:300],
        }
        filled = sum(1 for k in ["purpose","workflow","tool_bindings","output_contract","guardrails","failure_modes","activation_criteria","evidence_policy","inputs","delegations","budget"] if skill_data.get(k))
        skill_detail["sections_filled"] = filled
        skill_detail["execution_mode"] = skill_data.get("_execution_mode", "standard")

    system_prompt_text = agent.get("system_prompt", "")
    # LLM10 — Prompt Leak Guard: por padrão NÃO devolve o prompt cru no trace.
    # Sanitiza para hash + preview. Operador admin obtém o original direto
    # do agents_repo; traces de observabilidade não vazam o conteúdo.
    from app.core.config import get_settings as _gs_pl
    _pl = _gs_pl()
    if _pl.prompt_leak_guard_enabled and system_prompt_text:
        from app.core.prompt_guard import sanitize_for_trace
        system_prompt_summary = sanitize_for_trace(
            system_prompt_text, preview_chars=_pl.prompt_leak_preview_chars,
        )
    else:
        system_prompt_summary = system_prompt_text[:800] if system_prompt_text else ""

    exec_log = _build_execution_log(
        agent=agent, skill_data=skill_data, skill_detail=skill_detail,
        mcp_tools_detail=mcp_tools_detail or [],
        mcp_tools_declared_count=mcp_tools_declared_count,
        mcp_tools_unmatched=mcp_tools_unmatched or [],
        transitions=ctx.transition_log, evidence_count=evidence_count,
        evidence_sources=evidence_sources, evidence_score=ctx.evidence_score,
        evidence_detail=evidence_detail,
        evidence_min_relevance=_diag_min_relevance,
        evidence_min_relevance_source=_diag_threshold_source,
        duration=duration, final_state=final,
    )

    return {
        "interaction_id": ctx.interaction_id,
        "agent_id": ctx.agent_id,
        "output": output,
        "final_state": final,
        "evidence_score": ctx.evidence_score,
        "transitions": ctx.transition_log,
        "duration_ms": duration,
        "status": "completed",
        "verification": verification_dict,
        "trace": {
            "total_steps": total_steps,
            "evidence_count": evidence_count,
            "evidence_sources": evidence_sources,
            # Onda Observabilidade RAG (2026-05-27): texto + score por chunk
            # retornado pelo retriever, ordenado pela ordem original. Permite
            # UI mostrar aba "Evidências" no painel de rastreabilidade e
            # exportar pro XLSX, viabilizando triagem de "base não cobre" vs
            # "embedder mal calibrado".
            "evidence_detail": evidence_detail,
            # Threshold efetivo aplicado pelo verifier heurístico. `source`
            # diz se veio do ## Evidence Policy da skill ou default 0.3 do engine.
            "evidence_min_relevance": _diag_min_relevance,
            "evidence_min_relevance_source": _diag_threshold_source,
            # RAGAS heurístico: decomposição do Score de confiança em 4
            # métricas (context_relevancy, context_precision, faithfulness,
            # answer_relevancy). UI renderiza grid 2x2 logo abaixo do score.
            "ragas_metrics": ragas_metrics,
            "diagnostics": diagnostics,
            "journey": ctx.journey or "—",
            "channel": ctx.channel,
            "mesh_chain": mesh_chain or [],
            # `attachments` aqui é o parâmetro de _build_result — o caller
            # (execute_interaction) já passa `attachment_meta` enriquecido
            # com purpose/routed_to/category/extracted_chars (vide
            # linhas 1251 e 1307). UI do card "Anexos" lê esses campos.
            "attachments": attachments or [],
            "agent_name": agent.get("name", ""),
            "agent_kind": agent.get("kind", ""),
            "agent_model": agent.get("model", ""),
            "agent_provider": agent.get("llm_provider", ""),
            "agent_version": agent.get("version", "1.0.0"),
            "agent_domain": agent.get("domain", ""),
            "require_evidence": bool(agent.get("require_evidence", True)),
            "system_prompt": system_prompt_summary,
            "skill_detail": skill_detail,
            # mcp_tools: invocações REAIS registradas em tool_calls durante esta
            # interaction (não as tools disponíveis no registry). Métrica do painel
            # de rastreabilidade lê `.mcp_tools.length` — contagem agora reflete
            # chamadas executadas. mcp_tools_available preserva a lista declarada
            # para a aba de "Ferramenta(s) MCP vinculada(s)" no execution_log.
            "mcp_tools": mcp_tools_invoked,
            # Tools declaradas no SKILL.md que NÃO foram resolvidas no Tools
            # Registry. Quando esta lista é não-vazia, o LLM alucinou o
            # resultado pra essas tools — UI mostra warning explícito.
            "mcp_tools_unmatched": mcp_tools_unmatched or [],
            "mcp_tools_declared_count": mcp_tools_declared_count,
            "mcp_tools_available": mcp_tools_detail or [],
            # api_tools_count: contagem de binding_executions desta interaction. Em
            # modo LLM normalmente é 0 (bindings só rodam em execute_declarative);
            # mantido aqui pra simetria semântica com mcp_tools (execução real, não
            # declaração).
            "api_tools_count": api_tools_invoked_count,
            # api_tools: lista detalhada das execuções (PR #174 — drilldown).
            # Cada item tem binding_id, call_id, status_code, latency, attempts,
            # is_compensation. UI usa pra expandir o card API TOOLS com a lista.
            "api_tools": api_tools_invoked,
            # Onda 1 Output Shape (PR #175) — sinaliza format_compliance pra
            # UI/audit. True = LLM violou o length_preset declarado e engine
            # truncou. UI mostra warning no diagnóstico.
            "output_truncated_by_preset": bool(ctx.metadata.get("output_truncated_by_preset")),
            "output_preset_applied": ctx.metadata.get("output_preset_applied") or "",
            "tokens": ctx.metadata.get("tokens") or {"input": 0, "output": 0, "total": 0},
            # Cadeia de resiliência LLM (runtime fallback). Presente APENAS quando
            # houve degradação: o modelo escolhido falhou e a engine caiu pro
            # prioritário/fallback (ou todos falharam). Observabilidade é SEMPRE
            # registrada aqui + nos LOGs (event=agent.llm.fallback*). A nota
            # VISÍVEL no painel de Rastreabilidade é gateada por show_in_trace
            # (checkbox "Mostrar contingência na rastreabilidade" em /settings →
            # Multimodal Fallback). show_in_trace=False ⇒ frontend não mostra a
            # nota, mas o dado continua aqui pra auditoria via API/log.
            "llm_fallback": ctx.metadata.get("llm_fallback"),
            # Memória de conversa (2026-06-06). Presente APENAS quando o histórico
            # foi reinjetado no seed (router/aobd com session_id + turnos prévios):
            # {mode, turns_used, chars}. None p/ subagent, context_mode='none',
            # sessão nova ou sem histórico. Observabilidade p/ API/audit — espelha
            # o LOG event=context.injected.
            "context_injected": ctx.metadata.get("context_injected"),
            # Grounded-by-default (2026-06-06). Presente SEMPRE que o agente passou
            # pela guarda anti-conhecimento-paramétrico: {strict, allow_general_
            # knowledge, refused, has_evidence, has_attachment, has_tool_output,
            # has_pipeline_context}. refused=True ⇒ a resposta foi recusada por
            # não ter nenhuma fonte de fundamentação e o agente não estar
            # autorizado a usar conhecimento geral. Espelha o LOG
            # event=grounding.refused. None p/ subagent/pipeline que não chegou
            # à verificação.
            "grounding": ctx.metadata.get("grounding"),
            "execution_log": exec_log,
        },
    }


def _build_execution_log(
    agent: dict, skill_data: dict, skill_detail: dict,
    mcp_tools_detail: list, transitions: list,
    evidence_count: int, evidence_sources: list,
    evidence_score: float, duration: float, final_state: str,
    evidence_detail: list | None = None,
    # PR #238: default alinhado com _DEFAULT_MIN_RELEVANCE (era 0.3).
    evidence_min_relevance: float = 0.0,
    evidence_min_relevance_source: str = "default",
    mcp_tools_declared_count: int = 0,
    mcp_tools_unmatched: list[str] | None = None,
) -> list:
    """Constrói log de execução estruturado.

    `evidence_detail` (Onda Observabilidade RAG, 2026-05-27): lista de dicts
    com `text_preview`/`score`/`source` por chunk recuperado pelo retriever.
    Quando presente, cada chunk vira uma linha no log — permite ao operador
    ver POR QUE o score agregado ficou baixo (base não cobre o tema vs.
    embedder mal calibrado) sem precisar olhar logs estruturados crus.

    `evidence_min_relevance` + `_source`: threshold efetivo aplicado pelo
    verifier heurístico. Quando vem da skill (`source="skill"`), mostra
    no log que o valor é declarativo e auditável.
    """
    log = []

    def _add(category, icon, title, detail="", level="info"):
        log.append({"cat": category, "icon": icon, "title": title, "detail": detail, "level": level})

    kind_labels = {"aobd": "AOBD — Orquestrador", "router": "AR — Roteador", "subagent": "SA — Subagente"}
    _add("agent", "🤖", f"{agent.get('name', '?')}",
         f"{kind_labels.get(agent.get('kind',''), agent.get('kind',''))} · {agent.get('llm_provider','')}/{agent.get('model','')} · v{agent.get('version','1.0.0')}")
    # Frase de status humanizada — primeiro entry visível após o header do agente
    # ("Orquestrando seu pedido", "Escolhendo o especialista", "Pensando na sua
    # consulta"). Aparece no painel de rastreabilidade pra dar feedback narrativo
    # ao usuário sem custo de perf (apenas append no log que já estava sendo
    # construído).
    _pm = (agent.get("processing_message") or "").strip()
    if _pm:
        _add("agent", "💬", _pm[:140])
    if agent.get("domain"):
        _add("agent", "🏢", f"Domínio: {agent.get('domain')}")

    sp = agent.get("system_prompt", "")
    if sp:
        # Quando o leak guard está ativo, mostra hash + preview curto em vez
        # do conteúdo bruto — consistente com trace.system_prompt sanitizado.
        from app.core.config import get_settings as _gs_pl_log
        _pl_cfg = _gs_pl_log()
        if _pl_cfg.prompt_leak_guard_enabled:
            from app.core.prompt_guard import sanitize_for_trace
            s = sanitize_for_trace(sp, preview_chars=_pl_cfg.prompt_leak_preview_chars)
            _add("prompt", "📝", "System Prompt",
                 f"hash:{s['hash']} · {s['length']} chars · {s['preview']}")
        else:
            lines = sp.strip().split('\n')
            preview = lines[0][:120] + ("..." if len(lines[0]) > 120 or len(lines) > 1 else "")
            _add("prompt", "📝", "System Prompt", preview)

    if skill_detail.get("name"):
        _add("skill", "📋", f"SKILL.md: {skill_detail['name']}",
             f"URN: {skill_detail.get('urn','')} · v{skill_detail.get('version','')} · {skill_detail.get('stability','')}")
        _add("skill", "📊", f"{skill_detail.get('sections_filled', 0)} seções preenchidas")
        if skill_detail.get("purpose"):
            _add("skill", "🎯", "Purpose", skill_detail["purpose"][:200])
        if skill_detail.get("workflow"):
            _add("skill", "🔄", "Workflow", skill_detail["workflow"][:200])
        if skill_detail.get("output_contract"):
            _add("skill", "📤", "Output Contract", skill_detail["output_contract"][:200])
        if skill_detail.get("guardrails"):
            _add("skill", "🛡️", "Guardrails", skill_detail["guardrails"][:200])
    else:
        _add("skill", "📋", "Sem SKILL.md vinculado", "Agente opera com system prompt direto", "warning")

    # ── Execution Profile ──
    _exec_mode = skill_data.get("_execution_mode", "standard") if skill_data else "standard"
    _mode_labels = {"fast": "Fast — 1 LLM call, sem reflexão, sem evidence check", "standard": "Standard — reflexão adaptativa (somente quando necessário), evidence heurística", "rigorous": "Rigorous — reflexão adaptativa + evidence via LLM"}
    _mode_level = {"fast": "info", "standard": "info", "rigorous": "warning"}
    _add("skill", "⚡", f"Execution Profile: {_exec_mode}", _mode_labels.get(_exec_mode, ""), _mode_level.get(_exec_mode, "info"))

    # Distingue 3 cenários no log de tools MCP:
    # 1. Skill não declara nada → "Sem ferramentas MCP" (info, neutro)
    # 2. Skill declara N e TODAS resolvem → "N ferramenta(s) MCP vinculada(s)" + detalhes
    # 3. Skill declara N mas algumas/todas NÃO resolvem → warning explícito
    #    indicando alucinação iminente (LLM vai operar sem as tools que
    #    deveria usar). Antes a UI mostrava "Sem ferramentas MCP" igual ao
    #    cenário 1 — confusão crítica que esconde bug de Tools Registry.
    _unmatched = mcp_tools_unmatched or []
    if mcp_tools_detail:
        _add("tools", "🔧", f"{len(mcp_tools_detail)} ferramenta(s) MCP vinculada(s)",
             f"{len(mcp_tools_detail)} de {mcp_tools_declared_count} declarada(s) resolvem no Registry" if mcp_tools_declared_count else "")
        for t in mcp_tools_detail:
            ops = t.get("ops", [])
            if isinstance(ops, str):
                try: ops = json.loads(ops)
                except Exception: ops = [ops]
            ops_str = ", ".join(ops) if ops else "—"
            _add("tools", "⚙️", t.get("name", "?"), f"Server: {t.get('server','')} · Ops: {ops_str}")
    elif _unmatched:
        # Skill declarou tools mas NENHUMA resolveu no Registry.
        # Alucinação iminente — LLM vai operar sem ferramenta real.
        _add(
            "tools", "⚠️",
            f"{len(_unmatched)} tool(s) MCP declaradas mas NÃO resolvem no Tools Registry",
            f"Nomes: {', '.join(_unmatched[:5])}. "
            "LLM vai operar SEM essas ferramentas — risco alto de alucinação "
            "na resposta (URLs, dados, citações inventados). "
            "Confira /tools — o nome da skill precisa bater com o `name` do "
            "registro, case-insensitive. Sem fix, a resposta NÃO foi obtida via "
            "MCP real, apenas memorização do LLM.",
            "warning",
        )
    else:
        _add("tools", "🔧", "Sem ferramentas MCP", "", "info")

    # Linha extra: quando algumas resolvem e outras não, lista as órfãs
    if mcp_tools_detail and _unmatched:
        _add(
            "tools", "⚠️",
            f"{len(_unmatched)} tool(s) declarada(s) NÃO resolvem",
            f"Sem registro em /tools: {', '.join(_unmatched[:5])}. "
            "LLM pode tentar chamá-las e falhar, ou alucinar o resultado.",
            "warning",
        )

    _add("fsm", "🔀", f"FSM — {len(transitions)} transição(ões)")
    for i, t in enumerate(transitions):
        _add("fsm", "→", f"{t.get('from','')} → {t.get('to','')}",
             t.get("condition", ""), "success" if t.get("to") in ("Recommend","LogAndClose") else "info")

    if evidence_count > 0:
        # Header agregado: agora cita o threshold efetivo + se foi declarado
        # na skill (## Evidence Policy → min_relevance) ou é default do engine.
        # Score abaixo do threshold = FSM cai em Refuse — o user precisa ver o
        # número exato pra entender e poder ajustar.
        _aggregated_level = "warning" if evidence_score < evidence_min_relevance else (
            "success" if evidence_score >= 0.7 else "info"
        )
        _add("evidence", "🔍", f"{evidence_count} evidência(s) encontrada(s)",
             f"Score: {evidence_score:.2f} · Threshold: {evidence_min_relevance:.2f} "
             f"({evidence_min_relevance_source}) · Fontes: {', '.join(evidence_sources[:5])}",
             _aggregated_level)
        # Detalhe por chunk: ordenado por score desc, texto truncado a 300
        # chars no log (preview a 500 fica em evidence_detail no trace pra
        # quem precisar do conteúdo cheio via API).
        for ev in sorted(evidence_detail or [], key=lambda x: -x.get("score", 0)):
            score = ev.get("score", 0)
            # Score < threshold marca como warning pro user notar visualmente
            # qual chunk está puxando o agregado pra baixo. Threshold real
            # (não mais hardcoded 0.3) — espelha a regra do verifier.
            ev_level = "warning" if score < evidence_min_relevance else (
                "success" if score >= 0.7 else "info"
            )
            preview = (ev.get("text_preview") or "").replace("\n", " ")[:300]
            source = ev.get("source") or "?"
            _add(
                "evidence", "📄",
                f"#{ev.get('ordinal', '?')} · score {score:.2f} · {source}",
                preview + (" …" if ev.get("text_full_len", 0) > 300 else ""),
                ev_level,
            )
    else:
        _add("evidence", "🔍", "Sem evidências consultadas", "", "info")

    level = "success" if final_state in ("Recommend", "LogAndClose") else "warning" if final_state == "Refuse" else "danger"
    _add("result", "🏁", f"Resultado: {final_state}", f"Duração: {duration:.0f}ms", level)

    return log


async def _resolve_mesh_chain(agent_id: str, agent: dict) -> list:
    """Resolve a cadeia completa de agentes via AI Mesh usando BFS."""
    from app.core.database import mesh_repo

    entry = {"id": agent_id, "name": agent.get("name",""), "kind": agent.get("kind","subagent"), "model": agent.get("model",""), "role": "entry_point", "depth": 0}
    visited = {agent_id}

    upstream_chain = []
    queue_up = [agent_id]
    while queue_up:
        current = queue_up.pop(0)
        conns = await mesh_repo.find_all(target_agent_id=current, limit=20)
        for conn in conns:
            sid = conn.get("source_agent_id", "")
            if sid and sid not in visited:
                visited.add(sid)
                src = await _topo_agent(sid)
                if src:
                    upstream_chain.insert(0, {
                        "id": sid, "name": src.get("name",""), "kind": src.get("kind",""),
                        "model": src.get("model",""), "role": "upstream",
                        "connection": conn.get("connection_type","sequential"),
                    })
                    queue_up.append(sid)

    downstream_chain = []
    queue_down = [agent_id]
    while queue_down:
        current = queue_down.pop(0)
        conns = await _topo_mesh_out(current, limit=20)
        for conn in conns:
            tid = conn.get("target_agent_id", "")
            if tid and tid not in visited:
                visited.add(tid)
                tgt = await _topo_agent(tid)
                if tgt:
                    downstream_chain.append({
                        "id": tid, "name": tgt.get("name",""), "kind": tgt.get("kind",""),
                        "model": tgt.get("model",""), "role": "downstream",
                        "connection": conn.get("connection_type","sequential"),
                    })
                    queue_down.append(tid)

    chain = upstream_chain + [entry] + downstream_chain
    return chain if len(chain) > 1 else []


def _build_pipeline_trace_data(
    master_interaction_id: str, entry_agent_id: str, final_result: dict
) -> dict:
    """Monta o trace_data agregado do pipeline pra persistir no interaction
    mestre.

    Sem persistir isto, sessões de pipeline rodadas via /chat/stream (o
    caminho padrão do workspace) perdiam Rastreabilidade + Execution Log ao
    recarregar, e o toggle Agente/Pipeline caía pra 'agent' — porque o
    GET /workspace/sessions infere mode/log a partir de pipeline_steps +
    trace.execution_log, que ficavam ausentes (só o /chat sync persistia).
    As chaves abaixo espelham o que aquele endpoint e o frontend
    (loadSession/_restoreLog) esperam. `mode` é cravado em 'pipeline'.
    """
    return {
        "interaction_id": master_interaction_id,
        "agent_id": entry_agent_id,
        "final_state": final_result.get("final_state"),
        "evidence_score": final_result.get("evidence_score", 0),
        "transitions": final_result.get("transitions", []),
        "duration_ms": final_result.get("duration_ms", 0),
        "trace": final_result.get("trace", {}),
        "pipeline_steps": final_result.get("pipeline_steps", []),
        # QUEM respondeu (Pacote B, 35.x) — persistido para o reload da sessão
        # mostrar a autoria; None em envelopes pré-35.0.0 (aditivo, inofensivo).
        "output_agent": final_result.get("output_agent"),
        "mode": "pipeline",
    }


def _step_cost_and_tokens(result: dict, agent: dict) -> tuple:
    """(cost_usd, tokens_used) de um result do execute_interaction (PR7 cost auto-wire).

    Os tokens vivem em result['trace']['tokens'] (input/output/total) e o provider/
    model EFETIVAMENTE usados em result['trace']['agent_provider'|'agent_model'] — é
    o MESMO desempacotamento que o executor de recipe faz (_invoke_step). Usar
    trace['agent_model'] (não agent['model']) reflete o modelo de fato usado (o
    engine pode rerotear por task_type numa cópia local do agent). Defensivo: trace
    ausente / preço desconhecido → (0.0, 0).
    """
    trace = result.get("trace") or {}
    tok = trace.get("tokens") or {}
    # Custo/billing usam o input SOMADO entre as chamadas LLM (input_billed_sum):
    # em turnos multi-chamada (reflexão/tool-loop) o provider cobra o prompt a CADA
    # chamada — usar só o input da última SUBCONTAVA. tok['input'] (última chamada)
    # segue p/ display. Fallback a 'input' cobre traces antigos/single-call (billed
    # == last). Idem tokens_used: total_billed (soma real) quando existir.
    ti = int(tok.get("input_billed_sum") or tok.get("input") or 0)
    to = int(tok.get("output") or 0)
    try:
        from app.core.llm_pricing import compute_cost
        cost = compute_cost(
            trace.get("agent_provider") or (agent or {}).get("llm_provider"),
            trace.get("agent_model") or (agent or {}).get("model"),
            ti, to,
        )
    except Exception:
        cost = 0.0
    return float(cost or 0.0), (int(tok.get("total_billed") or tok.get("total") or 0) or (ti + to))


def _step_effective_model(result: dict, agent: dict) -> str:
    """Modelo REALMENTE usado no step, para o card de Rastreabilidade.

    O engine pode rerotear por ``task_type`` numa cópia local do agent (ex.: input
    multimodal força ``multimodal_fallback`` → azure/gpt-4o em runtime). O snapshot
    ``agent['model']`` é só o valor PRÉ-swap; o modelo de fato usado vive em
    ``result['trace']['agent_model']`` — mesmo princípio do `_step_cost_and_tokens`.
    Sem isto, um SA de visão que rodou em gpt-4o aparecia como "gpt-oss" no trace,
    mascarando o roteamento multimodal (confundiu o diagnóstico do bug de imagem)."""
    return (
        ((result or {}).get("trace") or {}).get("agent_model")
        or (agent or {}).get("model", "")
        or ""
    )


async def execute_pipeline(
    entry_agent_id: str,
    user_input: str,
    channel: str = "api",
    attachments: list = None,
    progress_callback=None,
    session_id: str | None = None,
    context_mode: str = "auto",
    allowed_agent_ids: set | None = None,
    sealed_inputs: dict | None = None,
    # Auditoria (24.10.0): id do pipeline (tabela pipelines) — propagado a
    # cada step p/ as verifications. None em execuções fora do invoke selado.
    pipeline_id: str | None = None,
    # Harness modo pipeline (Pacote C): paridade com execute_interaction —
    # None (default) lê settings.grounding_strict em cada step; o harness pina
    # False p/ reprodutibilidade (mesma razão do modo agente: golden datasets
    # calibrados antes da guarda anti-conhecimento-paramétrico).
    grounding_strict: bool | None = None,
    # Dono na CRIAÇÃO (35.4.0): quando informado, TODA interaction criada nesta
    # execução (master + filhas) nasce com owner_user_id — um aborto server-side
    # (timeout do invoke-job, crash) não deixa mais conversa órfã sem dono
    # (IDOR). Flui por ContextVar até os pontos de criação; o stamp pós-execução
    # das rotas vira rede de segurança (idempotente, WHERE owner IS NULL).
    owner_user_id: str | None = None,
    # customer_ref (35.9.0, LGPD-2): id do cliente-final → hash na interaction
    # (master + filhas), pivô do direito ao esquecimento. None = sem pivô.
    customer_ref: str | None = None,
    # customer_hash (35.14.2): hash já pronto (worker do 202). Vence customer_ref.
    customer_hash: str | None = None,
) -> dict:
    """Executa pipeline completo pelo AI Mesh.

    `sealed_inputs` (envelope param selado): args determinísticos do caller
    (`x-uso: param`) que acompanham TODA a cadeia fora da prosa e chegam intactos
    a qualquer agente declarativo (entry ou downstream), soberanos sobre o que um
    roteador LLM emita. None = sem envelope (comportamento legado).

    `allowed_agent_ids` (Trilha A / PR-A1, opt-in): SELA a execução ao subgrafo
    do pipeline — a cadeia só inclui agentes do conjunto (membros). None = BFS
    global (histórico). Usado pela execução de pipeline do Catálogo (snapshot) e,
    futuramente, pelo invoke-por-pipeline.

    MELHORIA: Short-circuit para agentes pass-through.
    Agentes sem SKILL.md e com prompt genérico são ignorados (0ms),
    propagando o input diretamente ao próximo agente da cadeia.

    progress_callback: opcional, `async def cb(event: dict) -> None`. Quando
    presente, é chamado em pontos-chave (pipeline_start, agent_start,
    agent_passthrough, agent_done) pra streaming via SSE. Erro no callback
    é absorvido — não afeta a execução do pipeline.
    """
    start = time.time()
    # Dono/titular na CRIAÇÃO — set INCONDICIONAL (35.14.4): vale para master E
    # filhas; setar sempre (None limpa) impede a herança entre operações da
    # MESMA task (loop do harness/batch). Ver execute_interaction.
    from app.core.interaction_access import (
        set_interaction_owner_for_creation, set_interaction_customer_hash_for_creation,
        set_interaction_customer_for_creation)
    set_interaction_owner_for_creation(owner_user_id)
    if customer_hash:  # 35.14.2: hash já pronto (worker do 202)
        set_interaction_customer_hash_for_creation(customer_hash)
    else:
        set_interaction_customer_for_creation(customer_ref)
    # Cache de topologia por requisição (25.2.0): liga o memo de mesh/agents
    # do caminho quente quando o toggle permite. contextvar é request-scoped
    # (sem vazar entre requisições concorrentes); resetado antes do return.
    from app.core.database import _topology_cache_on
    _topo_token = None
    if _topology_cache_on():
        _topo_token = _pipeline_topo.set({"mesh": {}, "agents": {}})
    def _clear_topo():
        # Higiene do contextvar: reseta em TODO caminho de saída (raises do
        # setup + return final). O loop de steps captura erros de step
        # internamente (não propaga), então estes + o return cobrem tudo.
        if _topo_token is not None:
            _pipeline_topo.reset(_topo_token)

    entry_agent = await _topo_agent(entry_agent_id)
    if not entry_agent:
        _clear_topo()
        raise ValueError(f"Agente '{entry_agent_id}' não encontrado.")

    # PR2 — gate de runtime por status do pipeline (decisão travada 2026-06-12).
    # SÓ 'aposentado' bloqueia, e SÓ na ENTRADA: um pipeline aposentado não é
    # roteável. 'rascunho'/'publicado' NÃO afetam o runtime aqui (a distinção
    # ganha sentido na Parte B / Catálogo). A cadeia downstream roda normal — não
    # pulamos membros no meio da cadeia. Zero regressão p/ grupos migrados no PR1
    # (que viraram pipelines 'rascunho'). FAIL-OPEN: se a resolução do pipeline
    # falhar (ex.: pool indisponível), NÃO bloqueia — disponibilidade > enforcement.
    from app.core.database import pipeline_membership, pipelines_repo
    entry_pipeline_id = None
    entry_pipeline = None
    try:
        entry_pipeline_id = await pipeline_membership.pipeline_of(entry_agent_id)
        if entry_pipeline_id:
            entry_pipeline = await pipelines_repo.find_by_id(entry_pipeline_id)
    except Exception as gate_err:
        logger.warning(
            "pipeline.gate.lookup_failed",
            extra={
                "event": "pipeline.gate.lookup_failed",
                "entry_agent_id": entry_agent_id,
                "error": str(gate_err)[:200],
            },
        )
        entry_pipeline = None
    if entry_pipeline and entry_pipeline.get("status") == "aposentado":
        try:
            await audit_repo.create({
                "entity_type": "pipeline",
                "entity_id": entry_pipeline_id,
                "action": "execution_blocked",
                "details": json.dumps(
                    {"entry_agent_id": entry_agent_id, "reason": "pipeline_aposentado", "channel": channel},
                    ensure_ascii=False,
                ),
            })
        except Exception:
            pass  # auditoria não impede o bloqueio
        _clear_topo()
        raise ValueError(
            f"Pipeline '{entry_pipeline.get('name')}' está aposentado — não é roteável. "
            f"Reative-o (aposentado→publicado) para voltar a executar."
        )

    # Auditoria (24.10.0): sem pipeline_id explícito do caller (invoke selado
    # passa o dele), reusa o resolvido pelo gate acima — atribuição
    # best-effort via membership exclusiva do entry. Cobre workspace/agents/
    # catálogo sem cada caller precisar plumbar o id.
    #
    # Quando o id foi INFERIDO, um run de mesh LIVRE (não-selado) pode
    # percorrer agentes FORA do pipeline do entry — esses steps não podem
    # ser atribuídos ao pipeline. Carrega o conjunto de membros UMA vez e
    # filtra por step. Caller explícito (invoke selado) já vem com
    # allowed_agent_ids restringindo a cadeia → sem filtro extra.
    _pipeline_members: set | None = None
    if pipeline_id is None:
        pipeline_id = entry_pipeline_id
        if pipeline_id:
            try:
                _pipeline_members = set(
                    await pipeline_membership.agents_of(pipeline_id)
                )
            except Exception:
                _pipeline_members = None

    # Roteamento rápido (26.0.0): ativo só se o MASTER global permitir E o
    # pipeline optou (coluna fast_routing). Elegibilidade estática das arestas
    # é checada no loop (i==0) — fail-safe cai no LLM quando ambígua.
    _fast_routing_active = False
    try:
        from app.core.config import get_settings as _gs_fr
        if _gs_fr().fast_routing_enabled and entry_pipeline and int(
            entry_pipeline.get("fast_routing") or 0
        ):
            _fast_routing_active = True
    except Exception:
        _fast_routing_active = False

    # Postura de auditoria por pipeline (26.1.0): inherit|sync|async|disabled.
    # Resolvida 1× do pipeline dono; passada a cada step (execute_interaction).
    # Fora de pipeline selado (entry_pipeline None) → 'inherit' (comportamento atual).
    _audit_posture = "inherit"
    try:
        _ap = (entry_pipeline or {}).get("audit_posture")
        if _ap in ("inherit", "sync", "async", "disabled"):
            _audit_posture = _ap
    except Exception:
        _audit_posture = "inherit"

    chain, parent_of = await _resolve_ordered_chain_with_parents(entry_agent_id, allowed_agent_ids)
    if not chain:
        chain = [entry_agent_id]
        parent_of = {}

    async def _emit(event: dict) -> None:
        if progress_callback is None:
            return
        try:
            await progress_callback(event)
        except Exception as cb_err:
            logger.warning(f"progress_callback erro (ignored): {cb_err}")

    # Pré-resolve os nomes da chain pro evento pipeline_start (não bloqueia)
    _chain_meta = []
    for _aid in chain:
        try:
            _a = await _topo_agent(_aid)
            if _a:
                _chain_meta.append({"id": _aid, "name": _a.get("name", ""), "kind": _a.get("kind", "")})
        except Exception:
            pass
    await _emit({"type": "pipeline_start", "total_agents": len(chain), "chain": _chain_meta})

    steps = []
    current_input = user_input
    # Sinal pegajoso da sessão (2026-06-06): texto das perguntas recentes do
    # usuário. Misturado em `text_all` no gate condicional pra casar follow-ups
    # por keyword ("liste os pontos" no t1 → "sobre qual o tema" no t2). Lido
    # AGORA (antes do loop): `run_intake` ainda não persistiu o turno atual,
    # então isto traz só turnos ANTERIORES. '' quando context_mode='none'.
    session_text = ""
    if session_id and context_enabled(context_mode):
        try:
            session_text = await _session_text_window(session_id, context_mode)
        except Exception:
            session_text = ""  # fail-open: sinal pegajoso é melhoria, não requisito
    last_result = None
    # Âncora do envelope (Pacote B, 35.0.0): referência ao ENTRY de steps do
    # agente que produziu o last_result vigente — decisão/transições/evidência
    # do envelope saem DELE (nunca de steps[-1], que pode ser um step pulado).
    owner_step = None
    master_interaction_id = None
    child_interaction_ids = []
    # Tracking por-id pro roteamento contra o PARENT REAL (fan-out / branch).
    # outputs_by_id[id]/final_states_by_id[id]/names_by_id[id] guardam o
    # resultado de cada agente `completed`. Usados quando o parent de um nó
    # NÃO é o irmão anterior na lista BFS (fan-out 1→N): aí o gate/scope/input
    # avaliam contra o output do PARENT, não do anterior. Cadeia linear não
    # usa esses mapas (parent == anterior) → comportamento byte-idêntico.
    outputs_by_id: dict = {}
    final_states_by_id: dict = {}
    names_by_id: dict = {}

    # ══════════════════════════════════════════════════════
    # Semântica multi-inbound (29.1.13 — achados N1 "Hélios" + N2 "Arca"):
    # um nó downstream roda se PELO MENOS UMA aresta inbound disparou.
    #   • sequential/parallel de source EXECUTADA → dispara sempre;
    #   • conditional → dispara se a expr/override casar (gate existente);
    #   • default → dispara se nenhum irmão condicional casou;
    #   • aresta de source PULADA nunca dispara.
    # Antes, só a aresta do parent BFS (primeiro-marca-vence) era avaliada:
    #   N1: nó com inbound misto (conditional não-casada + sequential de nó
    #       executado) ficava skipped_conditional — a cadeia não o reativava.
    #   N2: nó cujo único inbound é sequential de nó PULADO caía no fallback
    #       linear e RODAVA com o output de um nó não conectado a ele; sendo
    #       o último da ordem topológica, o output dele sobrepunha a resposta
    #       correta do especialista roteado.
    # `inbound_by_target`: arestas entre membros da chain (ordenadas pela
    # posição da source, pois iteramos a chain em ordem). `skipped_ids`:
    # nós pulados por gate (conditional/default/upstream) — passthrough,
    # fast_routed e inativos ficam FORA (são "transparentes", como antes).
    chain_pos = {aid: idx for idx, aid in enumerate(chain)}
    inbound_by_target: dict = {}
    if len(chain) > 1:
        for _src in chain:
            try:
                for _conn in await _topo_mesh_out(_src, limit=20):
                    _tgt = _conn.get("target_agent_id", "")
                    if _tgt in chain_pos and _tgt != _src:
                        inbound_by_target.setdefault(_tgt, []).append(_conn)
            except Exception:
                pass  # fail-open: sem o mapa, o nó cai no fluxo histórico
    skipped_ids: set = set()
    # Sources "transparentes" (passthrough / inativo): não produzem output mas
    # a cadeia delas está VIVA — não bloqueiam nem reativam; o alvo cai no
    # fallback linear histórico (fail-open), nunca em skipped_upstream.
    transparent_ids: set = set()
    reactivated_edge_by_id: dict = {}

    async def _alt_inbound_upstream(
        target_id: str, target_agent: dict, pos: int, exclude: str | None
    ) -> tuple:
        """Procura, entre as DEMAIS arestas inbound de `target_id` (sources já
        processadas na chain), uma que DISPARE. Devolve `(source_id, fired)`:
        primeira aresta que dispara em ordem de chain; se nenhuma dispara,
        `(primeira source executada viva, False)` — pro gate normal marcar o
        skip com a razão certa; `(None, False)` se não há source viva (todas
        as inbound processadas foram puladas → candidato a skipped_upstream).
        Sources transparentes (passthrough, sem output) não reativam."""
        first_live = None
        for conn in inbound_by_target.get(target_id, []):
            src = conn.get("source_agent_id", "")
            if not src or src == exclude:
                continue
            if chain_pos.get(src, 1 << 30) >= pos:
                continue  # source ainda não processada (cross-edge) não conta
            if src in skipped_ids or src not in outputs_by_id:
                continue
            if first_live is None:
                first_live = src
            ctype = conn.get("connection_type", "sequential")
            if ctype in ("sequential", "parallel"):
                return src, True
            if ctype == "conditional":
                if not await _should_skip_conditional(
                    source_id=src,
                    target_id=target_id,
                    last_output=outputs_by_id[src],
                    last_final_state=final_states_by_id.get(src, ""),
                    user_input=user_input,
                    target_name=target_agent.get("name", ""),
                    attachments=attachments,
                    session_text=session_text,
                    target_accepts_documents=bool(target_agent.get("accepts_documents") or 0),
                    target_accepts_images=bool(target_agent.get("accepts_images") or 0),
                    inputs=sealed_inputs,
                ):
                    return src, True
            elif ctype == "default":
                if not await _should_skip_default(
                    source_id=src,
                    target_id=target_id,
                    last_output=outputs_by_id[src],
                    last_final_state=final_states_by_id.get(src, ""),
                    user_input=user_input,
                    attachments=attachments,
                    session_text=session_text,
                    inputs=sealed_inputs,
                ):
                    return src, True
        return first_live, False

    # ── N1b (29.1.14): decisão de skip sem dependência da ordem BFS ──
    # A chain segue a ordem de descoberta da BFS, que segue a ordem de
    # retorno de `_topo_mesh_out` (arestas mais recentes primeiro) — ou
    # seja, a ORDEM DE CRIAÇÃO das conexões. Um alvo preterido que aparece
    # ANTES da sua source de cadeia (cross-edge pra frente, só possível
    # entre nós do mesmo nível BFS) era decidido cedo demais:
    # `_alt_inbound_upstream` só enxerga sources já processadas e a
    # reativação (29.1.13) nunca tinha chance — deletar e recriar a MESMA
    # aresta mudava o resultado do pipeline. Em vez de marcar o skip, a
    # decisão é ADIADA: o nó vai UMA vez pro fim da fila de trabalho e é
    # reavaliado quando as sources inbound já têm status. Máx. 1 requeue
    # por nó ⇒ termina sempre (ciclos degradam pro comportamento antigo,
    # fail-open). `chain` fica intacta (total_agents/contagens não inflam).
    work_queue = list(chain)
    requeued_ids: set = set()

    def _undecided_inbound_srcs(target_id: str) -> list:
        """Sources inbound do alvo (membros da chain) ainda SEM decisão
        nesta execução — nem executaram, nem foram puladas, nem são
        transparentes (passthrough/inativo)."""
        pend = []
        for c in inbound_by_target.get(target_id, []):
            src = c.get("source_agent_id", "")
            if not src or src == target_id:
                continue
            if src in outputs_by_id or src in skipped_ids or src in transparent_ids:
                continue
            pend.append(src)
        return pend

    def _defer(target_id: str, target_agent: dict, at_index: int, context: str) -> bool:
        """Adia a decisão de skip do nó pro fim da fila (UMA única vez).
        True = adiado (caller dá `continue`); False = decide agora."""
        if target_id in requeued_ids:
            return False
        pend = _undecided_inbound_srcs(target_id)
        if not pend:
            return False
        requeued_ids.add(target_id)
        work_queue.append(target_id)
        chain_pos[target_id] = len(work_queue) - 1
        logger.info(
            "mesh.chain.deferred",
            extra={
                "event": "mesh.chain.deferred",
                "agent_id": target_id,
                "agent_name": target_agent.get("name", ""),
                "undecided_sources": pend,
                "at_index": at_index,
                "context": context,
            },
        )
        return True

    for i, agent_id in enumerate(work_queue):
        agent = await _topo_agent(agent_id)
        if not agent or agent.get("status") != "active":
            transparent_ids.add(agent_id)
            steps.append({"agent_id": agent_id, "agent_name": agent.get("name","?") if agent else "?", "status": "skipped", "reason": "inativo"})
            await _emit({
                "type": "agent_skipped",
                "step_index": i,
                "agent_id": agent_id,
                "agent_name": agent.get("name", "?") if agent else "?",
                "reason": "inativo",
            })
            continue

        # ══════════════════════════════════════════════════════
        # ROTEAMENTO RÁPIDO (26.0.0): pula a chamada LLM do ENTRY (router)
        # quando o pipeline optou E as arestas de saída são 100%
        # determinísticas (só args selados + pergunta). A rota downstream
        # fica IDÊNTICA (as arestas não leem o output do router); o
        # especialista recebe user_input + args em vez da prosa do router.
        # Fail-safe: _entry_fast_routable=False ⇒ roda o LLM normalmente.
        # ══════════════════════════════════════════════════════
        if (
            i == 0
            and _fast_routing_active
            and len(chain) > 1
            and await _entry_fast_routable(agent_id, entry_agent=agent)
        ):
            logger.info(
                "pipeline.fast_routing",
                extra={"event": "pipeline.fast_routing", "entry_agent_id": agent_id,
                       "pipeline_id": pipeline_id},
            )
            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "agent_kind": agent.get("kind", ""),
                "agent_model": agent.get("model", ""),
                "status_message": (agent.get("processing_message") or "").strip(),
                "status": "fast_routed",
                "output": "",
                "final_state": "FastRouted",
                "duration_ms": 0,
                "evidence_score": 0,
                "transitions": [],
                "trace": {
                    "total_steps": 0, "evidence_count": 0, "evidence_sources": [],
                    "diagnostics": [{"level": "info", "text": (
                        f"Roteamento rápido: {agent.get('name','')} (router) pulado — "
                        "rota decidida pelos args + pergunta, sem chamada LLM (0ms).")}],
                    "agent_name": agent.get("name", ""),
                    "agent_kind": agent.get("kind", ""),
                    "agent_model": agent.get("model", ""),
                    "execution_log": [{"cat": "agent", "icon": "⚡",
                        "title": f"Roteamento rápido: {agent.get('name','')}",
                        "detail": "Arestas determinísticas → router pulado (0ms).",
                        "level": "info"}],
                },
            })
            # Propaga como se o router tivesse output vazio: downstream roteia
            # pelos args+pergunta (have_upstream=True, output=""). Set em AMBOS
            # os caminhos de parent-resolution (linear via last_result;
            # fan-out via outputs_by_id).
            outputs_by_id[agent_id] = ""
            final_states_by_id[agent_id] = "FastRouted"
            names_by_id[agent_id] = agent.get("name", "")
            last_result = {"output": "", "final_state": "FastRouted",
                           "interaction_id": None, "agent_id": agent_id}
            owner_step = steps[-1]  # âncora acompanha o last_result (Pacote B)
            await _emit({"type": "agent_fast_routed", "step_index": i,
                         "agent_id": agent_id, "agent_name": agent.get("name", "")})
            continue

        # ══════════════════════════════════════════════════════
        # SHORT-CIRCUIT: pass-through para agentes sem skill
        # e com prompt genérico. Pula a chamada LLM inteira.
        # O input é propagado inalterado ao próximo agente.
        # ══════════════════════════════════════════════════════
        if _is_passthrough(agent) and len(chain) > 1 and i < len(chain) - 1:
            logger.info(
                f"Pipeline short-circuit: agent '{agent.get('name','')}' "
                f"(kind={agent.get('kind','')}) is pass-through — skipping LLM"
            )
            await _emit({
                "type": "agent_passthrough",
                "step_index": i,
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "agent_kind": agent.get("kind", ""),
                "processing_message": (agent.get("processing_message") or "").strip(),
            })
            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "agent_kind": agent.get("kind", ""),
                "agent_model": agent.get("model", ""),
                "status_message": (agent.get("processing_message") or "").strip(),
                "status": "passthrough",
                "output": "",
                "final_state": "PassThrough",
                "duration_ms": 0,
                "evidence_score": 0,
                "transitions": [],
                "trace": {
                    "total_steps": 0,
                    "evidence_count": 0,
                    "evidence_sources": [],
                    "diagnostics": [{"level": "info", "text": f"Short-circuit: {agent.get('name','')} sem SKILL.md e prompt genérico — input propagado ao próximo agente (0ms)"}],
                    "agent_name": agent.get("name", ""),
                    "agent_kind": agent.get("kind", ""),
                    "agent_model": agent.get("model", ""),
                    "agent_provider": agent.get("llm_provider", ""),
                    "execution_log": (
                        [
                            {"cat": "agent", "icon": "⚡", "title": f"Pass-through: {agent.get('name','')}", "detail": f"Sem SKILL.md + prompt genérico → short-circuit (0ms). Kind: {agent.get('kind','')}.", "level": "info"},
                        ]
                        + (
                            [{"cat": "agent", "icon": "💬", "title": (agent.get("processing_message") or "").strip()[:140], "detail": "", "level": "info"}]
                            if (agent.get("processing_message") or "").strip()
                            else []
                        )
                    ),
                },
            })
            # current_input e last_result permanecem inalterados
            # O próximo agente receberá o input original
            transparent_ids.add(agent_id)
            continue

        # ══════════════════════════════════════════════════════
        # UPSTREAM REAL (parent) — 2026-06-05
        # Resolve contra QUEM este nó avalia gate/scope/input:
        # - Cadeia linear: o parent É o anterior na lista BFS (chain[i-1]).
        #   Usa `last_result` → byte-idêntico ao comportamento histórico.
        # - Fan-out (1 source → N filhos): o parent REAL (parent_of[agent_id])
        #   difere do irmão anterior na lista. Avaliamos contra o output DO
        #   PARENT (outputs_by_id) — é isso que habilita roteamento 1-de-N.
        # Fallback p/ linear quando o parent não produziu output
        # (passthrough/inativo): fail-safe, nunca pior que o antigo.
        # ══════════════════════════════════════════════════════
        parent_id = parent_of.get(agent_id)

        # ── Propagação de skip pela cadeia (29.1.13 — achado N2 "Arca") ──
        # Parent BFS foi PULADO → este nó NÃO pode cair no fallback linear
        # (que o fazia rodar com o output do último executado, um nó NÃO
        # conectado a ele — e o output dele virava a resposta final do
        # pipeline). Antes de pular junto, procura OUTRA aresta inbound viva:
        # se alguma source executada alcança este nó, ela vira o upstream e
        # os gates normais decidem (achado N1: cadeia válida reativa o nó).
        _upstream_forced = False
        _skip_propagates = False
        if parent_id is not None and i > 0 and parent_id in skipped_ids:
            _alt_src, _alt_fired = await _alt_inbound_upstream(
                agent_id, agent, i, exclude=parent_id
            )
            # N1b: nenhuma alternativa disparou AINDA, mas há source inbound
            # sem decisão (cross-edge pra frente) — adiar em vez de pular.
            if not _alt_fired and _defer(agent_id, agent, i, "upstream_skipped"):
                continue
            if _alt_src is None:
                # Nenhuma source EXECUTADA viva — mas se alguma inbound vem de
                # source transparente (passthrough/inativo), a cadeia está viva:
                # fail-open pro fallback linear histórico (nó roda como antes).
                _skip_propagates = not any(
                    c.get("source_agent_id") in transparent_ids
                    and chain_pos.get(c.get("source_agent_id", ""), 1 << 30) < i
                    for c in inbound_by_target.get(agent_id, [])
                )
            if _alt_src is None and _skip_propagates:
                _agent_nm = agent.get("name", "")
                _parent_nm = names_by_id.get(parent_id, "") or "o nó anterior"
                skip_text = (
                    f"Upstream pulado — «{_parent_nm}» não rodou e nenhuma outra "
                    f"entrada viva dispara «{_agent_nm}»; o skip propaga pela "
                    f"cadeia (o nó não roda nem sobrescreve a resposta final)"
                )
                skipped_ids.add(agent_id)
                names_by_id[agent_id] = _agent_nm
                logger.info(
                    "mesh.chain.skip_propagated",
                    extra={
                        "event": "mesh.chain.skip_propagated",
                        "agent_id": agent_id,
                        "agent_name": _agent_nm,
                        "skipped_parent_id": parent_id,
                    },
                )
                steps.append({
                    "agent_id": agent_id,
                    "agent_name": _agent_nm,
                    "agent_kind": agent.get("kind", ""),
                    "agent_model": agent.get("model", ""),
                    "status_message": (agent.get("processing_message") or "").strip(),
                    "status": "skipped_upstream",
                    "skip_reason": "upstream_skipped",
                    "output": "",
                    "final_state": "SkippedUpstream",
                    "duration_ms": 0,
                    "evidence_score": 0,
                    "transitions": [],
                    "trace": {
                        "diagnostics": [
                            {"level": "info", "text": skip_text}
                        ],
                    },
                })
                await _emit({
                    "type": "agent_skipped",
                    "step_index": i,
                    "agent_id": agent_id,
                    "agent_name": _agent_nm,
                    "reason": "upstream_skipped",
                    "reason_text": skip_text,
                })
                # last_result NÃO muda — o skip não vira resposta
                continue
            if _alt_src is not None:
                # source viva alcança este nó → ela é o upstream real; os gates
                # abaixo reavaliam a aresta dela (idempotentes) e decidem.
                upstream_id = _alt_src
                upstream_output = outputs_by_id[_alt_src]
                upstream_final_state = final_states_by_id.get(_alt_src, "")
                upstream_name = names_by_id.get(_alt_src, "")
                have_upstream = True
                _upstream_forced = True
                reactivated_edge_by_id[agent_id] = _alt_src
            # _alt_src None + source transparente inbound → segue pro fluxo
            # histórico abaixo (fallback linear) — fail-open.

        if _upstream_forced:
            pass  # upstream já resolvido acima (aresta inbound viva)
        elif (
            parent_id is not None
            and i > 0
            and parent_id != work_queue[i - 1]
            and parent_id in outputs_by_id
        ):
            upstream_id = parent_id
            upstream_output = outputs_by_id[parent_id]
            upstream_final_state = final_states_by_id.get(parent_id, "")
            upstream_name = names_by_id.get(parent_id, "")
            have_upstream = True
        elif i > 0 and last_result:
            upstream_id = work_queue[i - 1]
            upstream_output = last_result.get("output", "")
            upstream_final_state = last_result.get("final_state", "")
            upstream_name = steps[-1].get("agent_name", "") if steps else ""
            have_upstream = True
        else:
            upstream_id = None
            upstream_output = ""
            upstream_final_state = ""
            upstream_name = ""
            have_upstream = False

        # Conditional routing (2026-06-01; parent-aware 2026-06-05): se a
        # conexão parent→current é `conditional` e a expr avaliou false contra
        # o output do PARENT (+ a solicitação original do usuário via `input`),
        # skipa este agente — passthrough. Em fan-out, cada filho avalia contra
        # o MESMO source comum → 1-de-N elege o ramo certo (branch real).
        if have_upstream:
            skip_by_conditional = await _should_skip_conditional(
                source_id=upstream_id,
                target_id=agent_id,
                last_output=upstream_output,
                last_final_state=upstream_final_state,
                user_input=user_input,
                target_name=agent.get("name", ""),
                attachments=attachments,
                session_text=session_text,
                # Override "o anexo manda": handler declarado não é pulado quando
                # chega o tipo de anexo que ele aceita (router como dispatcher).
                target_accepts_documents=bool(agent.get("accepts_documents") or 0),
                target_accepts_images=bool(agent.get("accepts_images") or 0),
                # Postura B: os args selados alimentam `inputs.X` na regra da aresta.
                inputs=sealed_inputs,
            )
            if skip_by_conditional:
                # Reativação por cadeia válida (29.1.13 — achado N1 "Hélios"):
                # a aresta do upstream primário não casou, mas OUTRA aresta
                # inbound viva pode disparar (ex.: sequential de nó que
                # EXECUTOU). O primeiro-marca-vence do parent_of era artefato
                # da ordem de descoberta da BFS, não intenção de modelagem —
                # aresta sequential significa "roda sempre que a origem rodar".
                _alt_src, _alt_fired = await _alt_inbound_upstream(
                    agent_id, agent, i, exclude=upstream_id
                )
                if _alt_fired:
                    logger.info(
                        "mesh.chain.reactivated",
                        extra={
                            "event": "mesh.chain.reactivated",
                            "agent_id": agent_id,
                            "agent_name": agent.get("name", ""),
                            "preempted_source_id": upstream_id,
                            "via_source_id": _alt_src,
                        },
                    )
                    upstream_id = _alt_src
                    upstream_output = outputs_by_id[_alt_src]
                    upstream_final_state = final_states_by_id.get(_alt_src, "")
                    upstream_name = names_by_id.get(_alt_src, "")
                    reactivated_edge_by_id[agent_id] = _alt_src
                    await _emit({
                        "type": "agent_reactivated",
                        "step_index": i,
                        "agent_id": agent_id,
                        "agent_name": agent.get("name", ""),
                        "via_source_id": _alt_src,
                        "via_source_name": upstream_name,
                    })
                elif _defer(agent_id, agent, i, "conditional_false"):
                    # N1b: decisão prematura — há source de cadeia inbound ainda
                    # não processada nesta execução; reavalia no fim da fila.
                    continue
                else:
                    # Rastreabilidade do MOTIVO (Fatia 3a — 2026-06-07): distinguir
                    # "preterido pelo roteador" de "condição não satisfeita". O override
                    # de target estruturado roda ANTES da expr em _should_skip_conditional
                    # → skip COM bloco {"target": OUTRO} ⟺ preterido (1-de-N); skip SEM
                    # bloco ⟺ a expr de keyword não casou. Re-derivamos barato aqui (sem
                    # refatorar o gate, que é chamado direto por dezenas de testes).
                    _agent_nm = agent.get("name", "")
                    _routed = _extract_routed_target(upstream_output)
                    if _routed is not None and _norm_routing_name(_routed) != _norm_routing_name(_agent_nm):
                        skip_reason = "structured_target_not_chosen"
                        skip_text = (
                            f"Roteador selecionou «{_routed}» (target estruturado) — "
                            f"{_agent_nm} preterido"
                        )
                    else:
                        skip_reason = "conditional_false"
                        skip_text = f"Condição não satisfeita — {_agent_nm} pulado (passthrough)"
                    skipped_ids.add(agent_id)
                    names_by_id[agent_id] = _agent_nm
                    steps.append({
                        "agent_id": agent_id,
                        "agent_name": _agent_nm,
                        "agent_kind": agent.get("kind", ""),
                        "agent_model": agent.get("model", ""),
                        "status_message": (agent.get("processing_message") or "").strip(),
                        "status": "skipped_conditional",
                        "skip_reason": skip_reason,
                        "output": upstream_output,
                        "final_state": "SkippedConditional",
                        "duration_ms": 0,
                        "evidence_score": 0,
                        "transitions": [],
                        "trace": {
                            "diagnostics": [
                                {"level": "info", "text": skip_text}
                            ],
                        },
                    })
                    await _emit({
                        "type": "agent_skipped",
                        "step_index": i,
                        "agent_id": agent_id,
                        "agent_name": _agent_nm,
                        "reason": skip_reason,
                        "reason_text": skip_text,
                    })
                    # last_result NÃO muda — próximo agente recebe output do anterior
                    continue

            # Aresta default / "else" (2026-06-06): se a conexão parent→current
            # é `default` (catch-all) e ALGUM irmão condicional do mesmo parent
            # casou, este agente NÃO roda — o ramo condicional já respondeu. Só
            # roda quando NENHUM condicional casou (o "else" do roteamento 1-de-N).
            # Mata o dead-end do drift: pergunta fora de escopo cai num SA real.
            skip_by_default = await _should_skip_default(
                source_id=upstream_id,
                target_id=agent_id,
                last_output=upstream_output,
                last_final_state=upstream_final_state,
                user_input=user_input,
                attachments=attachments,
                session_text=session_text,
                inputs=sealed_inputs,
            )
            if skip_by_default:
                # Reativação por cadeia válida (29.1.13): simétrico ao gate
                # condicional — o "else" foi suprimido por um irmão que casou,
                # mas outra aresta inbound viva (ex.: sequential de nó que
                # executou) ainda pode disparar este nó.
                _alt_src, _alt_fired = await _alt_inbound_upstream(
                    agent_id, agent, i, exclude=upstream_id
                )
                if _alt_fired:
                    logger.info(
                        "mesh.chain.reactivated",
                        extra={
                            "event": "mesh.chain.reactivated",
                            "agent_id": agent_id,
                            "agent_name": agent.get("name", ""),
                            "preempted_source_id": upstream_id,
                            "via_source_id": _alt_src,
                        },
                    )
                    upstream_id = _alt_src
                    upstream_output = outputs_by_id[_alt_src]
                    upstream_final_state = final_states_by_id.get(_alt_src, "")
                    upstream_name = names_by_id.get(_alt_src, "")
                    reactivated_edge_by_id[agent_id] = _alt_src
                    await _emit({
                        "type": "agent_reactivated",
                        "step_index": i,
                        "agent_id": agent_id,
                        "agent_name": agent.get("name", ""),
                        "via_source_id": _alt_src,
                        "via_source_name": upstream_name,
                    })
                elif _defer(agent_id, agent, i, "default_suppressed"):
                    # N1b: simétrico ao gate condicional — adia antes de suprimir
                    # o "else" se ainda há source de cadeia inbound sem decisão.
                    continue
                else:
                    skipped_ids.add(agent_id)
                    names_by_id[agent_id] = agent.get("name", "")
                    steps.append({
                        "agent_id": agent_id,
                        "agent_name": agent.get("name", ""),
                        "agent_kind": agent.get("kind", ""),
                        "agent_model": agent.get("model", ""),
                        "status_message": (agent.get("processing_message") or "").strip(),
                        "status": "skipped_default",
                        "skip_reason": "default_sibling_matched",
                        "output": upstream_output,
                        "final_state": "SkippedDefault",
                        "duration_ms": 0,
                        "evidence_score": 0,
                        "transitions": [],
                        "trace": {
                            "diagnostics": [
                                {
                                    "level": "info",
                                    "text": f"Aresta default — {agent.get('name','')} pulado porque um ramo condicional casou (passthrough)",
                                }
                            ],
                        },
                    })
                    await _emit({
                        "type": "agent_skipped",
                        "step_index": i,
                        "agent_id": agent_id,
                        "agent_name": agent.get("name", ""),
                        "reason": "default_sibling_matched",
                    })
                    # last_result NÃO muda — próximo agente recebe output do anterior
                    continue

        # Context scope (2026-06-01): aplica política inherit/scoped/isolated
        # ao output do agente anterior ANTES de montar o prompt do próximo.
        # - inherit: comportamento padrão (output cru vira prefix)
        # - scoped: output passa por transform Jinja sandboxed (truncate,
        #   first-line, regex, etc) — economiza tokens + governance
        # - isolated: próximo agente recebe SÓ user_input, sem prefix
        # Fail-OPEN no runtime — ver `_resolve_context_scope`.
        scope_resolution = None
        if have_upstream:
            scope_resolution = await _resolve_context_scope(
                source_id=upstream_id,
                target_id=agent_id,
                last_output=upstream_output,
                last_final_state=upstream_final_state,
                user_input=user_input,
                attachments=attachments,
            )
            if scope_resolution["skip_prefix"]:
                # mode=isolated → próximo agente recebe SÓ a solicitação
                # original, sem nenhuma menção ao output anterior.
                current_input = user_input
            else:
                scoped_output = scope_resolution["output"]
                current_input = (
                    f"## Contexto do agente anterior ({upstream_name}):\n"
                    f"{scoped_output}\n\n"
                    f"## Solicitação original:\n{user_input}"
                )
            if scope_resolution["mode"] != "inherit":
                # Só emite evento quando scope efetivamente filtrou algo.
                # `inherit` é o default — não polui o stream de eventos.
                await _emit({
                    "type": "context_scope_applied",
                    "step_index": i,
                    "source_id": upstream_id,
                    "target_id": agent_id,
                    "mode": scope_resolution["mode"],
                    "chars_before": scope_resolution["chars_before"],
                    "chars_after": scope_resolution["chars_after"],
                })

        # Emite agent_start ANTES de chamar execute_interaction (LLM tarda 1-30s,
        # esse evento é o que destrava o UX "ao vivo" durante a chamada).
        await _emit({
            "type": "agent_start",
            "step_index": i,
            "agent_id": agent_id,
            "agent_name": agent.get("name", ""),
            "agent_kind": agent.get("kind", ""),
            "agent_model": agent.get("model", ""),
            "processing_message": (agent.get("processing_message") or "").strip(),
        })

        # pipeline_context segue o scope: isolated → None (zera ctx vindo
        # do anterior); inherit/scoped → versão filtrada que entrou no prompt.
        if scope_resolution and scope_resolution["skip_prefix"]:
            pipeline_ctx = None
        elif scope_resolution:
            pipeline_ctx = scope_resolution["output"]
        elif have_upstream:
            pipeline_ctx = upstream_output
        else:
            pipeline_ctx = None

        # ── Dispatcher de anexos (2026-06-06 — "router como dispatcher") ──
        # Entry (i==0) recebe TODOS os anexos (inalterado — zero regressão).
        # Downstream (i>0) recebe só o subconjunto que DECLARA aceitar; antes
        # recebia None → o especialista era CEGO ao arquivo bruto (via apenas o
        # texto do upstream em pipeline_context). Ver _filter_attachments_by_agent.
        _forwarded_atts = (
            attachments if i == 0 else _filter_attachments_by_agent(agent, attachments)
        )
        _fwd_names: list[str] = []
        if i > 0 and _forwarded_atts:
            _fwd_names = [str(a.get("name", "") or "") for a in _forwarded_atts]
            logger.info(
                "mesh.dispatch.attachments_forwarded",
                extra={
                    "event": "mesh.dispatch",
                    "source_id": upstream_id,
                    "target_id": agent_id,
                    "target_name": agent.get("name", ""),
                    "count": len(_forwarded_atts),
                    "names": _fwd_names,
                },
            )
            await _emit({
                "type": "attachments_dispatched",
                "step_index": i,
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "count": len(_forwarded_atts),
                "names": _fwd_names,
            })

        try:
            result = await execute_interaction(
                agent_id=agent_id,
                user_input=current_input,
                channel=channel,
                attachments=_forwarded_atts,
                pipeline_context=pipeline_ctx,
                # RAG em pipeline (2026-06-06): busca de evidências usa a pergunta
                # ORIGINAL e limpa — não o `current_input` prefixado com o texto do
                # upstream. Assim o BM25/vetorial do especialista não é poluído.
                retrieval_query=user_input,
                # Só o primeiro agente reutiliza a session_id do request.
                # Subsequentes criam sub-interactions próprias (child_interaction_ids)
                # que se ligam à master via execute_pipeline:2298+ depois.
                session_id=session_id if i == 0 else None,
                # Memória de conversa: só o entry (i==0) tem session_id → só ele
                # carrega histórico. Subagentes (i>0) recebem session_id=None e,
                # por política de camada, janela 0 — seguem stateless.
                context_mode=context_mode,
                # Envelope param selado: vai a TODO passo declarativo da cadeia
                # (entry e downstream), soberano sobre o bloco do roteador.
                sealed_inputs=sealed_inputs,
                # Auditoria (24.10.0): dono do julgamento nas verifications —
                # em run não-selado, só steps MEMBROS do pipeline inferido.
                pipeline_id=(
                    pipeline_id
                    if (_pipeline_members is None or agent_id in _pipeline_members)
                    else None
                ),
                # Todo downstream (i>0) É step de pipeline p/ o gate do verifier,
                # mesmo com upstream fast-routed (pipeline_context vazio). O entry
                # (i==0) mantém o comportamento atual (sinal via pipeline_context).
                pipeline_step=(i > 0),
                # Postura de auditoria por pipeline (26.1.0) + master p/ o dispatch
                # async gravar direto no master (evita órfã na consolidação).
                audit_posture=_audit_posture,
                master_interaction_id=master_interaction_id,
                # Paridade harness (Pacote C): None = comportamento atual.
                grounding_strict=grounding_strict,
                # Passo aninhado (35.14.5): NÃO reseta o ContextVar de criação —
                # herda o dono/customer_hash que execute_pipeline setou (3577),
                # senão master+filhas nascem órfãs (IDOR #595) e sem pivô LGPD (#601).
                inherit_creation_context=True,
            )
            iid = result.get("interaction_id")
            # Primeiro agente executado (não pass-through) vira o master
            if master_interaction_id is None:
                master_interaction_id = iid
            else:
                child_interaction_ids.append(iid)

            # PR7 (cost auto-wire): custo/tokens REAIS por step do mesh (helper testável).
            _step_cost, _step_tokens = _step_cost_and_tokens(result, agent)
            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name",""),
                "agent_kind": agent.get("kind",""),
                # Modelo REALMENTE usado (pós-swap multimodal), não o snapshot.
                "agent_model": _step_effective_model(result, agent),
                # Narrativa humanizada (💬) exposta por step p/ a projeção 'summary'
                # da resposta de invoke — antes só vivia dentro de trace.execution_log.
                "status_message": (agent.get("processing_message") or "").strip(),
                "status": "completed",
                "output": result.get("output",""),
                "final_state": result.get("final_state",""),
                "duration_ms": result.get("duration_ms", 0),
                "evidence_score": result.get("evidence_score", 0),
                "transitions": result.get("transitions", []),
                "trace": result.get("trace"),
                # Auditoria (24.10.0): snapshot do julgamento do step (steps
                # rigorous têm verifier próprio agora) — as interactions filhas
                # são deletadas na consolidação, este JSON é o rastro que fica.
                "verification": result.get("verification"),
                "interaction_id": iid,
                "tokens_used": _step_tokens,
                "cost_usd": _step_cost,
                # Dispatcher: anexos efetivamente entregues a este SA (i>0). Surge
                # no painel de rastreabilidade — operador vê que o arquivo chegou.
                **({"dispatched_attachments": _fwd_names} if _fwd_names else {}),
            })
            last_result = result
            owner_step = steps[-1]  # âncora acompanha o last_result (Pacote B)
            # Tracking por-id pro roteamento parent-aware (fan-out): registra
            # output/estado/nome deste nó pra que filhos cujo parent NÃO é o
            # irmão anterior avaliem contra o output correto.
            outputs_by_id[agent_id] = result.get("output", "")
            final_states_by_id[agent_id] = result.get("final_state", "")
            names_by_id[agent_id] = agent.get("name", "")
            await _emit({
                "type": "agent_done",
                "step_index": i,
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "status": "completed",
                "duration_ms": result.get("duration_ms", 0),
                "final_state": result.get("final_state", ""),
                # Preview SEM a linha DECISAO (36.2.1): mesmo regime de
                # apresentação do resultado final, com fallback de eco pelos
                # agentes ANTERIORES da cadeia. Só computa quando há consumidor
                # (sem callback, _emit descartaria — e sem o cache de topologia
                # o lookup por step seria pago à toa; review pré-push).
                "output_preview": (
                    await _display_preview(
                        result.get("output", ""), agent_id,
                        fallback_agent_ids=[s.get("agent_id") or "" for s in steps],
                    ) if progress_callback is not None else ""
                ),
                # 35.4.0 (aditivo): custo/tokens REAIS do step + iid — o worker
                # do invoke-job acumula via callback p/ que um TIMEOUT não suma
                # com o gasto dos steps já concluídos (ledger/orçamento por key).
                "cost_usd": _step_cost,
                "tokens_used": _step_tokens,
                "interaction_id": iid,
            })
        except Exception as e:
            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name",""),
                "status": "error",
                "error": str(e)[:200],
            })
            await _emit({
                "type": "agent_error",
                "step_index": i,
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "error": str(e)[:200],
            })
            break

    # ── Consolidar sessões ──
    if master_interaction_id:
        turn_number = 3
        for i, step in enumerate(steps):
            if step.get("status") == "passthrough":
                continue
            cid = step.get("interaction_id")
            if not cid or cid == master_interaction_id:
                continue
            if step.get("status") == "completed" and step.get("output"):
                from app.core.database import turns_repo
                from app.core.dlp import redact_for_persist
                from app.core.config import get_settings as _gs_dlp
                import uuid as _uuid
                _step_out = step["output"]
                if _gs_dlp().dlp_enabled:
                    _step_out = redact_for_persist(_step_out)
                from app.core.interaction_access import turn_customer_hash_fragment
                await turns_repo.create({
                    "id": str(_uuid.uuid4()),
                    "turn_number": turn_number,
                    "output_text_redacted": _step_out,
                    "interaction_id": master_interaction_id,
                    # FIN-3 (35.12.0): grão por step — tokens/latência REAIS
                    # já calculados no step (custo auto-wire PR7).
                    "tokens_used": int(step.get("tokens_used") or 0),
                    "latency_ms": float(step.get("duration_ms") or 0),
                    **turn_customer_hash_fragment(),  # LGPD-2 por-turno (35.15.0)
                })
                turn_number += 1

        for cid in child_interaction_ids:
            if cid:
                try:
                    # Auditoria (24.10.0): re-aponta as verifications da filha
                    # pro master ANTES do delete — senão o julgamento do step
                    # (rigorous) vira linha órfã, invisível no deep-link do
                    # /quality?interaction_id=master. verifications NÃO cascateia
                    # de propósito (é auditoria do juiz preservada); por isso o
                    # re-point continua explícito aqui.
                    try:
                        from app.core.database import _get_pool
                        async with _get_pool().acquire() as _con:
                            await _con.execute(
                                "UPDATE verifications SET interaction_id = $1 "
                                "WHERE interaction_id = $2",
                                master_interaction_id, cid,
                            )
                    except Exception:
                        pass
                    # FK ON DELETE CASCADE (33.5.0): deletar a interaction filha
                    # já apaga seus turns/tool_calls/binding_executions no banco —
                    # o loop manual de turns (find_all+delete, limit=100 que
                    # truncava >100) virou redundante e foi removido.
                    await interactions_repo.delete(cid)
                except Exception:
                    pass

        await interactions_repo.update(master_interaction_id, {
            "title": f"🔗 {user_input[:70].strip()}",
        })

    total_duration = round((time.time() - start) * 1000, 2)
    final_output = last_result.get("output","") if last_result else "Pipeline sem resultado"

    # Contabilizar pass-through vs executed
    passthrough_count = sum(1 for s in steps if s.get("status") == "passthrough")
    executed_count = sum(1 for s in steps if s.get("status") == "completed")

    # Tipo de conexão REAL por nó (display honesto: não cravar "sequential"
    # quando a aresta parent→nó é `conditional`). Lookup leve por step, contra
    # o parent real. Fail-open → "sequential" se a busca falhar.
    conn_type_by_id: dict = {}
    for s in steps:
        _aid = s.get("agent_id", "")
        # Nó reativado por aresta inbound alternativa (29.1.13): o display
        # aponta a aresta que DE FATO disparou, não a do parent BFS preterido.
        _pid = reactivated_edge_by_id.get(_aid) or parent_of.get(_aid)
        if not _pid:
            continue
        try:
            _conns = await _topo_mesh_out(_pid, limit=20)
            _c = next((c for c in _conns if c.get("target_agent_id") == _aid), None)
            conn_type_by_id[_aid] = (_c or {}).get("connection_type", "sequential")
        except Exception:
            conn_type_by_id[_aid] = "sequential"

    pipeline_mesh = [
        {
            "id": s.get("agent_id",""), "name": s.get("agent_name",""),
            "kind": s.get("agent_kind",""), "model": s.get("agent_model",""),
            "role": "entry_point" if idx==0 else "downstream",
            "connection": conn_type_by_id.get(s.get("agent_id",""), "sequential"),
            "passthrough": s.get("status") == "passthrough",
        }
        for idx, s in enumerate(steps) if s.get("status") in ("completed", "passthrough")
    ]

    total_evidence = sum(s.get("trace",{}).get("evidence_count",0) for s in steps if s.get("trace"))
    all_evidence_sources = []
    for s in steps:
        if s.get("trace",{}).get("evidence_sources"):
            all_evidence_sources.extend(s["trace"]["evidence_sources"])

    last_completed = [s for s in steps if s.get("status") == "completed"]
    last_step_diag = last_completed[-1].get("trace",{}).get("diagnostics",[]) if last_completed else []
    all_diagnostics = [{"level":"success","text":f"Pipeline completo: {executed_count}/{len(chain)} executados, {passthrough_count} pass-through, em {total_duration:.0f}ms"}]
    if passthrough_count > 0:
        all_diagnostics.append({"level":"info","text":f"Short-circuit: {passthrough_count} agente(s) sem SKILL.md ignorado(s) — economia de ~{passthrough_count * 20}s estimados"})
    all_diagnostics.extend(last_step_diag)

    # ── Agregar execution_logs de todos os steps ──
    all_exec_logs = []
    for s in steps:
        if s.get("trace", {}).get("execution_log"):
            step_name = s.get("agent_name", "?")
            status_label = "⚡ PASS-THROUGH" if s.get("status") == "passthrough" else "🔄 EXECUTADO"
            all_exec_logs.append({"cat": "pipeline", "icon": "🔗", "title": f"─── {step_name} ({status_label}) ───", "detail": "", "level": "info"})
            all_exec_logs.extend(s["trace"]["execution_log"])

    # ── Âncora do envelope (Pacote B, 35.0.0) ──
    # O `output` sempre veio do dono do last_result, mas final_state/transitions
    # vinham de steps[-1] — que em fan-out 1-de-N pode ser um step PULADO: a
    # resposta era de um agente e o "estado final" de outro (às vezes de um que
    # nem rodou). Agora decisão, transições e evidence_score vêm do MESMO step
    # que produziu o output (owner_step acompanha cada atribuição de last_result
    # — mesma proteção que o harness modo-pipeline #589 já fazia por fora), e o
    # envelope declara QUEM respondeu (`output_agent`, aditivo). evidence_score
    # deixou de ser o MAX da cadeia (inflava confiança quando quem respondeu não
    # citou evidência). Fallback steps[-1]: cadeia sem nenhum produtor (tudo
    # skip/passthrough) preserva o comportamento antigo.
    _owner_step = owner_step if owner_step is not None else (steps[-1] if steps else None)
    # Contrato de Decisão (35.19.0): a linha DECISAO é protocolo de máquina — o
    # gate já a leu e steps/trace a preservam (auditoria). Sai APENAS da resposta
    # final apresentada (decisão de design 2026-07-15). Gate duplo no helper
    # (schema declarado + pares válidos): agente sem contrato → no-op, prosa
    # legítima 'Decisão: ...' fica intacta. Fail-safe: erro → output intacto.
    # Tenta o schema do DONO da resposta e depois os dos demais steps: o agente
    # final sem contrato pode ECOAR a linha do upstream que veio no contexto
    # (review pré-push 2026-07-15) — lookups memoizados por pipeline.
    # 36.1.0: a decisão ESTRUTURADA entra no envelope (`decision`) ANTES do
    # strip — o consumidor máquina não parseia texto. Semântica: o DONO da
    # resposta tem prioridade; sem contrato nele, vale a decisão mais recente
    # anunciada na cadeia (o caso comum: triagem decide, especialista responde).
    _final_decision = None
    if final_output and owner_step is not None and has_decision_line(final_output):
        _final_decision = await extract_decision_for_agent(
            final_output, owner_step.get("agent_id") or "")
        try:
            final_output = await _strip_for_display_multi(
                final_output,
                [(owner_step or {}).get("agent_id") or "", *[(s or {}).get("agent_id") or "" for s in steps]],
            )
        except Exception:
            logger.warning("pipeline.decision_line_strip_failed", exc_info=True)
    if _final_decision is None:
        try:
            _final_decision = await _decision_from_steps(steps)
        except Exception:
            _final_decision = None
    # Apresentação POR STEP (Backlog 4, review pré-push 36.2.1): os balões do
    # chat AO VIVO renderizam `pipeline_steps[].output` — sem isto a linha
    # DECISAO do agente intermediário aparecia durante a execução e SUMIA no F5
    # (o reload de sessão stripa por autor). `output` segue CRU (trace/gate/
    # auditoria); `output_display` só existe quando difere (payload ~0 no caso
    # comum). A trilha "Como cheguei aqui" do modal do fluxograma segue CRUA
    # de propósito (trilha≈trace, decisão da Fase 1).
    try:
        _chain_ids = [(s or {}).get("agent_id") or "" for s in steps]
        for _s in steps:
            _out_s = _s.get("output") or ""
            if _out_s and has_decision_line(_out_s):
                _disp = await _strip_for_display_multi(
                    _out_s, [(_s.get("agent_id") or ""), *_chain_ids])
                if _disp != _out_s:
                    _s["output_display"] = _disp
    except Exception:
        logger.warning("pipeline.step_display_failed", exc_info=True)
    final_result = {
        "mode": "pipeline",
        "output": final_output,
        # Contrato de Decisão estruturado (36.1.0, ADITIVO): {campo: valor} do
        # agente que produziu a resposta, ou None. O texto apresentado não tem
        # mais a linha — este campo é a via de máquina.
        "decision": _final_decision,
        # output_agent SÓ quando houve produtor REAL (owner_step) — no fallback
        # steps[-1] (cadeia sem produtor: tudo skip/erro) atribuir autoria a um
        # agente que não respondeu seria mentira (review adversarial). Os demais
        # campos usam o fallback p/ preservar o comportamento legado degenerado.
        "output_agent": ({"id": owner_step.get("agent_id"), "name": owner_step.get("agent_name")}
                         if owner_step is not None else None),
        "pipeline_steps": steps,
        "total_agents": len(chain),
        "completed_agents": executed_count,
        "passthrough_agents": passthrough_count,
        "duration_ms": total_duration,
        "interaction_id": master_interaction_id,
        "final_state": _owner_step.get("final_state") if _owner_step else None,
        "evidence_score": _owner_step.get("evidence_score", 0) if _owner_step else 0,
        "transitions": _owner_step.get("transitions", []) if _owner_step else [],
        "status": "completed",
        "trace": {
            "total_steps": sum(len(s.get("transitions",[])) for s in steps),
            "evidence_count": total_evidence,
            "evidence_sources": list(set(all_evidence_sources)),
            "diagnostics": all_diagnostics,
            "mesh_chain": pipeline_mesh,
            "attachments": [],
            "channel": channel,
            "journey": "",
            "execution_log": all_exec_logs,
        },
    }

    # ── Persistir trace_data agregado no interaction mestre (2026-06-06) ──
    # Mirror do que o POST /chat sync já faz pós-execução. Garante que ao
    # recarregar a sessão de pipeline o GET /sessions devolva mode='pipeline'
    # + pipeline_steps + execution_log — assim a Rastreabilidade e o Execution
    # Log voltam e o toggle fica em Pipeline (antes sumiam e caía em 'agent',
    # porque o caminho /chat/stream nunca gravava trace_data). Cobre stream E
    # sync num lugar só. Falha de persist não derruba a resposta (já temos
    # final_result) — só loga pra troubleshooting.
    if master_interaction_id:
        try:
            await interactions_repo.update(
                master_interaction_id,
                {"trace_data": json.dumps(
                    _build_pipeline_trace_data(
                        master_interaction_id, entry_agent_id, final_result
                    ),
                    ensure_ascii=False,
                    default=str,
                )},
            )
        except Exception as _persist_err:
            logger.warning(
                "pipeline.trace_data_persist_failed",
                extra={
                    "event": "pipeline.trace_data_persist",
                    "interaction_id": master_interaction_id,
                    "error_type": type(_persist_err).__name__,
                },
                exc_info=True,
            )

    await _emit({"type": "pipeline_done", "result": final_result})

    _clear_topo()
    return final_result


_conditional_jinja_env = None


def _get_conditional_jinja_env():
    """Sandboxed Jinja2 environment para avaliar expressões de roteamento
    condicional do AI Mesh. Lazy init pra não acoplar import no startup.
    """
    global _conditional_jinja_env
    if _conditional_jinja_env is None:
        from jinja2.sandbox import SandboxedEnvironment
        from jinja2 import ChainableUndefined
        _conditional_jinja_env = SandboxedEnvironment(
            undefined=ChainableUndefined,
            autoescape=False,
        )
    return _conditional_jinja_env


# Extensões reconhecidas para classificar anexos sem depender só do MIME
# (o browser às vezes manda type vazio/genérico). Espelha _classify_attachment
# da camada de rotas, mas vive aqui pra o gate condicional não importar rotas.
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "heif", "tif", "tiff"}
_DOC_EXTS = {
    "pdf", "doc", "docx", "ppt", "pptx", "pps", "ppsx", "xls", "xlsx",
    "csv", "txt", "md", "markdown", "rtf", "odt", "ods", "odp", "json",
}


def _attachment_ext(name: str | None) -> str:
    """Extensão (sem ponto, lowercase) do nome de arquivo — '' se não tiver."""
    n = (name or "").strip().lower()
    if "." not in n:
        return ""
    return n.rsplit(".", 1)[-1]


def _classify_attachment_kind(att: dict | None) -> str:
    """'image' | 'document' | 'other' — por MIME (prioritário) ou extensão.

    O roteamento por anexo precisa de um sinal de TIPO robusto: alguns uploads
    chegam com `type` vazio do browser, então caímos pra extensão do nome.
    """
    t = str((att or {}).get("type", "")).lower()
    ext = _attachment_ext((att or {}).get("name", ""))
    if t.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    doc_mime_prefixes = (
        "application/pdf",
        "application/msword",
        "application/vnd",  # openxmlformats (docx/pptx/xlsx) + oasis (odt/ods)
        "application/rtf",
        "application/json",
        "text/",  # txt/csv/markdown
    )
    if ext in _DOC_EXTS or any(t.startswith(m) for m in doc_mime_prefixes):
        return "document"
    return "other"


def _attachment_image_data_url(att: dict | None) -> str | None:
    """Data URL (``data:<mime>;base64,…``) de um anexo de IMAGEM, ou ``None``.

    Lê os bytes de ``att['abs_path']`` (caminho absoluto já saneado/validado pela
    rota dona do diretório de uploads) OU de um base64 já presente
    (``image_b64``/``content_base64``). Só dispara para anexos classificados como
    imagem (ver ``_classify_attachment_kind``). Erro de leitura → log + ``None``
    (fail-soft: a interação segue sem a imagem, melhor que derrubar o turno).
    """
    if _classify_attachment_kind(att) != "image":
        return None
    mime = str((att or {}).get("type", "")).strip() or "image/png"
    b64 = (att or {}).get("image_b64") or (att or {}).get("content_base64")
    if not b64:
        p = (att or {}).get("abs_path") or ""
        if not p:
            return None
        try:
            import base64 as _b64
            from pathlib import Path as _P
            b64 = _b64.b64encode(_P(p).read_bytes()).decode()
        except Exception as e:  # pragma: no cover - I/O defensivo
            logger.warning(
                "mesh.vision.image_read_failed",
                extra={
                    "event": "mesh.vision",
                    "attachment_name": (att or {}).get("name", ""),
                    "error_type": type(e).__name__,
                    "error_msg": str(e)[:200],
                },
            )
            return None
    return f"data:{mime};base64,{b64}"


def _build_user_message_content(
    text: str, attachments: list | None, provider: str, model: str
):
    """Conteúdo da ``HumanMessage`` do turno atual.

    Retorna ``text`` (str — caminho legado) OU uma lista multimodal
    ``[{type:text}, {type:image_url}, …]`` quando há anexo de IMAGEM **e** o
    modelo candidato é multimodal (``is_multimodal``). Para modelo text-only a
    imagem é DESCARTADA (registrado em ``mesh.vision.image_dropped_text_only_model``)
    — gpt-oss & cia. quebram (400) se receberem ``image_url``.

    Construído POR CANDIDATO (recebe provider/model da tentativa atual) para que a
    cadeia de resiliência possa cair de um modelo de visão para um text-only sem
    mandar imagem a quem não aceita. Corrige o bug "SA Imagem devolve objects
    vazio": antes a imagem virava só o texto "ImageSize: LxA" e nunca chegava ao
    LLM como pixels.
    """
    if not attachments:
        return text
    imgs = [a for a in attachments if _classify_attachment_kind(a) == "image"]
    if not imgs:
        return text
    from app.llm_routing import is_multimodal
    if not is_multimodal(provider, model):
        logger.info(
            "mesh.vision.image_dropped_text_only_model",
            extra={
                "event": "mesh.vision",
                "provider": provider,
                "model": model,
                "image_count": len(imgs),
                "decision": "dropped_text_only",
            },
        )
        return text
    parts: list = [{"type": "text", "text": text}]
    attached = 0
    for a in imgs:
        durl = _attachment_image_data_url(a)
        if durl:
            parts.append({"type": "image_url", "image_url": {"url": durl}})
            attached += 1
    if attached == 0:
        return text
    logger.info(
        "mesh.vision.images_attached",
        extra={
            "event": "mesh.vision",
            "provider": provider,
            "model": model,
            "image_count": attached,
            "decision": "attached",
        },
    )
    return parts


def _image_is_grounding(attachments: list | None, provider: str, model: str) -> bool:
    """True se há anexo de IMAGEM que será enviado a um modelo MULTIMODAL — ou
    seja, a imagem É evidência/grounding (o SA de visão responde a partir dela).

    Usado pelo grounded-by-default (`_grounding_guard`): sem isto, como a imagem
    não entra mais em `attachment_context` (texto) desde o #310, o guard recusaria
    todo SA de imagem por "falta de evidência". Conta SÓ p/ modelo multimodal — em
    text-only a imagem é descartada (ver `_build_user_message_content`), então não
    é grounding e a recusa permanece correta.
    """
    if not attachments:
        return False
    from app.llm_routing import is_multimodal
    return any(
        _classify_attachment_kind(a) == "image" for a in attachments
    ) and is_multimodal(provider, model)


def _filter_attachments_by_agent(
    agent: dict | None, attachments: list | None
) -> list | None:
    """Subconjunto de `attachments` cujo TIPO o agente DECLARA aceitar.

    "Router como dispatcher" (2026-06-06): antes, no pipeline multi-agente, só o
    nó de entrada (i==0) recebia os anexos; todo SA downstream recebia ``None``
    (ver execute_pipeline). Resultado: o especialista era CEGO ao arquivo bruto —
    via apenas o TEXTO do upstream via pipeline_context. Um SA de documentos não
    conseguia analisar o documento que o usuário soltou; pior, "se fundamentava"
    na prosa do upstream (buraco de grounding que o PR A não pega, pois
    pipeline_context conta como evidência).

    Agora cada SA downstream recebe só o que sabe tratar:
    - ``accepts_documents`` → anexos kind 'document' (e 'other', best-effort: se
      o markitdown não classificou, é melhor entregar os bytes ao handler de
      documentos do que descartá-los);
    - ``accepts_images``   → anexos kind 'image'.

    Defaults da tabela agents são 0 → forwarding é OPT-IN: um SA só recebe anexo
    se o operador marcou a capacidade. Retorna ``None`` (não ``[]``) quando nada
    passa, para casar a assinatura de execute_interaction e o caminho legado
    (sem anexos → sem attachment_context).
    """
    atts = attachments or []
    if not atts:
        return None
    accepts_doc = bool((agent or {}).get("accepts_documents") or 0)
    accepts_img = bool((agent or {}).get("accepts_images") or 0)
    if not accepts_doc and not accepts_img:
        return None
    out: list = []
    for a in atts:
        kind = _classify_attachment_kind(a)
        if kind == "image" and accepts_img:
            out.append(a)
        elif kind in ("document", "other") and accepts_doc:
            out.append(a)
    return out or None


def _target_handles_attachment(
    *,
    accepts_documents: bool,
    accepts_images: bool,
    attachments: list | None,
) -> bool:
    """True se o agente-alvo DECLARA capacidade para algum anexo presente.

    Override de roteamento por CAPACIDADE (2026-06-06 — "router como dispatcher"),
    irmão do override "o roteador mandou" (`_output_names_target`): um especialista
    que marcou `accepts_documents`/`accepts_images` NUNCA deve ser pulado quando
    chega um anexo do tipo que ele declarou tratar — independente da expr de
    keyword. É a autoridade de CAPACIDADE: roda o agente que o desenho elegeu para
    aquele tipo de arquivo, em vez de perder o anexo por vocabulário que não bate a
    regra (a causa-raiz do bug "Doc Analise": SA pulado com documento anexado).

    Fail-safe (roda o SA) e opt-in (defaults 0 → só dispara para handlers
    explícitos). 'other' conta como documento — ver _filter_attachments_by_agent.
    """
    atts = attachments or []
    if not atts:
        return False
    kinds = {_classify_attachment_kind(a) for a in atts}
    has_doc = "document" in kinds or "other" in kinds
    has_img = "image" in kinds
    return bool((accepts_documents and has_doc) or (accepts_images and has_img))


class _MissingArg:
    """`inputs.<campo>` AUSENTE numa regra condicional: falsy e comparação-SEGURA em
    QUALQUER operador (==, !=, <, >, <=, >=, `in`, aninhado). Assim uma regra por
    valor (ex.: `inputs.limite > 1000`) com o campo omitido simplesmente NÃO casa —
    em vez de estourar (o que, no fail-open do gate, faria o agente RODAR, o oposto
    do intent). O `ChainableUndefined` do Jinja é falsy em `==`/`in` mas ESTOURA em
    ordenação — por isso o sentinel próprio."""
    __slots__ = ()
    def __eq__(self, o): return False
    def __ne__(self, o): return True
    def __lt__(self, o): return False
    def __le__(self, o): return False
    def __gt__(self, o): return False
    def __ge__(self, o): return False
    def __bool__(self): return False
    def __contains__(self, o): return False
    def __iter__(self): return iter(())
    def __len__(self): return 0
    def __getattr__(self, name): return self
    def __getitem__(self, key): return self
    def __hash__(self): return 0
    def __str__(self): return ""


_MISSING_ARG = _MissingArg()


class _ArgsView(dict):
    """Args do caller expostos às regras condicionais: chave AUSENTE → sentinel
    comparação-segura (`_MissingArg`), não KeyError/Undefined. Chave presente = valor
    normal."""
    def __missing__(self, key):
        return _MISSING_ARG


# 38.0.0 (review): régua ÚNICA no módulo-folha textnorm — as vars *_norm do
# runtime e os repairs dos tradutores (NL→Jinja, NL→args) normalizam igual.
# Alias mantido: testes e este módulo referenciam engine._strip_accents.
from app.agents.textnorm import strip_accents as _strip_accents  # noqa: E402


def _build_conditional_context(
    output: str | None = None,
    final_state: str | None = None,
    user_input: str | None = None,
    attachments: list | None = None,
    session_text: str | None = None,
    inputs: dict | None = None,
    decision: dict | None = None,
) -> dict:
    """Monta o dict de variáveis disponíveis para expressões condicionais.

    Centralizado para garantir que runtime (`_should_skip_conditional`),
    endpoint de teste (`POST /mesh/connections/test-conditional`) e a UI
    de "vars panel" mostrem EXATAMENTE o mesmo conjunto.

    Vars (2026-06-01 — wizard expansion):
    - `output` (str): texto da resposta do agente upstream
    - `output_lower` (str): output em lowercase
    - `output_length` (int): len(output) — útil pra detectar respostas
      vazias ou curtas demais
    - `has_output` (bool): output não-vazio
    - `final_state` (str): "Recommend"/"Refuse"/"Escalate"/"LogAndClose"
    - `is_recommend` / `is_refuse` / `is_escalate` (bool): atalhos comuns
    - `contains_image` (bool): output menciona imagem (jpg/png/gif/webp/
      "imagem")
    - `contains_url` (bool): output menciona URL (http:// ou https://)
    - `contains_pdf` (bool): output menciona pdf
    - `lines_count` (int): número de linhas (\\n + 1)

    Vars (2026-06-05 — roteamento 1-de-N / fan-out):
    - `input` (str): solicitação ORIGINAL do usuário (entrada do pipeline)
    - `input_lower` (str): input em lowercase

    Por que `input`: o roteamento condicional (fan-out AOBD/SR → N SAs)
    quase sempre quer ramificar pela PERGUNTA do usuário ("quando o usuário
    pede X → delegar ao SA-X"), não pelo texto que o agente anterior
    devolveu. Sem `input`, a expr derivada do "quando …" só casaria se o
    upstream ecoasse a palavra-chave no output — frágil. Aditivo: exprs
    antigas (sobre `output`) continuam idênticas.
    """
    out = output or ""
    out_lower = out.lower()
    fs = final_state or ""
    fs_lower = fs.lower()
    inp = user_input or ""
    inp_lower = inp.lower()
    # ── Sinais de anexo (2026-06-06) ──────────────────────────────────
    # Roteamento fan-out muitas vezes ramifica pelo ARQUIVO que o usuário
    # soltou (ex.: "documento → SA Documentos"), não pelo texto digitado —
    # frequentemente vazio/genérico ("o que temos aqui"). Sem isto, drops de
    # arquivo não casavam nenhuma expr → todos os SAs pulados (dead-end).
    atts = attachments or []
    att_names = " ".join(str(a.get("name", "") or "") for a in atts).lower().strip()
    att_types = " ".join(str(a.get("type", "") or "") for a in atts).lower().strip()
    att_exts = " ".join(e for e in (_attachment_ext(a.get("name", "")) for a in atts) if e)
    _kinds = {_classify_attachment_kind(a) for a in atts}
    # ── Sinal pegajoso da sessão (2026-06-06 — memória de conversa) ─────
    # Texto das perguntas recentes do usuário (lowercase) vindo de
    # `conversation_memory.session_text_window`. Sob context_mode ativo, isto
    # faz o gate casar follow-ups por keyword mesmo quando o turno ATUAL é vago
    # ("sobre qual o tema") — a keyword do turno anterior ainda conta. '' quando
    # context off → text_all byte-idêntico ao legado.
    sess_text = (session_text or "").lower().strip()
    # text_all: pergunta + nome/extensão do anexo + perguntas recentes da sessão
    # num só campo — a expr derivada casa keyword no texto digitado, no arquivo
    # OU no histórico recente (1 var, não três).
    text_all = " ".join(p for p in (inp_lower, att_names, att_exts, sess_text) if p).strip()
    return {
        "output": out,
        "output_lower": out_lower,
        "output_length": len(out),
        "has_output": bool(out.strip()),
        "final_state": fs,
        "is_recommend": fs_lower == "recommend",
        "is_refuse": fs_lower == "refuse",
        "is_escalate": fs_lower == "escalate",
        "contains_image": any(
            kw in out_lower
            for kw in ("imagem", ".jpg", ".jpeg", ".png", ".gif", ".webp")
        ),
        "contains_url": "http://" in out_lower or "https://" in out_lower,
        "contains_pdf": ".pdf" in out_lower or " pdf " in out_lower,
        "lines_count": out.count("\n") + 1 if out else 0,
        "input": inp,
        "input_lower": inp_lower,
        # Anexos (2026-06-06) — roteamento por arquivo
        "has_attachments": bool(atts),
        "attachment_count": len(atts),
        "attachment_names": att_names,
        "attachment_types": att_types,
        "attachment_exts": att_exts,
        "has_document": "document" in _kinds,
        "has_image": "image" in _kinds,
        "text_all": text_all,
        # ── Normalizadas pt-BR (35.17.0) ──────────────────────────────────
        # Lowercase + SEM acento. Casam 'nao reconheco' com 'não reconheço' e
        # 'e' com 'é' sem o autor precisar enumerar as duas grafias — a dor #1
        # dos fluxos reais (a mesma palavra digitada 2x na expr). ADITIVAS: as
        # exprs existentes (sobre *_lower) seguem byte-idênticas. O card de
        # palavra-chave gera com estas por default em REGRAS NOVAS.
        # casefold (35.19.2, review do #617): 'ß' casa 'ss' etc. nas _norm;
        # *_lower seguem intocadas. Alinha com o `_norm` do decisions_schema e
        # com o repair do tradutor (`normalize_norm_literals`). Honestidade
        # (review pré-push): NÃO é 100% aditivo — um literal contendo ß salvo
        # pela UI na janela 35.17.0→35.19.1 deixa de casar (o detector do
        # editor corrige em 1 clique ao reabrir a regra); ß em regra pt-BR é
        # teórico e a janela foi de ~1 dia.
        "input_norm": _strip_accents(inp_lower.casefold()),
        "output_norm": _strip_accents(out_lower.casefold()),
        "text_norm": _strip_accents(text_all.casefold()),
        # Memória de conversa: perguntas recentes do usuário (já em text_all;
        # exposta isolada p/ exprs que queiram olhar só o histórico).
        "session_text": sess_text,
        # Args SELADOS `x-uso: param` (Postura B — roteamento determinístico): valores
        # EXATOS do caller, disponíveis como `inputs.<campo>` na expr. Sobrevivem intactos
        # à cadeia (não passam por LLM), então uma regra pode ramificar por VALOR sem
        # roteador LLM. Campo ausente → sentinel comparação-seguro (não casa em NENHUM
        # operador, inclusive `>`/`<`). dict-view → acesso `inputs.tier`.
        "inputs": _ArgsView(inputs or {}),
        # Contrato de Decisão (Cond-C, 35.19.0): decisões ANUNCIADAS pelo agente
        # anterior via linha `DECISAO:` — já validadas contra o ## Decisions da
        # skill dele (grafia CANÔNICA do schema). Campo ausente/linha ausente →
        # sentinel comparação-seguro (regra não casa, nunca estoura). Acesso
        # `decision.escalar == 'sim'`.
        "decision": _ArgsView(decision or {}),
    }


# Metadata declarativa das vars — consumida pela UI do wizard (vars panel
# com descrição + exemplo). Mantém ao lado de _build_conditional_context
# para não dar drift.
CONDITIONAL_VARS_META: list[dict] = [
    {"name": "output", "type": "str", "desc": "Texto completo da resposta do agente anterior"},
    {"name": "output_lower", "type": "str", "desc": "A resposta do agente anterior em letras minúsculas (para buscar sem diferenciar maiúscula de minúscula)"},
    {"name": "output_length", "type": "int", "desc": "Quantos caracteres tem a resposta do agente anterior"},
    {"name": "has_output", "type": "bool", "desc": "Verdadeiro se a resposta do agente anterior tem conteúdo"},
    {"name": "final_state", "type": "str", "desc": "Decisão final do agente anterior: Recommend (recomendar), Refuse (recusar), Escalate (escalar) ou LogAndClose (registrar e fechar)"},
    {"name": "is_recommend", "type": "bool", "desc": "Verdadeiro se a decisão foi recomendar — forma curta de escrever a regra"},
    {"name": "is_refuse", "type": "bool", "desc": "Verdadeiro se a decisão foi recusar — forma curta"},
    {"name": "is_escalate", "type": "bool", "desc": "Verdadeiro se a decisão foi escalar — forma curta"},
    {"name": "contains_image", "type": "bool", "desc": "Verdadeiro se a resposta menciona uma imagem (.jpg, .png, .webp ou a palavra 'imagem')"},
    {"name": "contains_url", "type": "bool", "desc": "Verdadeiro se a resposta contém um link (http:// ou https://)"},
    {"name": "contains_pdf", "type": "bool", "desc": "Verdadeiro se a resposta menciona um PDF"},
    {"name": "lines_count", "type": "int", "desc": "Quantas linhas tem a resposta do agente anterior"},
    {"name": "input", "type": "str", "desc": "A pergunta original do usuário — útil para decidir qual agente responde conforme o que foi perguntado"},
    {"name": "input_lower", "type": "str", "desc": "A pergunta original em letras minúsculas (para buscar sem diferenciar maiúscula de minúscula)"},
    {"name": "text_all", "type": "str", "desc": "Tudo junto: a pergunta, os nomes/tipos dos arquivos enviados e as conversas recentes (em minúsculas) — para achar uma palavra em qualquer parte"},
    {"name": "input_norm", "type": "str", "desc": "A pergunta em minúsculas E SEM ACENTO — 'nao reconheco' acha 'não reconheço'. Prefira esta a input_lower para não precisar escrever a palavra com e sem acento."},
    {"name": "output_norm", "type": "str", "desc": "A resposta do agente anterior em minúsculas E sem acento — mesma ideia de input_norm, para casar palavra sem se preocupar com acento."},
    {"name": "text_norm", "type": "str", "desc": "Tudo junto (pergunta + arquivos + histórico) em minúsculas E sem acento — a forma mais tolerante de achar uma palavra."},
    {"name": "session_text", "type": "str", "desc": "Perguntas anteriores da conversa — ajuda a entender pedidos curtos como 'fala mais sobre isso' procurando a palavra no que já foi perguntado"},
    {"name": "has_attachments", "type": "bool", "desc": "Verdadeiro se o usuário enviou algum arquivo"},
    {"name": "has_document", "type": "bool", "desc": "Verdadeiro se há um documento entre os arquivos (PDF, Word, PowerPoint, Excel, texto, etc.)"},
    {"name": "has_image", "type": "bool", "desc": "Verdadeiro se há uma imagem entre os arquivos (JPG, PNG, GIF, WebP, etc.)"},
    {"name": "attachment_names", "type": "str", "desc": "Os nomes de todos os arquivos juntos, em minúsculas — ex.: 'relatorio.pdf foto.png'"},
    {"name": "attachment_exts", "type": "str", "desc": "O tipo dos arquivos pela extensão, em minúsculas — ex.: 'pdf png'"},
    {"name": "attachment_types", "type": "str", "desc": "O tipo técnico de cada arquivo, em minúsculas — ex.: 'application/pdf image/png'"},
    {"name": "attachment_count", "type": "int", "desc": "Quantos arquivos o usuário enviou"},
    {"name": "inputs", "type": "dict", "desc": "Os parâmetros EXATOS enviados na chamada (campos marcados como 'exato' no formulário). Use como inputs.<campo> — ex.: inputs.tier == 'gold'. Chega intacto (sem passar pela IA), então a regra decide o caminho por VALOR, sem depender de um agente de IA para rotear."},
    {"name": "decision", "type": "dict", "desc": "As decisões que o agente anterior ANUNCIOU na linha 'DECISAO:' da resposta, já conferidas contra o contrato (## Decisions) da skill dele. Use como decision.<campo> — ex.: decision.escalar == 'sim'. Só existe se a skill do agente anterior declarar o Contrato de Decisão; campo ausente simplesmente não casa."},
]


def _eval_conditional(expr: str, ctx: dict) -> bool:
    """Avalia uma expressão Jinja booleana contra `ctx`.

    Variáveis disponíveis vêm de `_build_conditional_context()` — ver
    docstring lá.

    Em erro de sintaxe ou execução, levanta — caller decide fail-open vs
    fail-closed.
    """
    env = _get_conditional_jinja_env()
    return bool(env.compile_expression(expr)(**ctx))


def _output_names_target(output: str | None, target_name: str | None) -> bool:
    r"""True se o texto do agente upstream NOMEIA explicitamente o agente alvo.

    Racional (2026-06-06 — bug "AR roteou certo mas o SA foi pulado"): o
    roteamento condicional fan-out tem DOIS decisores que podem discordar:

    1. o LLM do roteador/orquestrador, que decide *semanticamente* e escreve
       p.ex. "Encaminhar a pergunta ao agente **Rentab**."; e
    2. o gate de keywords da `expr`, que casa contra a PERGUNTA do usuário
       (`input_lower`).

    Quando o usuário usa vocabulário que não bate as keywords (ex.: a pergunta
    "como gerar **receita**" → nenhuma keyword de Rentab casa), o gate burro
    PULAVA o alvo que o roteador acabara de nomear — o oposto do esperado.

    A decisão EXPLÍCITA do roteador é a autoridade do roteamento: se o texto
    dele cita o nome do alvo, honramos (NÃO skipamos), independente da expr.
    É fail-safe (roda o agente que o roteador escolheu) e casa o modelo mental
    do operador ("o AR decidiu pelo SA → o SA deve responder").

    Robustez:
    - case- e acento-insensível (NFKD): "Retenção" casa "retencao"/"retenção";
    - fronteira de palavra (\b): "Rentab" casa "**Rentab**" mas NÃO casa dentro
      de "rentabilidade" — só o naming EXPLÍCITO dispara o override;
    - guard de nome curto (< 3 chars): nomes ambíguos (ex.: "A") casariam
      qualquer texto → não disparam; cai no fluxo normal da expr.
    """
    name = (target_name or "").strip().lower()
    if len(name) < 3:
        return False
    out = (output or "").lower()
    if not out:
        return False
    import re as _re
    import unicodedata as _ud

    def _no_accents(s: str) -> str:
        return "".join(
            ch for ch in _ud.normalize("NFKD", s) if not _ud.combining(ch)
        )

    pattern = r"\b" + _re.escape(_no_accents(name)) + r"\b"
    return _re.search(pattern, _no_accents(out)) is not None


# Cues de ROTEAMENTO (lowercase, sem acento): sinais de que o upstream está
# DIRECIONANDO a um agente nomeado, não só citando a palavra em prosa. Verbo-cues
# casam por prefixo + \w* ("encaminh" → encaminhar/encaminhe); noun-cues
# ("agente/subagente/especialista") cobrem "ao agente X"; setas cobrem "→ X".
# Tudo ancorado em \b para NÃO casar dentro de outra palavra (ex.: "acion" não
# dispara em "nacional"). Consumido por _output_routes_to_target.
_ROUTING_CUE_PATTERN = (
    r"(?:"
    r"\b(?:encaminh|reencaminh|rotea|redirecion|direcion|deleg|acion|invoc"
    r"|chama|despach|repass)\w*"
    r"|\bagente\b|\bsubagente\b|\bespecialista\b"
    r"|->|→|»"
    r")"
)


def _output_routes_to_target(output: str | None, target_name: str | None) -> bool:
    r"""True se o upstream NOMEIA o alvo EM CONTEXTO DE ROTEAMENTO.

    Camada mais estrita que `_output_names_target` (2026-06-06 — bug "Doc Analise
    → Imagem"): exige, ALÉM da menção do nome (mesma fronteira de palavra/acento),
    que um CUE de roteamento apareça imediatamente ANTES do nome (janela ~40
    chars, mesma linha) — verbo ("encaminhar/rotear/delegar/direcionar/acionar…"),
    substantivo de destino ("agente/subagente/especialista") ou seta ("->"/"→").

    Por quê: o override "o roteador mandou" (`_should_skip_conditional`) passou a
    disparar em FALSO quando o agente tem nome de palavra comum ("Imagem",
    "Documentos") e o roteador produz uma RESPOSTA (não um token de roteamento).
    No bug "Doc Analise", o roteador RESUMIA um .pptx cujo texto contém a palavra
    "imagem" (descrevendo figuras: '(imagem "Dinheiro…")') — `_output_names_target`
    casava e rodava o SA "Imagem" sobre um documento. Restringir ao contexto de
    roteamento mata o falso-positivo e preserva o caso legítimo ("ao agente X").
    """
    if not _output_names_target(output, target_name):
        return False
    import re as _re
    import unicodedata as _ud

    def _no_accents(s: str) -> str:
        return "".join(
            ch for ch in _ud.normalize("NFKD", s) if not _ud.combining(ch)
        )

    name = _no_accents((target_name or "").strip().lower())
    out = _no_accents((output or "").lower())
    pattern = _ROUTING_CUE_PATTERN + r"[^\n]{0,40}?\b" + _re.escape(name) + r"\b"
    return _re.search(pattern, out) is not None


def _norm_routing_name(s: str | None) -> str:
    """Normaliza um nome de agente p/ comparação de roteamento: trim, lowercase
    e sem acentos (NFKD). Diferente de `_output_names_target` (fronteira de
    palavra em prosa), aqui o uso é IGUALDADE EXATA do campo `target` estruturado.
    """
    import unicodedata as _ud

    s = (s or "").strip().lower()
    return "".join(ch for ch in _ud.normalize("NFKD", s) if not _ud.combining(ch))


def _extract_routed_target(output: str | None) -> str | None:
    """Extrai o `target` de um bloco estruturado ``{"target": X, "inputs": {...}}``
    emitido pelo roteador (Fase B — #316). Retorna a string `target` (não-vazia)
    ou None se não houver objeto parseável com `target` string.

    Varre na MESMA ordem de `_extract_inputs_from_text` (bloco cercado
    ```json {...}``` → primeiro objeto {...} inline), pra NÃO divergir do que o
    SA consome do outro lado da cadeia. Tolerante: prosa sem bloco → None.
    """
    import re as _re

    t = output or ""
    candidates: list[str] = []
    fenced = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, _re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    inline = _re.search(r"(\{.*\})", t, _re.DOTALL)
    if inline:
        candidates.append(inline.group(1))
    for c in candidates:
        try:
            d = json.loads(c)
        except Exception:
            continue
        if isinstance(d, dict):
            tgt = d.get("target")
            if isinstance(tgt, str) and tgt.strip():
                return tgt.strip()
    return None


def _preserve_decision_line(*, original: str, truncated: str, schema: dict | None) -> str:
    """Pós-truncate do Output Shape: a linha `DECISAO:` vive no FIM da resposta —
    o hard-cut a mataria e toda regra `decision.*` do gate viraria falso-negativo
    silencioso. Delega ao helper canônico do parser, que compara a EXTRAÇÃO
    validada do original vs truncado (o guard antigo `has_decision_line` falhava
    no corte NO MEIO da linha — campos amputados em silêncio, review 2026-07-15)."""
    return preserve_decision_line(original, truncated, schema)


async def _decisions_schema_for_agent(agent_id: str) -> Optional[dict]:
    """Schema `## Decisions` da skill do agente, memoizado por requisição de
    pipeline (mesmo contextvar do `_topo_agent`): um fan-out de N arestas
    condicionais paga o find_by_id + parse UMA vez, não N (review 2026-07-15).
    Fora de pipeline (cache None) resolve direto. None = sem contrato."""
    agent = await _topo_agent(agent_id)
    skill_id = (agent or {}).get("skill_id")
    if not skill_id:
        return None
    cache = _pipeline_topo.get()
    memo = cache.setdefault("decisions_schema", {}) if cache is not None else None
    if memo is not None and skill_id in memo:
        return memo[skill_id]
    row = await skills_repo.find_by_id(skill_id)
    schema = extract_decisions_schema((row or {}).get("raw_content") or "")
    if memo is not None:
        memo[skill_id] = schema
    return schema


async def _decision_vars_for_source(source_id: str, last_output: str) -> dict:
    """Decisões ANUNCIADAS pelo agente `source_id` em `last_output`, validadas
    contra o `## Decisions` da skill dele (Cond-C, 35.19.0). Grafia canônica.

    Barato por construção: sem linha `DECISAO:` no output (caso comum) retorna
    {} SEM tocar o banco. Com linha, resolve agente + schema (ambos memoizados
    por pipeline) e valida — campo/valor fora do contrato caem fora (o contrato
    é selado; regra `decision.x` só vê o que a skill declarou).
    Fail-safe: qualquer erro → {} (a regra decision.* não casa; demais vars da
    expr seguem intactas)."""
    if not has_decision_line(last_output):
        return {}
    try:
        schema = await _decisions_schema_for_agent(source_id)
        if not schema:
            return {}
        decision = extract_decision_line(last_output, schema)
        if not decision:
            # Linha PRESENTE mas nada validou — ex.: agente respondendo noutro
            # idioma traduziu os valores ('escalar=yes') apesar da diretiva
            # verbatim. Sem este log o falso-negativo do gate seria 100%
            # silencioso e indiagnosticável em produção (review 2026-07-15).
            # SEM trecho do output no log: o fim da resposta costuma citar dados
            # do cliente e logs/ ficam FORA do forget LGPD (review pré-push).
            logger.warning(
                "mesh.conditional.decision_line_invalid",
                extra={
                    "event": "mesh.conditional",
                    "source_id": source_id,
                    "schema_fields": sorted(schema.keys()),
                    "output_len": len(last_output or ""),
                },
            )
        return decision
    except Exception as e:
        logger.warning(
            "mesh.conditional.decision_extract_failed",
            extra={
                "event": "mesh.conditional",
                "source_id": source_id,
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return {}


async def evaluate_test_phrases_for_edge(*, source_id: str, expr: str, phrases: list) -> list[dict]:
    """Avalia as Frases-Prova de uma aresta condicional com o MESMO avaliador
    do runtime (`_build_conditional_context` + `_eval_conditional`; `decision.*`
    extraída do output simulado do source quando a frase é de resposta) — o
    mesmo escopo do simulador do editor, então o veredito do publish nunca
    diverge do badge que o autor viu. Insumo do gate de publicação (36.0.0).

    ESCOPO (review pré-push): a frase prova a REGRA da aresta, não a decisão
    de skip completa — os overrides do runtime ("o roteador nomeou o alvo",
    "o anexo manda") rodam ANTES da expr e podem executar uma aresta cuja
    frase expect=pular passou aqui. É o mesmo ponto cego do simulador.

    Retorna [{text, where, expect, got, passed, error}] por frase. Política
    fail-CLOSED por frase: erro de avaliação — e frase MALFORMADA (item
    não-dict vindo de config gravada via API crua) — conta como reprovada
    com o erro anexado; um item podre não pode desligar o gate inteiro."""
    results: list[dict] = []
    expr = (expr or "").strip()
    for p in phrases or []:
        if not isinstance(p, dict):
            results.append({
                "text": str(p)[:80], "where": "input", "expect": True,
                "got": None, "passed": False,
                "error": "frase malformada no config da aresta (esperado objeto {text, where, expect})",
            })
            continue
        text = str(p.get("text") or "").strip()
        if not text:
            continue
        where = "output" if p.get("where") == "output" else "input"
        expect = p.get("expect") is not False
        row = {"text": text, "where": where, "expect": expect, "got": None, "passed": False, "error": ""}
        try:
            if not expr:
                # Condicional SEM expr nunca skipa no runtime (equivale a
                # sequencial) — a frase é provada contra essa semântica.
                got = True
            else:
                decision = await _decision_vars_for_source(source_id, text) if where == "output" else {}
                ctx = _build_conditional_context(
                    output=text if where == "output" else "",
                    final_state="Recommend",
                    user_input=text if where == "input" else "",
                    decision=decision,
                )
                got = bool(_eval_conditional(expr, ctx))
            row["got"] = got
            row["passed"] = got == expect
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        results.append(row)
    return results


async def _decision_from_steps(steps: list) -> Optional[dict]:
    """Fallback do envelope (36.1.0): a decisão mais RECENTE anunciada na
    cadeia. Na topologia mais comum (triagem COM contrato decide → especialista
    SEM contrato responde) o dono da resposta não anuncia nada — mas o sinal
    útil ao consumidor é a decisão que ROTEOU. Os steps guardam o output CRU
    (com a linha); schemas memoizados por pipeline tornam a varredura barata."""
    for s in reversed(steps or []):
        if (s or {}).get("status") != "completed":
            continue
        out = s.get("output") or ""
        if not has_decision_line(out):
            continue
        dec = await extract_decision_for_agent(out, s.get("agent_id") or "")
        if dec:
            return dec
    return None


async def extract_decision_for_agent(output: str, agent_id: str) -> Optional[dict]:
    """Decisão ESTRUTURADA anunciada em `output` pelo agente, validada contra o
    `## Decisions` da skill dele — para o ENVELOPE do invoke (36.1.0, backlog
    do arco condicional): o consumidor MÁQUINA (X-API-Key) recebe
    `decision: {campo: valor}` em vez de parsear a linha do texto (que a
    camada de apresentação remove). None = sem contrato / sem linha / nada
    válido. Fail-safe: erro → None (o envelope segue sem o campo preenchido)."""
    if not output or not agent_id or not has_decision_line(output):
        return None
    try:
        schema = await _decisions_schema_for_agent(agent_id)
        if not schema:
            return None
        return extract_decision_line(output, schema) or None
    except Exception:
        return None


async def decision_and_display_output(output: str, agent_id: str) -> tuple:
    """(decision, output_para_exibição) com UMA resolução de schema — fora de
    pipeline o contextvar de memoização é None, então extract+strip separados
    pagavam 4 lookups por invoke com linha (review 36.1.0). Fail-safe:
    (None, output intacto)."""
    if not output or not agent_id or not has_decision_line(output):
        return None, output
    try:
        schema = await _decisions_schema_for_agent(agent_id)
        if not schema:
            return None, output
        return (extract_decision_line(output, schema) or None), strip_decision_line(output, schema)
    except Exception:
        return None, output


async def _strip_for_display_multi(output: str, agent_ids: list) -> str:
    """Regime de APRESENTAÇÃO com fallback de ECO: tenta o schema de cada
    agente (autor primeiro, depois os demais da cadeia) até a linha DECISAO
    sumir — um agente SEM contrato pode ecoar a linha do upstream que veio no
    contexto (review pré-push 36.2.1: a assimetria vazava a linha no step).
    Lookups memoizados por pipeline; fail-safe: erro → segue tentando/cru."""
    out = output or ""
    if not has_decision_line(out):
        return out
    seen: set = set()
    for aid in agent_ids or []:
        if not aid or aid in seen:
            continue
        seen.add(aid)
        try:
            sch = await _decisions_schema_for_agent(aid)
            if sch:
                out = strip_decision_line(out, sch)
        except Exception:
            continue
        if not has_decision_line(out):
            break
    return out


async def _display_preview(output: str, agent_id: str, limit: int = 300, fallback_agent_ids: list | None = None) -> str:
    """Preview do output para EVENTOS de stream (`agent_done`): a linha DECISAO
    é protocolo de máquina e não aparece ao vivo (Backlog 4, 36.2.1) — uma
    resposta curta de classificador cabe inteira nos 300 chars, linha inclusa.
    steps/trace seguem CRUS; o gate lê o output completo. Eco coberto via
    `fallback_agent_ids` (agentes anteriores da cadeia)."""
    out = await _strip_for_display_multi(output or "", [agent_id, *(fallback_agent_ids or [])])
    return out[:limit]


async def strip_decision_line_for_display(output: str, agent_id: str) -> str:
    """`strip_decision_line` com resolução do contrato do agente (Cond-C).

    Para as superfícies de apresentação SINGLE-AGENT (/agents/invoke, chat do
    workspace) — o pipeline aplica o strip na montagem do resultado final em
    `execute_pipeline`. NUNCA usar sobre o output que alimenta gate/contexto
    downstream (a linha é o insumo do `decision.*`). Fail-safe: erro → intacto."""
    if not output or not agent_id or not has_decision_line(output):
        return output
    try:
        schema = await _decisions_schema_for_agent(agent_id)
        if not schema:
            return output
        return strip_decision_line(output, schema)
    except Exception:
        return output


async def _should_skip_conditional(
    *,
    source_id: str,
    target_id: str,
    last_output: str,
    last_final_state: str,
    user_input: str = "",
    target_name: str = "",
    attachments: list | None = None,
    session_text: str = "",
    target_accepts_documents: bool = False,
    target_accepts_images: bool = False,
    inputs: dict | None = None,
) -> bool:
    """True se a conexão source→target é `connection_type=conditional` e
    a expressão configurada em `config.expr` avaliou para `False`.

    Override "o roteador mandou" (2026-06-06; restrito + vetado 2026-06-06): se
    o upstream NOMEIA este alvo EM CONTEXTO DE ROTEAMENTO (ver
    `_output_routes_to_target` — verbo/"agente"/seta antes do nome, não só prosa)
    E o alvo NÃO está vetado por capacidade (anexo de tipo que ele não trata),
    a decisão do roteador vence o heurístico de keywords e NÃO skipamos. Corrige
    o caso em que o AR/AOBD roteia certo mas o vocabulário não bate a expr — sem
    o falso-positivo do bug "Doc Analise → Imagem" (nome de palavra comum, como
    "Imagem", citado na prosa do roteador ao resumir um documento).

    Override "o anexo manda" (2026-06-06 — router como dispatcher): se o alvo
    DECLARA `accepts_documents`/`accepts_images` e chega um anexo do tipo
    declarado, NÃO skipamos — independente da expr. Um especialista de documentos
    não pode ser pulado quando o usuário soltou um documento (a causa-raiz do bug
    "Doc Analise"). Ver `_target_handles_attachment`. Defaults False → opt-in:
    só dispara para handlers explícitos, comportamento legado preservado.

    Política de erro: **fail-open** — qualquer falha (config malformado,
    expr inválida, exception no Jinja) loga warning e devolve `False`
    (NÃO skipa). É melhor executar o agente que perder dados por bug
    de regra. Operador vê o warning em errors.log e corrige.
    """
    conns = await _topo_mesh_out(source_id, limit=20)
    conn = next((c for c in conns if c.get("target_agent_id") == target_id), None)
    if not conn or conn.get("connection_type") != "conditional":
        return False

    # ── Override "target estruturado" (Fase B — 2026-06-07): AUTORITATIVO ──
    # Se o upstream emitiu o bloco {"target": X, "inputs": {...}} (roteador da
    # Fase B, #316 — o MESMO que o SA consome via _extract_inputs_from_text/#315),
    # ESSA é a decisão de roteamento: DETERMINÍSTICA e EXCLUSIVA.
    #   • X casa este alvo  → NÃO skipa (roda), ignorando a expr de keyword;
    #   • X nomeia OUTRO    → SKIPA este (o roteador elegeu um só — 1-de-N real).
    # Supera a heurística de NL-cue/keyword (que vira fallback p/ meshes SEM bloco
    # estruturado). Precede tudo de propósito: é o sinal mais explícito que existe.
    # Inerte quando não há bloco (`_extract_routed_target` → None) → preserva 100%
    # do comportamento legado (nenhum teste antigo emite esse bloco). Casamento de
    # nome é case/acento-insensível, igualdade EXATA do campo (`_norm_routing_name`).
    routed = _extract_routed_target(last_output)
    if routed is not None:
        chosen = _norm_routing_name(routed) == _norm_routing_name(target_name)
        logger.info(
            "mesh.conditional.structured_target",
            extra={
                "event": "mesh.conditional",
                "source_id": source_id,
                "target_id": target_id,
                "target_name": target_name,
                "routed_target": routed,
                "decision": "run_not_skip" if chosen else "skip_not_chosen",
            },
        )
        return not chosen

    # ── Override "o roteador mandou" (2026-06-06; restrito + vetado 2026-06-06) ──
    # Honra a decisão EXPLÍCITA do roteador (ex.: o AR responde "Encaminhar ao
    # agente Rentab") quando o vocabulário da pergunta não bate a expr. DUAS
    # salvaguardas contra o falso-positivo do bug "Doc Analise → Imagem":
    #   1) CONTEXTO DE ROTEAMENTO (_output_routes_to_target): o nome só conta se
    #      vier após um cue de roteamento (verbo/"agente"/seta), não em prosa —
    #      o roteador resumindo um doc que contém a palavra "imagem" não roteia.
    #   2) VETO DE CAPACIDADE: se há anexo e o alvo NÃO trata aquele tipo
    #      (ex.: SA de imagem + documento), o naming NÃO vence — cai na expr
    #      (provável skip). Capacidade é autoridade: não rodar handler errado.
    if _output_routes_to_target(last_output, target_name):
        if attachments and not _target_handles_attachment(
            accepts_documents=target_accepts_documents,
            accepts_images=target_accepts_images,
            attachments=attachments,
        ):
            logger.info(
                "mesh.conditional.router_named_target_vetoed",
                extra={
                    "event": "mesh.conditional",
                    "source_id": source_id,
                    "target_id": target_id,
                    "target_name": target_name,
                    "decision": "vetoed_capability_mismatch",
                    "reason": "capability_mismatch",
                },
            )
            # cai para a avaliação da expr abaixo — não honra o naming.
        else:
            logger.info(
                "mesh.conditional.router_named_target",
                extra={
                    "event": "mesh.conditional",
                    "source_id": source_id,
                    "target_id": target_id,
                    "target_name": target_name,
                    "decision": "run_not_skip",
                },
            )
            return False

    # ── Override "o anexo manda" (2026-06-06 — router como dispatcher) ──
    # Se este alvo DECLARA capacidade para o tipo de anexo presente
    # (accepts_documents + documento, ou accepts_images + imagem), NÃO skipa,
    # qualquer que seja a expr. Corrige o bug "Doc Analise": SA de documentos
    # pulado quando o usuário soltou um documento mas o texto digitado não tinha
    # keyword. É a autoridade de CAPACIDADE — ver _target_handles_attachment.
    if _target_handles_attachment(
        accepts_documents=target_accepts_documents,
        accepts_images=target_accepts_images,
        attachments=attachments,
    ):
        logger.info(
            "mesh.conditional.target_handles_attachment",
            extra={
                "event": "mesh.conditional",
                "source_id": source_id,
                "target_id": target_id,
                "target_name": target_name,
                "decision": "run_not_skip",
                "reason": "capability",
            },
        )
        return False

    cfg = conn.get("config") or "{}"
    try:
        cfg_dict = json.loads(cfg) if isinstance(cfg, str) else cfg
    except (ValueError, TypeError) as e:
        logger.warning(
            "mesh.conditional.bad_config",
            extra={
                "event": "mesh.conditional",
                "source_id": source_id,
                "target_id": target_id,
                "config_preview": str(cfg)[:200],
                "error_type": type(e).__name__,
            },
        )
        return False

    expr = ((cfg_dict or {}).get("expr") or "").strip()
    if not expr:
        # Condicional sem expr = sempre passa (operador escolheu o tipo
        # mas não definiu a regra — equivalente a sequencial).
        return False

    try:
        result = _eval_conditional(
            expr,
            _build_conditional_context(
                output=last_output,
                final_state=last_final_state,
                user_input=user_input,
                attachments=attachments,
                session_text=session_text,
                inputs=inputs,
                # Contrato de Decisão (35.19.0): decisões anunciadas pelo source
                # na linha DECISAO:, validadas contra a skill dele. Gate lexical
                # (review 2026-07-15): a extração paga lookup de skill + parse —
                # só vale quando a expr USA decision.*. Substring é fail-safe:
                # falso-positivo custa só o lookup; `decision.x` sempre contém
                # "decision", então nunca há falso-negativo.
                decision=(
                    await _decision_vars_for_source(source_id, last_output)
                    if "decision" in expr else {}
                ),
            ),
        )
    except Exception as e:
        logger.warning(
            "mesh.conditional.eval_failed",
            extra={
                "event": "mesh.conditional",
                "source_id": source_id,
                "target_id": target_id,
                "expr": expr[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return False  # fail-open

    return not result


async def _should_skip_default(
    *,
    source_id: str,
    target_id: str,
    last_output: str,
    last_final_state: str,
    user_input: str = "",
    attachments: list | None = None,
    session_text: str = "",
    inputs: dict | None = None,
) -> bool:
    """True se a conexão source→target é `connection_type=default` (aresta
    catch-all / "else") E algum IRMÃO condicional do mesmo source disparou.

    Semântica do agente-padrão (2026-06-06 — bug "AR roteou para SA fora do
    mesh"): numa árvore fan-out 1-de-N, o operador pode marcar UM alvo como
    `default`. Ele é o "else" do roteamento: roda SOMENTE quando NENHUM irmão
    condicional casou. Assim a pergunta fora de escopo sempre cai num agente
    REAL (mata o dead-end do drift) em vez de ficar sem resposta ou o roteador
    citar um agente que nem está cabeado.

    Como o resolver da chain (BFS) ignora connection_type, o alvo default JÁ
    está na chain; este gate só decide SE ele roda. A avaliação é
    ORDEM-INDEPENDENTE: reavaliamos cada irmão condicional aqui (reusando
    `_should_skip_conditional`, que já honra o override "o roteador mandou")
    — não dependemos de o irmão ter rodado antes na chain.

    Política de erro: **fail-open** — qualquer falha NÃO skipa (roda o
    default). É melhor responder pelo else do que cair em silêncio.
    """
    conns = await _topo_mesh_out(source_id, limit=50)
    conn = next((c for c in conns if c.get("target_agent_id") == target_id), None)
    if not conn or conn.get("connection_type") != "default":
        return False

    # Irmãos condicionais do MESMO source (exclui o próprio default). Apenas
    # `conditional` conta como "ramo" — `sequential` é incondicional e roda
    # sempre, não é alternativa que o else substitua.
    siblings = [
        c
        for c in conns
        if c.get("connection_type") == "conditional"
        and c.get("target_agent_id") != target_id
    ]
    if not siblings:
        # default sem irmãos condicionais = roda sempre (equivale a sequencial).
        logger.info(
            "mesh.default.no_conditional_siblings",
            extra={
                "event": "mesh.default",
                "source_id": source_id,
                "target_id": target_id,
                "decision": "run_default",
            },
        )
        return False

    for sib in siblings:
        sib_target_id = sib.get("target_agent_id")
        sib_name = ""
        sib_accepts_doc = False
        sib_accepts_img = False
        try:
            sib_agent = await _topo_agent(sib_target_id)
            if sib_agent:
                sib_name = sib_agent.get("name", "")
                # Capability do irmão p/ o override "o anexo manda" propagar aqui:
                # se um irmão é handler do anexo presente, ele "casa" → o default
                # (else) não roda (o especialista responde). Ver _should_skip_conditional.
                sib_accepts_doc = bool(sib_agent.get("accepts_documents") or 0)
                sib_accepts_img = bool(sib_agent.get("accepts_images") or 0)
        except Exception:
            sib_name = ""  # fail-open: sem nome, o override de roteador não dispara

        sib_skip = await _should_skip_conditional(
            source_id=source_id,
            target_id=sib_target_id,
            last_output=last_output,
            last_final_state=last_final_state,
            user_input=user_input,
            target_name=sib_name,
            attachments=attachments,
            session_text=session_text,
            target_accepts_documents=sib_accepts_doc,
            target_accepts_images=sib_accepts_img,
            inputs=inputs,
        )
        if not sib_skip:
            # um irmão condicional disparou → o default (else) NÃO roda.
            logger.info(
                "mesh.default.sibling_matched",
                extra={
                    "event": "mesh.default",
                    "source_id": source_id,
                    "target_id": target_id,
                    "matched_sibling_id": sib_target_id,
                    "decision": "skip_default",
                },
            )
            return True

    # nenhum irmão condicional casou → roda o default (else).
    logger.info(
        "mesh.default.no_sibling_matched",
        extra={
            "event": "mesh.default",
            "source_id": source_id,
            "target_id": target_id,
            "decision": "run_default",
        },
    )
    return False


# ─── Context Scope (2026-06-01) ───
# Inherit/Scoped/Isolated propagation control entre nós da mesh chain.
# Funciona em complemento ao conditional routing: conditional decide SE
# o agente downstream executa; scope decide QUE PARTE do output anterior
# vira contexto pra ele. Persistido em `mesh_connections.config.context_scope`.
#
# Shape esperado de `config.context_scope` (parseado do JSON em config):
#   {
#     "mode": "inherit" | "scoped" | "isolated",
#     "template": "<jinja expression>",   # só p/ mode=scoped (precedência)
#     "max_chars": 500                     # só p/ mode=scoped (atalho)
#   }
#
# Política de erro: fail-OPEN — qualquer falha (config malformado, template
# inválido) loga warning + cai pra inherit. Mesma filosofia do conditional.

CONTEXT_SCOPE_MODES: tuple[str, ...] = ("inherit", "scoped", "isolated")

# Vars disponíveis no template Jinja do modo scoped — reusa as do conditional
# pra não fragmentar mental model do operador. Ver `_build_conditional_context`.
CONTEXT_SCOPE_VARS_META: list[dict] = CONDITIONAL_VARS_META


def _apply_context_scope_template(template: str, ctx: dict) -> str:
    """Avalia uma expressão Jinja contra `ctx` e retorna o resultado como str.

    Aceita expressão (ex.: `output[:200]`, `output | upper`,
    `output.split('\\n')[0]`) — usa `compile_expression` pra simetria total
    com `_eval_conditional` e reuso do mesmo ambiente sandboxed.

    Em erro de sintaxe ou execução, levanta — caller decide fail-open.
    """
    env = _get_conditional_jinja_env()
    result = env.compile_expression(template)(**ctx)
    if result is None:
        return ""
    return result if isinstance(result, str) else str(result)


async def _resolve_context_scope(
    *,
    source_id: str,
    target_id: str,
    last_output: str,
    last_final_state: str,
    user_input: str = "",
    attachments: list | None = None,
) -> dict:
    """Resolve a política de scope da conexão `source→target` e aplica-a
    ao `last_output`. Retorna dict com:

    - `mode`: 'inherit' | 'scoped' | 'isolated'
    - `output`: string a propagar adiante (vazia se isolated)
    - `skip_prefix`: bool — quando True, caller NÃO deve montar o prefix
      "## Contexto do agente anterior" (apenas user_input vai pro próximo)
    - `chars_before` / `chars_after`: int — telemetria

    Política fail-OPEN: erro em config/template → loga warning + retorna
    inherit (output original). É melhor over-share contexto que perder dado
    por bug de regra.
    """

    prev_len = len(last_output or "")
    inherit_result = {
        "mode": "inherit",
        "output": last_output or "",
        "skip_prefix": False,
        "chars_before": prev_len,
        "chars_after": prev_len,
    }

    try:
        conns = await _topo_mesh_out(source_id, limit=20)
    except Exception as e:
        logger.error(
            "mesh.context_scope.repo_lookup_failed",
            extra={
                "event": "mesh.context_scope",
                "source_id": source_id,
                "target_id": target_id,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return inherit_result

    conn = next((c for c in conns if c.get("target_agent_id") == target_id), None)
    if not conn:
        return inherit_result

    cfg = conn.get("config") or "{}"
    try:
        cfg_dict = json.loads(cfg) if isinstance(cfg, str) else cfg
    except (ValueError, TypeError) as e:
        logger.warning(
            "mesh.context_scope.bad_config",
            extra={
                "event": "mesh.context_scope",
                "source_id": source_id,
                "target_id": target_id,
                "config_preview": str(cfg)[:200],
                "error_type": type(e).__name__,
            },
        )
        return inherit_result

    scope_cfg = (cfg_dict or {}).get("context_scope")
    if not isinstance(scope_cfg, dict):
        return inherit_result

    mode = (scope_cfg.get("mode") or "inherit").strip().lower()
    if mode not in CONTEXT_SCOPE_MODES:
        logger.warning(
            "mesh.context_scope.invalid_mode",
            extra={
                "event": "mesh.context_scope",
                "source_id": source_id,
                "target_id": target_id,
                "mode": str(mode)[:50],
            },
        )
        return inherit_result

    if mode == "inherit":
        return inherit_result

    if mode == "isolated":
        logger.info(
            "mesh.context_scope.applied",
            extra={
                "event": "mesh.context_scope",
                "source_id": source_id,
                "target_id": target_id,
                "mode": "isolated",
                "chars_before": prev_len,
                "chars_after": 0,
                "reduction_pct": 100.0 if prev_len > 0 else 0.0,
            },
        )
        return {
            "mode": "isolated",
            "output": "",
            "skip_prefix": True,
            "chars_before": prev_len,
            "chars_after": 0,
        }

    # mode == "scoped"
    template = (scope_cfg.get("template") or "").strip()
    max_chars = scope_cfg.get("max_chars")
    if not template and isinstance(max_chars, int) and max_chars > 0:
        template = f"output[:{max_chars}]"
    if not template:
        # scoped sem template nem max_chars = inherit (operador escolheu
        # mas não definiu a regra — fail-open).
        return inherit_result

    try:
        scoped_output = _apply_context_scope_template(
            template,
            _build_conditional_context(
                output=last_output,
                final_state=last_final_state,
                user_input=user_input,
                attachments=attachments,
            ),
        )
    except Exception as e:
        logger.warning(
            "mesh.context_scope.eval_failed",
            extra={
                "event": "mesh.context_scope",
                "source_id": source_id,
                "target_id": target_id,
                "template": template[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
            exc_info=True,
        )
        return inherit_result  # fail-open

    chars_after = len(scoped_output)
    logger.info(
        "mesh.context_scope.applied",
        extra={
            "event": "mesh.context_scope",
            "source_id": source_id,
            "target_id": target_id,
            "mode": "scoped",
            "template_preview": template[:80],
            "chars_before": prev_len,
            "chars_after": chars_after,
            "reduction_pct": (
                round((1 - chars_after / prev_len) * 100, 1)
                if prev_len > 0 else 0.0
            ),
        },
    )
    return {
        "mode": "scoped",
        "output": scoped_output,
        "skip_prefix": False,
        "chars_before": prev_len,
        "chars_after": chars_after,
    }


async def _resolve_ordered_chain_with_parents(
    entry_agent_id: str, allowed_agent_ids: set | None = None
) -> tuple[list, dict]:
    """Resolve a cadeia ordenada downstream (BFS) **e** o mapa `parent_of`.

    Devolve `(chain, parent_of)`:
    - `chain`: agent_ids em ordem BFS a partir do entry.
    - `parent_of`: `{child_id: source_id}` — o agente que DESCOBRIU cada
      filho (a aresta real `source→child` que o trouxe à cadeia). O entry
      é raiz e não aparece como chave.

    `allowed_agent_ids` (Trilha A / PR-A1, opt-in): quando fornecido, a BFS só
    anda para targets DENTRO do conjunto — SELANDO a execução ao subgrafo do
    pipeline (membros), sem vazar para o mesh global. `None` (default) = BFS
    global, comportamento histórico → ZERO regressão p/ /invoke e workspace.

    Importante: ESTA função NÃO consulta `connection_type` — ela devolve
    a topologia completa (todos os possíveis filhos). A decisão de pular
    nós condicionais é tomada em runtime por `execute_pipeline` via
    `_should_skip_conditional`, contra o PARENT REAL (`parent_of`) e o
    output DELE — habilitando roteamento 1-de-N (fan-out / branch), não só
    cadeia linear.

    Invariante útil do BFS: como o source é visitado antes de qualquer
    filho, `parent_of[child]` sempre aponta pra um nó de índice MENOR na
    chain. Logo, ao processar um filho em runtime, o output do parent já
    foi computado. Em fan-out (1 source → N filhos), todos os N recebem o
    MESMO `parent_of` (o source comum) — e o gate de cada um avalia contra
    o output do source, não do irmão anterior na lista (que era o bug).
    """
    allowed = set(allowed_agent_ids) if allowed_agent_ids is not None else None
    chain = [entry_agent_id]
    visited = {entry_agent_id}
    queue = [entry_agent_id]
    parent_of: dict = {}
    while queue:
        current = queue.pop(0)
        conns = await _topo_mesh_out(current, limit=20)
        for conn in conns:
            tid = conn.get("target_agent_id", "")
            if not tid or tid in visited:
                continue
            if allowed is not None and tid not in allowed:
                continue  # selado: não sai do subgrafo do pipeline
            visited.add(tid)
            chain.append(tid)
            parent_of[tid] = current
            queue.append(tid)
    return chain, parent_of


async def _resolve_ordered_chain(entry_agent_id: str) -> list:
    """Compat: devolve só a chain BFS (sem `parent_of`).

    Mantida por retrocompatibilidade; delega a
    `_resolve_ordered_chain_with_parents`. Call-sites que precisam rotear
    contra o parent real devem usar a versão `_with_parents`.
    """
    chain, _ = await _resolve_ordered_chain_with_parents(entry_agent_id)
    return chain
