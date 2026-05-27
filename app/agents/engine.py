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
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


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
    except (json.JSONDecodeError, ValueError):
        return None
    # JSON Schema válido tem que ser objeto com pelo menos type ou properties.
    if not isinstance(schema, dict):
        return None
    if not (schema.get("type") or schema.get("properties") or schema.get("$ref")):
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

    def __init__(self, agent_config: dict, max_iterations: int = 3, mcp_tools: list = None, interaction_id: str = ""):
        self.config = agent_config
        self.max_iterations = max_iterations
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
        )
        self.mcp_tools = mcp_tools or []
        self.openai_tools = []
        if self.mcp_tools:
            from app.mcp.runtime import build_openai_tools
            self.openai_tools = build_openai_tools(self.mcp_tools)
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
        # OpenAI espera nome no schema. Usa title do schema ou fallback genérico.
        name = (schema.get("title") or "SkillOutput")[:64]
        return {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "schema": schema,
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

                    _tc_start = time.time()
                    _tc_status = "completed"
                    try:
                        result_text = await execute_tool_call(
                            tool_name, tool_args, self.mcp_tools, timeout=30
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
        from app.mcp.runtime import execute_tool_call
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
                "_api_bindings_count": len(getattr(parsed, "api_bindings_parsed", []) or []),
                # Onda 6 Wave 2: evidence_policy estruturado (sources/limits/cite_sources)
                "_evidence_policy_parsed": getattr(parsed, "evidence_policy_parsed", {}) or {},
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
    evidences = []
    if pipeline_context or skip_evidence:
        await fsm.run_retrieve_evidence([])
        enriched_input = user_input if not attachment_context else f"{user_input}{attachment_context}"
    else:
        # Spans separados para retrieve e rerank — facilita identificar gargalo
        # (no Onda 3, search vai virar busca vetorial e o rerank um cross-encoder real).
        # Onda 6 Wave 2: skill pode declarar evidence_policy.sources pra restringir
        # quais fontes essa skill consulta. None = legacy (todas autorizadas);
        # [] = bloqueia tudo; populada = filtro estrito.
        _ev_policy = (skill_data.get("_evidence_policy_parsed") or {})
        _allowed_sources = _ev_policy.get("sources")  # None | list
        with _tracer.start_as_current_span("evidence.retrieve") as _span_r:
            _span_r.set_attribute("evidence.top_n", 5)
            if _allowed_sources is not None:
                _span_r.set_attribute("evidence.allowed_sources_count", len(_allowed_sources))
            evidences = await retriever.search(
                user_input,
                top_n=5,
                allowed_source_ids=_allowed_sources,
            )
            _span_r.set_attribute("evidence.retrieved_count", len(evidences))
        with _tracer.start_as_current_span("evidence.rerank") as _span_rr:
            _span_rr.set_attribute("evidence.input_count", len(evidences))
            evidences = await reranker.rerank(user_input, evidences, top_n=5)
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
    try:
        if skill_data.get("tool_bindings"):
            from app.mcp.runtime import parse_tool_bindings, match_with_registry
            from app.core.database import tools_repo
            parsed_bindings = parse_tool_bindings(skill_data["tool_bindings"])
            if parsed_bindings:
                mcp_tools = await match_with_registry(parsed_bindings, tools_repo)
                mcp_tools_detail = [{"name": t.get("name",""), "server": t.get("mcp_server",""), "ops": t.get("operations",[])} for t in mcp_tools]
                logger.info(f"MCP tools resolved: {[t.get('name') for t in mcp_tools]}")

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
        harness = DeepAgentHarness(agent, max_iterations=_max_iter, mcp_tools=mcp_tools, interaction_id=ctx.interaction_id)
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
        # Coleta usage por chamada LLM e calcula tokens da interação.
        # IMPORTANTE: somar input_tokens ingenuamente conta o histórico/system
        # prompt N vezes (cada chamada da reflexão/tool-loop reenvia tudo).
        # Convenção da métrica:
        #   input  = input da ÚLTIMA chamada (representa o tamanho final do prompt)
        #   output = SOMA dos outputs (cada geração é única)
        #   total  = input + output
        #   calls  = quantas chamadas LLM aconteceram nesta interação
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
            ctx.metadata["tokens"] = {
                "input": tin_last,
                "output": tout_sum,
                "total": tin_last + tout_sum,
                "calls": len(per_call),
                "input_billed_sum": tin_billed_sum,
                "total_billed": tin_billed_sum + tout_sum,
            }
            # Cap de tokens — defesa LLM04 contra runaway loops e abuso de custo.
            # Não é interrupção retroativa (já consumiu) — sinaliza no trace
            # e em log para que ratelimit/quotas externas possam agir.
            from app.core.config import get_settings as _gs
            _cap = _gs().interaction_max_tokens
            _billed = tin_billed_sum + tout_sum
            if _cap and _billed > _cap:
                logger.warning(
                    f"Token cap ultrapassado: agent={agent_id} billed={_billed} cap={_cap} calls={len(per_call)}"
                )
                ctx.metadata["tokens"]["cap_exceeded"] = True
                ctx.metadata["tokens"]["cap"] = _cap
        else:
            # Diagnóstico: nenhuma das messages tinha usage_metadata nem
            # response_metadata.token_usage / .usage. Provavelmente provider
            # fora do padrão LangChain (Maritaca/Sabia-4 reporta diferente).
            # Loga shape pra inspeção; follow-up: fallback via tiktoken.
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
            ctx.metadata["tokens"] = {"input": 0, "output": 0, "total": 0, "calls": 0, "input_billed_sum": 0, "total_billed": 0}
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

    # Verificação multi-dim (Verifier v2) — capturada para retornar no result.
    # None quando nenhum verifier roda (pipeline, fast skip, fallback heurístico).
    verification = None

    # Threshold de evidência: lê min_relevance do ## Evidence Policy da skill
    # (parser já extrai pra _evidence_policy_parsed.min_relevance). Quando
    # ausente, default 0.3 — comportamento histórico. Faixa válida [0..1]
    # garantida pelo parser. Single source of truth pros 3 caminhos
    # heurísticos abaixo (production_async, v2_fallback, standard).
    _DEFAULT_MIN_RELEVANCE = 0.3
    _ev_policy_for_threshold = (skill_data.get("_evidence_policy_parsed") or {}) if skill_data else {}
    _raw_mr = _ev_policy_for_threshold.get("min_relevance")
    _min_relevance = float(_raw_mr) if isinstance(_raw_mr, (int, float)) else _DEFAULT_MIN_RELEVANCE
    _min_relevance_source = "skill" if isinstance(_raw_mr, (int, float)) else "default"
    # Exposto no result.trace pra audit/UI mostrar "Threshold: 0.15 (skill)"
    ctx.metadata["evidence_min_relevance"] = _min_relevance
    ctx.metadata["evidence_min_relevance_source"] = _min_relevance_source

    if pipeline_context or skip_evidence:
        await fsm.run_verify_evidence({"ok": True, "confidence": 1.0})
    elif _pg_settings.verifier_v2_enabled and _pg_settings.verifier_production_async:
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
            )
            await fsm.run_verify_evidence({
                "ok": verification.ok,
                "confidence": verification.confidence,
                "risk_high": verification.risk_high,
                "fraud_suspected": verification.fraud_suspected,
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

    return await _build_result(
        ctx, start, mesh_chain=mesh_chain, attachments=attachment_meta,
        agent=agent, skill_data=skill_data, mcp_tools_detail=mcp_tools_detail,
        verification=verification,
    )


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
    mcp_tools_invoked: list = []
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
    _diag_min_relevance = float(ctx.metadata.get("evidence_min_relevance") or 0.3)
    _diag_threshold_source = ctx.metadata.get("evidence_min_relevance_source") or "default"
    _diag_threshold_label = f"{_diag_min_relevance:.2f} ({_diag_threshold_source})"

    if evidence_count == 0:
        diagnostics.append({"level": "info", "text": "Nenhuma evidência encontrada. Registre bases de conhecimento em Evidência para habilitar RAG."})
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

    verification_dict = _serialize_verification(verification)
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
            # mcp_tools: invocações REAIS registradas em tool_calls durante esta
            # interaction (não as tools disponíveis no registry). Métrica do painel
            # de rastreabilidade lê `.mcp_tools.length` — contagem agora reflete
            # chamadas executadas. mcp_tools_available preserva a lista declarada
            # para a aba de "Ferramenta(s) MCP vinculada(s)" no execution_log.
            "mcp_tools": mcp_tools_invoked,
            "mcp_tools_available": mcp_tools_detail or [],
            # api_tools_count: contagem de binding_executions desta interaction. Em
            # modo LLM normalmente é 0 (bindings só rodam em execute_declarative);
            # mantido aqui pra simetria semântica com mcp_tools (execução real, não
            # declaração).
            "api_tools_count": api_tools_invoked_count,
            "tokens": ctx.metadata.get("tokens") or {"input": 0, "output": 0, "total": 0},
            "execution_log": exec_log,
        },
    }


def _build_execution_log(
    agent: dict, skill_data: dict, skill_detail: dict,
    mcp_tools_detail: list, transitions: list,
    evidence_count: int, evidence_sources: list,
    evidence_score: float, duration: float, final_state: str,
    evidence_detail: list | None = None,
    evidence_min_relevance: float = 0.3,
    evidence_min_relevance_source: str = "default",
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
    progress_callback=None,
) -> dict:
    """Executa pipeline completo pelo AI Mesh.

    MELHORIA: Short-circuit para agentes pass-through.
    Agentes sem SKILL.md e com prompt genérico são ignorados (0ms),
    propagando o input diretamente ao próximo agente da cadeia.

    progress_callback: opcional, `async def cb(event: dict) -> None`. Quando
    presente, é chamado em pontos-chave (pipeline_start, agent_start,
    agent_passthrough, agent_done) pra streaming via SSE. Erro no callback
    é absorvido — não afeta a execução do pipeline.
    """
    start = time.time()
    entry_agent = await agents_repo.find_by_id(entry_agent_id)
    if not entry_agent:
        raise ValueError(f"Agente '{entry_agent_id}' não encontrado.")

    chain = await _resolve_ordered_chain(entry_agent_id)
    if not chain:
        chain = [entry_agent_id]

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
            _a = await agents_repo.find_by_id(_aid)
            if _a:
                _chain_meta.append({"id": _aid, "name": _a.get("name", ""), "kind": _a.get("kind", "")})
        except Exception:
            pass
    await _emit({"type": "pipeline_start", "total_agents": len(chain), "chain": _chain_meta})

    steps = []
    current_input = user_input
    last_result = None
    master_interaction_id = None
    child_interaction_ids = []

    for i, agent_id in enumerate(chain):
        agent = await agents_repo.find_by_id(agent_id)
        if not agent or agent.get("status") != "active":
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
            continue

        if i > 0 and last_result:
            current_input = (
                f"## Contexto do agente anterior ({steps[-1].get('agent_name','')}):\n"
                f"{last_result.get('output','')}\n\n"
                f"## Solicitação original:\n{user_input}"
            )

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
            await _emit({
                "type": "agent_done",
                "step_index": i,
                "agent_id": agent_id,
                "agent_name": agent.get("name", ""),
                "status": "completed",
                "duration_ms": result.get("duration_ms", 0),
                "final_state": result.get("final_state", ""),
                "output_preview": (result.get("output", "") or "")[:300],
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
                await turns_repo.create({
                    "id": str(_uuid.uuid4()),
                    "turn_number": turn_number,
                    "output_text_redacted": _step_out,
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

    final_result = {
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

    await _emit({"type": "pipeline_done", "result": final_result})

    return final_result


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
