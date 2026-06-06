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

from app.core.llm_providers import get_provider, is_llm_unreachable
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


def _build_response_language_directive(lang_tag: str) -> str:
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
    """
    label = _LANGUAGE_LABELS.get(lang_tag, lang_tag)
    return (
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


def _build_response_language_closing(lang_tag: str) -> str:
    """Reminder curto colado ao FIM do system prompt (estratégia sanduíche).

    Modelos open-weight grudam no que está mais próximo da geração — quando
    o prompt cresce (Output Contract + MCP Tools catalog + Guardrails), a
    diretiva inicial perde força. Este reminder de 1 linha no fim refresca
    a instrução logo antes do LLM começar a gerar tokens.

    Mantém-se curto de propósito: o detalhe está na diretiva inicial; aqui
    só ancora a regra.
    """
    label = _LANGUAGE_LABELS.get(lang_tag, lang_tag)
    return (
        "[LEMBRETE FINAL — IDIOMA]\n"
        f"Antes de gerar sua resposta: TODO texto que você produzir, "
        f"inclusive em campos JSON, deve estar em {label}. Traduza títulos "
        "e conteúdo do tool result; preserve apenas URLs, código e nomes "
        "próprios."
    )


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
    attempted: list[str] = []
    result = None
    for ci, (cand_p, cand_m) in enumerate(candidates):
        agent["llm_provider"] = cand_p
        agent["model"] = cand_m
        attempted.append(f"{cand_p}/{cand_m}")
        try:
            result = await run_attempt(cand_p, cand_m)
        except Exception as attempt_exc:
            if not is_llm_unreachable(attempt_exc):
                # Não é "não responder" (404/401/etc) → propaga pro except
                # externo, que mapeia pra mensagem acionável específica.
                raise
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
                exc_info=True,
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
                exc_info=True,
            )
            break
        # sucesso nesta tentativa — encerra a cadeia
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
        import re as _re
        from app.core.config import get_settings as _get_settings_lang
        skill = self.config.get("_parsed_skill", {})
        parts = []
        # Idioma — DIRETIVA prependida (antes do system_prompt do agent) pra
        # ter precedência forte na atenção do LLM.
        _lang = _resolve_response_language(self.config, _get_settings_lang())
        parts.append(_build_response_language_directive(_lang))
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
        parts.append(_build_response_language_closing(_lang))
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
                # Onda 1 Output Shape: length_preset + max_chars (engine usa
                # pra injetar diretiva no system_prompt + truncate hard).
                "_output_shape_parsed": getattr(parsed, "output_shape_parsed", {}) or {},
            }

    agent["_parsed_skill"] = skill_data

    # ── Execution Profile: determina modo de execução ──
    exec_profile = skill_data.get("_execution_mode", "standard")

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
                # Imagens são roteadas para o modelo multimodal (vision)
                # via detect_image_in_attachments → multimodal_fallback.
                # Conteúdo textual fica vazio porque a imagem vai raw na API.
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
            if _content:
                attachment_context += f"\n\n## Arquivo Anexo: {att.get('name','arquivo')}\n```\n{_content[:5000]}\n```"

    ctx = InteractionContext(agent_id=agent_id, journey=journey, channel=channel)
    fsm = InteractionStateMachine(ctx)
    # Propaga session_id para FSM reusar interaction existente (2026-06-01).
    # Antes ficava só na assinatura de execute_interaction e era descartado
    # — cada call criava uma sessão nova mesmo com session_id fornecido,
    # fragmentando conversas no workspace (user reportou sidebar com várias
    # entries quando esperava uma só).
    await fsm.run_intake(user_input, agent_id, journey, channel, session_id=session_id)

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
            draft = new_draft
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
        mcp_tools_declared_count=mcp_tools_declared_count,
        mcp_tools_unmatched=mcp_tools_unmatched,
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
                except: ops = [ops]
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
        "mode": "pipeline",
    }


async def execute_pipeline(
    entry_agent_id: str,
    user_input: str,
    channel: str = "api",
    attachments: list = None,
    progress_callback=None,
    session_id: str | None = None,
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

    chain, parent_of = await _resolve_ordered_chain_with_parents(entry_agent_id)
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
    # Tracking por-id pro roteamento contra o PARENT REAL (fan-out / branch).
    # outputs_by_id[id]/final_states_by_id[id]/names_by_id[id] guardam o
    # resultado de cada agente `completed`. Usados quando o parent de um nó
    # NÃO é o irmão anterior na lista BFS (fan-out 1→N): aí o gate/scope/input
    # avaliam contra o output do PARENT, não do anterior. Cadeia linear não
    # usa esses mapas (parent == anterior) → comportamento byte-idêntico.
    outputs_by_id: dict = {}
    final_states_by_id: dict = {}
    names_by_id: dict = {}

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
        if (
            parent_id is not None
            and i > 0
            and parent_id != chain[i - 1]
            and parent_id in outputs_by_id
        ):
            upstream_id = parent_id
            upstream_output = outputs_by_id[parent_id]
            upstream_final_state = final_states_by_id.get(parent_id, "")
            upstream_name = names_by_id.get(parent_id, "")
            have_upstream = True
        elif i > 0 and last_result:
            upstream_id = chain[i - 1]
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
            )
            if skip_by_conditional:
                steps.append({
                    "agent_id": agent_id,
                    "agent_name": agent.get("name", ""),
                    "agent_kind": agent.get("kind", ""),
                    "agent_model": agent.get("model", ""),
                    "status": "skipped_conditional",
                    "output": upstream_output,
                    "final_state": "SkippedConditional",
                    "duration_ms": 0,
                    "evidence_score": 0,
                    "transitions": [],
                    "trace": {
                        "diagnostics": [
                            {
                                "level": "info",
                                "text": f"Conexão condicional avaliou false — {agent.get('name','')} pulado (passthrough)",
                            }
                        ],
                    },
                })
                await _emit({
                    "type": "agent_skipped",
                    "step_index": i,
                    "agent_id": agent_id,
                    "agent_name": agent.get("name", ""),
                    "reason": "conditional_false",
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
            )
            if skip_by_default:
                steps.append({
                    "agent_id": agent_id,
                    "agent_name": agent.get("name", ""),
                    "agent_kind": agent.get("kind", ""),
                    "agent_model": agent.get("model", ""),
                    "status": "skipped_default",
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

        try:
            result = await execute_interaction(
                agent_id=agent_id,
                user_input=current_input,
                channel=channel,
                attachments=attachments if i == 0 else None,
                pipeline_context=pipeline_ctx,
                # Só o primeiro agente reutiliza a session_id do request.
                # Subsequentes criam sub-interactions próprias (child_interaction_ids)
                # que se ligam à master via execute_pipeline:2298+ depois.
                session_id=session_id if i == 0 else None,
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

    # Tipo de conexão REAL por nó (display honesto: não cravar "sequential"
    # quando a aresta parent→nó é `conditional`). Lookup leve por step, contra
    # o parent real. Fail-open → "sequential" se a busca falhar.
    from app.core.database import mesh_repo as _mesh_repo_disp
    conn_type_by_id: dict = {}
    for s in steps:
        _aid = s.get("agent_id", "")
        _pid = parent_of.get(_aid)
        if not _pid:
            continue
        try:
            _conns = await _mesh_repo_disp.find_all(source_agent_id=_pid, limit=20)
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


def _build_conditional_context(
    output: str | None = None,
    final_state: str | None = None,
    user_input: str | None = None,
    attachments: list | None = None,
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
    # text_all: pergunta + nome/extensão do anexo num só campo — a expr derivada
    # casa keyword tanto no texto digitado quanto no arquivo (1 var, não duas).
    text_all = " ".join(p for p in (inp_lower, att_names, att_exts) if p).strip()
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
    }


# Metadata declarativa das vars — consumida pela UI do wizard (vars panel
# com descrição + exemplo). Mantém ao lado de _build_conditional_context
# para não dar drift.
CONDITIONAL_VARS_META: list[dict] = [
    {"name": "output", "type": "str", "desc": "Texto completo da resposta do agente upstream"},
    {"name": "output_lower", "type": "str", "desc": "output em lowercase (case-insensitive matching)"},
    {"name": "output_length", "type": "int", "desc": "Quantidade de caracteres em output"},
    {"name": "has_output", "type": "bool", "desc": "True se output não é vazio"},
    {"name": "final_state", "type": "str", "desc": "Estado final do FSM: Recommend / Refuse / Escalate / LogAndClose"},
    {"name": "is_recommend", "type": "bool", "desc": "Atalho para final_state == 'Recommend'"},
    {"name": "is_refuse", "type": "bool", "desc": "Atalho para final_state == 'Refuse'"},
    {"name": "is_escalate", "type": "bool", "desc": "Atalho para final_state == 'Escalate'"},
    {"name": "contains_image", "type": "bool", "desc": "True se output menciona imagem (.jpg/.png/.webp/'imagem')"},
    {"name": "contains_url", "type": "bool", "desc": "True se output contém http:// ou https://"},
    {"name": "contains_pdf", "type": "bool", "desc": "True se output menciona pdf"},
    {"name": "lines_count", "type": "int", "desc": "Quantas linhas tem output"},
    {"name": "input", "type": "str", "desc": "Solicitação original do usuário (entrada do pipeline) — útil p/ rotear pela pergunta"},
    {"name": "input_lower", "type": "str", "desc": "input em lowercase (case-insensitive matching da pergunta do usuário)"},
    {"name": "text_all", "type": "str", "desc": "input + nome/extensão dos anexos (lowercase) — casa keyword tanto na pergunta quanto no arquivo"},
    {"name": "has_attachments", "type": "bool", "desc": "True se o usuário anexou algum arquivo"},
    {"name": "has_document", "type": "bool", "desc": "True se há anexo do tipo documento (pdf/doc/docx/ppt/pptx/xls/xlsx/csv/txt/…)"},
    {"name": "has_image", "type": "bool", "desc": "True se há anexo do tipo imagem (jpg/png/gif/webp/…)"},
    {"name": "attachment_names", "type": "str", "desc": "Nomes dos anexos concatenados (lowercase) — ex.: 'relatorio.pdf foto.png'"},
    {"name": "attachment_exts", "type": "str", "desc": "Extensões dos anexos (lowercase) — ex.: 'pdf png'"},
    {"name": "attachment_types", "type": "str", "desc": "MIME types dos anexos (lowercase) — ex.: 'application/pdf image/png'"},
    {"name": "attachment_count", "type": "int", "desc": "Quantidade de anexos"},
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


async def _should_skip_conditional(
    *,
    source_id: str,
    target_id: str,
    last_output: str,
    last_final_state: str,
    user_input: str = "",
    target_name: str = "",
    attachments: list | None = None,
) -> bool:
    """True se a conexão source→target é `connection_type=conditional` e
    a expressão configurada em `config.expr` avaliou para `False`.

    Override "o roteador mandou" (2026-06-06): se o agente upstream NOMEIA
    explicitamente este alvo no output (`target_name` aparece em `last_output`),
    a decisão do roteador vence o heurístico de keywords e NÃO skipamos — ver
    `_output_names_target`. Isto corrige o caso em que o AR/AOBD roteia certo
    pela semântica mas o vocabulário da pergunta não bate a expr.

    Política de erro: **fail-open** — qualquer falha (config malformado,
    expr inválida, exception no Jinja) loga warning e devolve `False`
    (NÃO skipa). É melhor executar o agente que perder dados por bug
    de regra. Operador vê o warning em errors.log e corrige.
    """
    from app.core.database import mesh_repo

    conns = await mesh_repo.find_all(source_agent_id=source_id, limit=20)
    conn = next((c for c in conns if c.get("target_agent_id") == target_id), None)
    if not conn or conn.get("connection_type") != "conditional":
        return False

    # ── Override "o roteador mandou" (2026-06-06) ──────────────────────
    # Se o agente upstream NOMEIA explicitamente este alvo (ex.: o AR responde
    # "Encaminhar ao agente Rentab"), a decisão do roteador vence o heurístico
    # de keywords: NÃO skipa. Sem isto, perguntas cujo vocabulário não bate a
    # expr (mas que o LLM roteou certo) eram puladas — o SA nomeado nunca
    # respondia. Ver _output_names_target.
    if _output_names_target(last_output, target_name):
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
    from app.core.database import mesh_repo

    conns = await mesh_repo.find_all(source_agent_id=source_id, limit=50)
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
        try:
            sib_agent = await agents_repo.find_by_id(sib_target_id)
            if sib_agent:
                sib_name = sib_agent.get("name", "")
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
    from app.core.database import mesh_repo

    prev_len = len(last_output or "")
    inherit_result = {
        "mode": "inherit",
        "output": last_output or "",
        "skip_prefix": False,
        "chars_before": prev_len,
        "chars_after": prev_len,
    }

    try:
        conns = await mesh_repo.find_all(source_agent_id=source_id, limit=20)
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


async def _resolve_ordered_chain_with_parents(entry_agent_id: str) -> tuple[list, dict]:
    """Resolve a cadeia ordenada downstream (BFS) **e** o mapa `parent_of`.

    Devolve `(chain, parent_of)`:
    - `chain`: agent_ids em ordem BFS a partir do entry.
    - `parent_of`: `{child_id: source_id}` — o agente que DESCOBRIU cada
      filho (a aresta real `source→child` que o trouxe à cadeia). O entry
      é raiz e não aparece como chave.

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
    from app.core.database import mesh_repo
    chain = [entry_agent_id]
    visited = {entry_agent_id}
    queue = [entry_agent_id]
    parent_of: dict = {}
    while queue:
        current = queue.pop(0)
        conns = await mesh_repo.find_all(source_agent_id=current, limit=20)
        for conn in conns:
            tid = conn.get("target_agent_id", "")
            if tid and tid not in visited:
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
