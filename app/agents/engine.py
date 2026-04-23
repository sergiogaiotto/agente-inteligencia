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

from app.core.llm_providers import get_provider
from app.core.observability import get_langfuse_handler, tracker
from app.core.database import (
    agents_repo, skills_repo, interactions_repo, turns_repo,
    envelopes_repo, audit_repo, car_repo,
)
from app.a2a.protocol import (
    Envelope, IntentDescriptor, Budget, ContextDelta,
    create_delegation_envelope, persist_envelope, apply_context_delta,
)
from app.agents.state_machine import (
    InteractionStateMachine, InteractionContext, State,
)
from app.evidence.runtime import retriever, reranker, evidence_checker, EvidenceResult
from app.skill_parser.parser import parse_skill_md

logger = logging.getLogger(__name__)


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

    def __init__(self, agent_config: dict, max_iterations: int = 3, mcp_tools: list = None):
        self.config = agent_config
        self.max_iterations = max_iterations
        self.provider = get_provider(
            agent_config.get("llm_provider", "openai"),
            model=agent_config.get("model"),
        )
        self.mcp_tools = mcp_tools or []
        self.openai_tools = []
        if self.mcp_tools:
            from app.mcp.runtime import build_openai_tools
            self.openai_tools = build_openai_tools(self.mcp_tools)

    def _build_system_prompt(self) -> str:
        """Constrói system prompt a partir do SKILL.md carregado.

        A seção de Ferramentas Disponíveis é colocada DEPOIS do Output Contract
        e inclui catálogo explícito com o nome exato (sanitizado) que o LLM
        deve usar no function call — isso evita que o modelo priorize fabricar
        o shape do Output Contract em vez de invocar a ferramenta.
        """
        import re as _re
        skill = self.config.get("_parsed_skill", {})
        parts = [
            self.config.get("system_prompt", "Você é um agente inteligente."),
        ]
        if skill.get("purpose"):
            parts.append(f"\n## Purpose\n{skill['purpose']}")
        if skill.get("workflow"):
            parts.append(f"\n## Workflow\n{skill['workflow']}")
        if skill.get("output_contract"):
            parts.append(f"\n## Output Contract\n{skill['output_contract']}")
        if skill.get("guardrails"):
            parts.append(f"\n## Guardrails\n{skill['guardrails']}")
        if self.mcp_tools:
            tool_catalog_lines: list[str] = []
            for t in self.mcp_tools:
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
            tool_catalog = "\n".join(tool_catalog_lines)

            parts.append(
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
                "**Como chamar**: use o function call com `operation` (uma das operações "
                "listadas acima) e `query` (a consulta ou parâmetros em string). "
                "Aguarde o retorno antes de gerar sua resposta final."
            )
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

    async def reason(self, state: AgentState) -> AgentState:
        """Nó de raciocínio com suporte a tool calling MCP."""
        system = self._build_system_prompt()
        messages = [SystemMessage(content=system)] + list(state["messages"])

        llm = self.provider.get_langchain_llm()
        handler = get_langfuse_handler(trace_name=f"agent_{self.config.get('id', 'x')}")
        callbacks = [handler] if handler else []

        # tool_choice="required" força invocação de QUALQUER função na
        # primeira chamada quando a SKILL mostra claramente a intenção
        # (Workflow com verbo de invocação ou nome da tool). Sem isso o
        # LLM frequentemente prefere fabricar o shape do Output Contract.
        # Se só uma tool está registrada, força ESSA tool especificamente.
        force_tool = self._should_force_tool_call()
        iteration = state.get("iteration", 0)
        first_pass = iteration == 0

        if self.openai_tools:
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
            llm_with_tools = llm

        response = await llm_with_tools.ainvoke(messages, config={"callbacks": callbacks})
        if self.openai_tools:
            _tc = getattr(response, "tool_calls", None) or []
            logger.info(
                f"LLM response: tool_calls={len(_tc)} "
                f"content_len={len(getattr(response, 'content', '') or '')}"
            )

        if self.openai_tools and hasattr(response, 'tool_calls') and response.tool_calls:
            from langchain_core.messages import ToolMessage
            from app.mcp.runtime import execute_tool_call

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

                    result_text = await execute_tool_call(
                        tool_name, tool_args, self.mcp_tools, timeout=30
                    )

                    logger.info(f"MCP Tool Result [round={round_n}, {len(result_text)}B]: {result_text[:200]}")

                    current_messages.append(ToolMessage(
                        content=result_text,
                        tool_call_id=tool_id,
                    ))

                response = await llm_with_tools_auto.ainvoke(current_messages, config={"callbacks": callbacks})
                current_messages.append(response)

            return {**state, "messages": [response], "iteration": state.get("iteration", 0) + 1}

        return {**state, "messages": [response], "iteration": state.get("iteration", 0) + 1}

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

async def execute_interaction(
    agent_id: str,
    user_input: str,
    session_id: str = None,
    channel: str = "api",
    journey: str = "",
    attachments: list = None,
    pipeline_context: str = None,
) -> dict:
    """Execução completa de uma interação pela FSM §15."""
    start = time.time()
    agent = await agents_repo.find_by_id(agent_id)
    if not agent:
        raise ValueError(f"Agente '{agent_id}' não encontrado.")

    from app.core.database import mesh_repo
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
            }

    agent["_parsed_skill"] = skill_data

    # ── Execution Profile: determina modo de execução ──
    exec_profile = skill_data.get("_execution_mode", "standard")

    attachment_context = ""
    attachment_meta = []
    if attachments:
        for att in attachments:
            attachment_meta.append({"name": att.get("name",""), "type": att.get("type",""), "size": att.get("size",0)})
            if att.get("content"):
                attachment_context += f"\n\n## Arquivo Anexo: {att.get('name','arquivo')}\n```\n{att['content'][:5000]}\n```"

    ctx = InteractionContext(agent_id=agent_id, journey=journey, channel=channel)
    fsm = InteractionStateMachine(ctx)
    await fsm.run_intake(user_input, agent_id, journey, channel)

    policy_ok = await fsm.run_policy_check({"allowed": True, "tools": [], "budget": {}})
    if not policy_ok:
        await fsm.run_refuse("Política de acesso negou a solicitação.", "Contate o administrador.")
        await fsm.run_log_and_close()
        return _build_result(ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta, agent=agent, skill_data=skill_data)

    skip_evidence = (
        not agent.get("require_evidence", True)
        or agent.get("require_evidence") == 0
        or exec_profile == "fast"
    )
    evidences = []
    if pipeline_context or skip_evidence:
        await fsm.run_retrieve_evidence([])
        enriched_input = user_input if not attachment_context else f"{user_input}{attachment_context}"
    else:
        evidences = await retriever.search(user_input, top_n=5)
        evidences = await reranker.rerank(user_input, evidences, top_n=5)
        await fsm.run_retrieve_evidence([asdict(e) if hasattr(e, '__dataclass_fields__') else e for e in evidences])

        evidence_context = "\n".join(
            f"[E{i+1}] {e.snippet_text}" for i, e in enumerate(evidences)
        ) if evidences else "Nenhuma evidência encontrada nas bases autorizadas."

        enriched_input = f"{user_input}{attachment_context}\n\n## Evidências Disponíveis\n{evidence_context}"

    from app.core.config import get_settings
    settings = get_settings()
    provider = agent.get("llm_provider", "openai")
    api_key = settings.openai_api_key if provider == "openai" else settings.maritaca_api_key
    if not api_key or api_key.startswith(("sk-your", "your-", "mrt-your", "change")):
        draft = (
            f"⚠ API Key do provedor '{provider}' não configurada.\n\n"
            f"Acesse Configurações → Plataforma e insira a API Key do {provider.upper()}.\n"
            f"Modelo selecionado: {agent.get('model', '?')}"
        )
        await fsm.run_draft_answer(draft)
        await fsm.run_verify_evidence({"ok": True, "confidence": 1.0})
        await fsm.run_recommend(draft)
        await fsm.run_log_and_close()
        return _build_result(ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta, agent=agent, skill_data=skill_data)

    mcp_tools = []
    mcp_tools_detail = []
    try:
        if skill_data.get("tool_bindings"):
            from app.mcp.runtime import parse_tool_bindings, match_with_registry
            from app.core.database import tools_repo
            parsed_bindings = parse_tool_bindings(skill_data["tool_bindings"])
            if parsed_bindings:
                mcp_tools = await match_with_registry(parsed_bindings, tools_repo)
                mcp_tools_detail = [{"name": t.get("name",""), "server": t.get("mcp_server",""), "ops": t.get("operations",[])} for t in mcp_tools]
                logger.info(f"MCP tools resolved: {[t.get('name') for t in mcp_tools]}")

        # Execution Profile: fast=1 (sem reflexão), standard=2, rigorous=3
        _max_iter = 1 if exec_profile == "fast" else (2 if exec_profile == "standard" else 3)
        harness = DeepAgentHarness(agent, max_iterations=_max_iter, mcp_tools=mcp_tools)
        graph = harness.build_graph()
        state = {
            "messages": [HumanMessage(content=enriched_input)],
            "current_agent": agent_id,
            "agent_kind": agent.get("kind", "subagent"),
            "iteration": 0,
            "max_iterations": _max_iter,
            "context": {},
            "envelope": {},
            "skill_data": skill_data,
            "metadata": {},
        }
        result = await graph.ainvoke(state)
        draft = result["messages"][-1].content if result["messages"] else ""
    except Exception as llm_err:
        err_str = str(llm_err)
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

    await fsm.run_draft_answer(draft)

    if pipeline_context or skip_evidence:
        await fsm.run_verify_evidence({"ok": True, "confidence": 1.0})
    elif exec_profile == "rigorous" and evidences:
        # Rigorous: verificação completa via LLM (Evidence Checker §14)
        verification = await evidence_checker.verify(draft, evidences, skill_data.get("guardrails", ""))
        await fsm.run_verify_evidence({
            "ok": verification.ok,
            "confidence": verification.confidence,
            "risk_high": verification.risk_high,
            "fraud_suspected": verification.fraud_suspected,
        })
    elif evidences:
        # Standard: verificação heurística (sem chamada LLM)
        avg_score = sum(e.relevance_score for e in evidences) / len(evidences)
        await fsm.run_verify_evidence({
            "ok": avg_score >= 0.3,
            "confidence": avg_score,
        })
    else:
        if skill_data.get("evidence_policy") and exec_profile == "rigorous":
            await fsm.run_verify_evidence({"ok": False, "confidence": 0.0})
        else:
            await fsm.run_verify_evidence({"ok": True, "confidence": 0.8})

    if ctx.current_state == State.RECOMMEND:
        await fsm.run_recommend(draft)
    elif ctx.current_state == State.REFUSE:
        await fsm.run_refuse("Evidência insuficiente para recomendação segura.")
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

    return _build_result(
        ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta,
        agent=agent, skill_data=skill_data, mcp_tools_detail=mcp_tools_detail,
    )


def _build_result(
    ctx: InteractionContext, start_time: float,
    mesh_chain: list = None, attachments: list = None,
    agent: dict = None, skill_data: dict = None,
    mcp_tools_detail: list = None,
) -> dict:
    """Constrói resultado enriquecido com detalhes de execução."""
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

    diagnostics = []
    if final == "Recommend":
        diagnostics.append({"level": "success", "text": "Recomendação entregue com evidência verificada"})
    elif final == "Refuse":
        diagnostics.append({"level": "warning", "text": "Recusa controlada — evidência insuficiente ou conflito de política"})
    elif final == "Escalate":
        diagnostics.append({"level": "danger", "text": "Escalado para supervisão humana — risco alto detectado"})

    if evidence_count == 0:
        diagnostics.append({"level": "info", "text": "Nenhuma evidência encontrada. Registre bases de conhecimento em Evidência para habilitar RAG."})
    elif ctx.evidence_score < 0.3:
        diagnostics.append({"level": "warning", "text": f"Score de evidência baixo ({ctx.evidence_score:.2f}). As bases de conhecimento podem não cobrir este tema."})
    elif ctx.evidence_score >= 0.7:
        diagnostics.append({"level": "success", "text": f"Evidência forte (score {ctx.evidence_score:.2f}). Boa cobertura pelas bases autorizadas."})

    if duration > 10000:
        diagnostics.append({"level": "warning", "text": f"Latência alta ({duration:.0f}ms). Considere modelo mais rápido ou reduzir max_iterations."})
    elif duration < 3000:
        diagnostics.append({"level": "success", "text": f"Resposta rápida ({duration:.0f}ms)."})

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
    system_prompt_summary = system_prompt_text[:800] if system_prompt_text else ""

    exec_log = _build_execution_log(
        agent=agent, skill_data=skill_data, skill_detail=skill_detail,
        mcp_tools_detail=mcp_tools_detail or [],
        transitions=ctx.transition_log, evidence_count=evidence_count,
        evidence_sources=evidence_sources, evidence_score=ctx.evidence_score,
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
        "trace": {
            "total_steps": total_steps,
            "evidence_count": evidence_count,
            "evidence_sources": evidence_sources,
            "diagnostics": diagnostics,
            "journey": ctx.journey or "—",
            "channel": ctx.channel,
            "mesh_chain": mesh_chain or [],
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
            "mcp_tools": mcp_tools_detail or [],
            "execution_log": exec_log,
        },
    }


def _build_execution_log(
    agent: dict, skill_data: dict, skill_detail: dict,
    mcp_tools_detail: list, transitions: list,
    evidence_count: int, evidence_sources: list,
    evidence_score: float, duration: float, final_state: str,
) -> list:
    """Constrói log de execução estruturado."""
    log = []

    def _add(category, icon, title, detail="", level="info"):
        log.append({"cat": category, "icon": icon, "title": title, "detail": detail, "level": level})

    kind_labels = {"aobd": "AOBD — Orquestrador", "router": "AR — Roteador", "subagent": "SA — Subagente"}
    _add("agent", "🤖", f"{agent.get('name', '?')}",
         f"{kind_labels.get(agent.get('kind',''), agent.get('kind',''))} · {agent.get('llm_provider','')}/{agent.get('model','')} · v{agent.get('version','1.0.0')}")
    if agent.get("domain"):
        _add("agent", "🏢", f"Domínio: {agent.get('domain')}")

    sp = agent.get("system_prompt", "")
    if sp:
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
    _mode_labels = {"fast": "Fast — 1 LLM call, sem reflexão, sem evidence check", "standard": "Standard — reflexão on-error, evidence heurística", "rigorous": "Rigorous — reflexão completa, evidence via LLM"}
    _mode_level = {"fast": "info", "standard": "info", "rigorous": "warning"}
    _add("skill", "⚡", f"Execution Profile: {_exec_mode}", _mode_labels.get(_exec_mode, ""), _mode_level.get(_exec_mode, "info"))

    if mcp_tools_detail:
        _add("tools", "🔧", f"{len(mcp_tools_detail)} ferramenta(s) MCP vinculada(s)")
        for t in mcp_tools_detail:
            ops = t.get("ops", [])
            if isinstance(ops, str):
                try: ops = json.loads(ops)
                except: ops = [ops]
            ops_str = ", ".join(ops) if ops else "—"
            _add("tools", "⚙️", t.get("name", "?"), f"Server: {t.get('server','')} · Ops: {ops_str}")
    else:
        _add("tools", "🔧", "Sem ferramentas MCP", "", "info")

    _add("fsm", "🔀", f"FSM — {len(transitions)} transição(ões)")
    for i, t in enumerate(transitions):
        _add("fsm", "→", f"{t.get('from','')} → {t.get('to','')}",
             t.get("condition", ""), "success" if t.get("to") in ("Recommend","LogAndClose") else "info")

    if evidence_count > 0:
        _add("evidence", "🔍", f"{evidence_count} evidência(s) encontrada(s)",
             f"Score: {evidence_score:.2f} · Fontes: {', '.join(evidence_sources[:5])}")
    else:
        _add("evidence", "🔍", "Sem evidências consultadas", "", "info")

    level = "success" if final_state in ("Recommend", "LogAndClose") else "warning" if final_state == "Refuse" else "danger"
    _add("result", "🏁", f"Resultado: {final_state}", f"Duração: {duration:.0f}ms", level)

    return log


async def _resolve_mesh_chain(agent_id: str, agent: dict) -> list:
    """Resolve a cadeia completa de agentes via AI Mesh usando BFS."""
    from app.core.database import mesh_repo, agents_repo

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
                src = await agents_repo.find_by_id(sid)
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
        conns = await mesh_repo.find_all(source_agent_id=current, limit=20)
        for conn in conns:
            tid = conn.get("target_agent_id", "")
            if tid and tid not in visited:
                visited.add(tid)
                tgt = await agents_repo.find_by_id(tid)
                if tgt:
                    downstream_chain.append({
                        "id": tid, "name": tgt.get("name",""), "kind": tgt.get("kind",""),
                        "model": tgt.get("model",""), "role": "downstream",
                        "connection": conn.get("connection_type","sequential"),
                    })
                    queue_down.append(tid)

    chain = upstream_chain + [entry] + downstream_chain
    return chain if len(chain) > 1 else []


async def execute_pipeline(
    entry_agent_id: str,
    user_input: str,
    channel: str = "api",
    attachments: list = None,
) -> dict:
    """Executa pipeline completo pelo AI Mesh.

    MELHORIA: Short-circuit para agentes pass-through.
    Agentes sem SKILL.md e com prompt genérico são ignorados (0ms),
    propagando o input diretamente ao próximo agente da cadeia.
    """
    start = time.time()
    entry_agent = await agents_repo.find_by_id(entry_agent_id)
    if not entry_agent:
        raise ValueError(f"Agente '{entry_agent_id}' não encontrado.")

    chain = await _resolve_ordered_chain(entry_agent_id)
    if not chain:
        chain = [entry_agent_id]

    steps = []
    current_input = user_input
    last_result = None
    master_interaction_id = None
    child_interaction_ids = []

    for i, agent_id in enumerate(chain):
        agent = await agents_repo.find_by_id(agent_id)
        if not agent or agent.get("status") != "active":
            steps.append({"agent_id": agent_id, "agent_name": agent.get("name","?") if agent else "?", "status": "skipped", "reason": "inativo"})
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
            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "agent_kind": agent.get("kind", ""),
                "agent_model": agent.get("model", ""),
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
                    "execution_log": [
                        {"cat": "agent", "icon": "⚡", "title": f"Pass-through: {agent.get('name','')}", "detail": f"Sem SKILL.md + prompt genérico → short-circuit (0ms). Kind: {agent.get('kind','')}.", "level": "info"},
                    ],
                },
            })
            # current_input e last_result permanecem inalterados
            # O próximo agente receberá o input original
            continue

        if i > 0 and last_result:
            current_input = (
                f"## Contexto do agente anterior ({steps[-1].get('agent_name','')}):\n"
                f"{last_result.get('output','')}\n\n"
                f"## Solicitação original:\n{user_input}"
            )

        try:
            result = await execute_interaction(
                agent_id=agent_id,
                user_input=current_input,
                channel=channel,
                attachments=attachments if i == 0 else None,
                pipeline_context=last_result.get("output","") if i > 0 and last_result else None,
            )
            iid = result.get("interaction_id")
            # Primeiro agente executado (não pass-through) vira o master
            if master_interaction_id is None:
                master_interaction_id = iid
            else:
                child_interaction_ids.append(iid)

            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name",""),
                "agent_kind": agent.get("kind",""),
                "agent_model": agent.get("model",""),
                "status": "completed",
                "output": result.get("output",""),
                "final_state": result.get("final_state",""),
                "duration_ms": result.get("duration_ms", 0),
                "evidence_score": result.get("evidence_score", 0),
                "transitions": result.get("transitions", []),
                "trace": result.get("trace"),
                "interaction_id": iid,
            })
            last_result = result
        except Exception as e:
            steps.append({
                "agent_id": agent_id,
                "agent_name": agent.get("name",""),
                "status": "error",
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
                import uuid as _uuid
                await turns_repo.create({
                    "id": str(_uuid.uuid4()),
                    "turn_number": turn_number,
                    "output_text_redacted": step["output"],
                    "interaction_id": master_interaction_id,
                })
                turn_number += 1

        for cid in child_interaction_ids:
            if cid:
                try:
                    child_turns = await turns_repo.find_all(interaction_id=cid, limit=100)
                    for ct in child_turns:
                        await turns_repo.delete(ct["id"])
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

    pipeline_mesh = [
        {
            "id": s.get("agent_id",""), "name": s.get("agent_name",""),
            "kind": s.get("agent_kind",""), "model": s.get("agent_model",""),
            "role": "entry_point" if idx==0 else "downstream",
            "connection": "sequential",
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

    return {
        "mode": "pipeline",
        "output": final_output,
        "pipeline_steps": steps,
        "total_agents": len(chain),
        "completed_agents": executed_count,
        "passthrough_agents": passthrough_count,
        "duration_ms": total_duration,
        "interaction_id": master_interaction_id,
        "final_state": steps[-1].get("final_state") if steps else None,
        "evidence_score": max((s.get("evidence_score",0) for s in steps), default=0),
        "transitions": steps[-1].get("transitions",[]) if steps else [],
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


async def _resolve_ordered_chain(entry_agent_id: str) -> list:
    """Resolve cadeia ordenada downstream a partir do agente de entrada (BFS)."""
    from app.core.database import mesh_repo
    chain = [entry_agent_id]
    visited = {entry_agent_id}
    queue = [entry_agent_id]
    while queue:
        current = queue.pop(0)
        conns = await mesh_repo.find_all(source_agent_id=current, limit=20)
        for conn in conns:
            tid = conn.get("target_agent_id", "")
            if tid and tid not in visited:
                visited.add(tid)
                chain.append(tid)
                queue.append(tid)
    return chain