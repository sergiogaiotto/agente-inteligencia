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
- /skill   → skill_generation  (criar/alterar SKILL.md exige seguir regras
                                 estruturais rígidas — operations declaradas,
                                 verbos imperativos, frases proibidas. Separado
                                 de reasoning desde 2026-05-29 após gpt-oss-120b
                                 errar 4x consecutivas o mesmo padrão de omitir
                                 operation=)
- /agent   → reasoning          (planejar system_prompt + skills + tools)
- /refine  → instruct           (refinar texto existente é instruction-following)
"""
import json
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from app.core.llm_providers import (
    canonical_provider,
    get_provider,
    is_llm_auth_error,
    is_llm_param_rejection,
    is_llm_unreachable,
)
from app.core.llm_breaker import breaker, note_short_circuit
from app.llm_routing import resolve_llm_for_task, load_routing
from app.core.config import get_settings
from app.skill_parser.parser import strip_code_fence
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wizard", tags=["wizard"])


# Defaults por rota — usado quando frontend não enviar task_type.
# Os valores batem com TASK_TYPES de app/llm_routing.py.
_DEFAULT_TASK_TYPE = {
    "agent": "reasoning",
    "skill": "skill_generation",
    "refine": "instruct",
    # "Pergunte ao mentor": conversa-guia é instruction-following — modelo
    # menor basta, mesmo critério do /refine.
    "mentor": "instruct",
    # "IA, me ajude!" do Composer: compor missão + regras de delegação é
    # planejamento (qual destino para qual intenção) — mesmo espírito do /agent.
    "compose": "reasoning",
}

# Esforço de raciocínio nas GERAÇÕES pesadas do wizard (/agent e /skill).
# Passa pelo factory get_provider, que só REPASSA quando o modelo de destino
# aceita o parâmetro (gpt-oss sempre; Azure/OpenAI só o1/o3/o4/gpt-5 —
# gpt-4o devolve 400 "Unrecognized request argument"). Efeito prático: com o
# gpt-oss-120b roteado a geração USA reasoning; se cair no fallback hospedado
# (gpt-4o), o parâmetro é descartado e a geração segue SEM reasoning em vez
# de quebrar. Rotas leves (refine/mentor/compose) não o usam — latência de
# chat importa mais que profundidade.
#
# Configurável (27.0.0): antes era a constante hardcoded `_WIZARD_REASONING_EFFORT
# = "high"`; agora vem do setting `wizard_reasoning_effort` (aba Parâmetros,
# runtime sem restart). Default 'high' preserva o comportamento anterior.
_REASONING_EFFORT_VALUES = frozenset({"high", "medium", "low"})


def _wizard_reasoning_effort() -> Optional[str]:
    """Esforço de raciocínio configurado para as gerações do Wizard.

    Lê o setting `wizard_reasoning_effort` e sanitiza para {high,medium,low}
    ou None (qualquer outro valor = desligado; o sentinela canônico da UI é
    'off' — 68.0.0 — porque '' não sobrevive ao apply_settings_to_env, que
    poppa env falsy e faz o Settings cair no default 'high'). O gate por
    MODELO continua em get_provider — o valor só tem EFEITO em modelos que
    aceitam (gpt-oss sempre); em gpt-4o/gpt-4.1 é descartado sem erro.
    """
    raw = (get_settings().wizard_reasoning_effort or "").strip().lower()
    return raw if raw in _REASONING_EFFORT_VALUES else None


# Verbosidade da GERAÇÃO de SKILL.md (68.0.0) — tamanho do DOCUMENTO gerado,
# não da resposta em runtime (isso é length_preset/## Output Shape). Motivo:
# ~76% do output do Wizard era prosa induzida pelo prompt além do que o
# validador exige — e as seções Purpose/Workflow/Output Contract/Guardrails
# entram VERBATIM no prompt a CADA invoke (engine._build_system_prompt), com
# Contract+Guardrails de novo no prompt do juiz, sem teto. Verbosidade na
# geração é custo recorrente em TODA execução futura da skill.
_WIZARD_VERBOSITY_VALUES = frozenset({"enxuto", "padrao", "didatico"})
_WIZARD_VERBOSITY_DEFAULT = "didatico"


def _wizard_verbosity() -> str:
    """Nível de verbosidade configurado para a geração de SKILL.md.

    Lê o setting `wizard_verbosity` e sanitiza para {enxuto,padrao,didatico}.
    Valor fora do enum cai no default 'didatico' (= comportamento anterior,
    fail-safe): a UI/API já rejeitam com 422 (pattern no SettingsSave), mas a
    env var WIZARD_VERBOSITY crua ainda pode trazer lixo.
    """
    raw = (get_settings().wizard_verbosity or "").strip().lower()
    return raw if raw in _WIZARD_VERBOSITY_VALUES else _WIZARD_VERBOSITY_DEFAULT


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


# ─── Resiliência de LLM no wizard: fallback hospedado quando inacessível ───
# As rotas do wizard resolvem o LLM via task_type, e os defaults apontam para
# o hub interno (GPT-OSS), que fica INACESSÍVEL fora da rede corporativa/VPN
# — aí o wizard morria com 500 genérico após ~21s de timeout ("Não consegui
# responder agora"). Aqui detectamos esse caso e caímos no modelo HOSPEDADO do
# `multimodal_fallback` (azure/gpt-4o por padrão — acessível pela internet
# pública), que é o modelo "sempre disponível" da plataforma. Se nem ele
# responder, devolvemos mensagem CLARA e ACIONÁVEL. TODAS as rotas que geram
# via LLM devem usar _wizard_llm_complete — chamar get_provider().generate()
# direto deixa a rota sem fallback (incidente req_c60a15302ffd, 2026-07-03:
# /skill e /agent estavam cruas e o ConnectError virava 500).

def _is_llm_unreachable(exc: Exception) -> bool:
    """Wrapper fino — lógica canônica em
    ``app.core.llm_providers.is_llm_unreachable`` (compartilhada com a cadeia
    de resiliência do runtime do engine). Mantido como nome local pra não
    quebrar os testes existentes do wizard e os call-sites abaixo.
    """
    return is_llm_unreachable(exc)


def _is_llm_auth_error(exc: Exception) -> bool:
    """Wrapper fino — lógica canônica em
    ``app.core.llm_providers.is_llm_auth_error`` (movida pra lá em 24.9.0,
    compartilhada com ``generate_with_hosted_fallback`` do core). Mantido
    como nome local pra não quebrar testes e call-sites do wizard.
    """
    return is_llm_auth_error(exc)


def _wizard_unreachable_message(provider: str, model: str) -> str:
    """Mensagem acionável quando o modelo (e o fallback) estão inacessíveis."""
    pm = f"{provider}/{model}" if model else (provider or "desconhecido")
    return (
        f"O modelo de IA configurado ({pm}) está inacessível agora — não "
        "consegui conectar ao servidor dele. Se ele roda no hub interno "
        "(GPT-OSS), conecte-se à VPN/rede corporativa. Como alternativa, "
        "ajuste o Roteamento LLM em Configurações para um provedor hospedado "
        "com credenciais (ex.: Azure/OpenAI)."
    )


def _wizard_auth_message(provider: str, model: str) -> str:
    """Mensagem acionável quando a CREDENCIAL do modelo (e do fallback) é recusada."""
    pm = f"{provider}/{model}" if model else (provider or "desconhecido")
    return (
        f"As credenciais do modelo de IA ({pm}) foram recusadas (401 — chave "
        "inválida ou expirada). Atualize a API key desse provedor em "
        "Configurações → Plataforma, ou aponte o papel de geração para um "
        "provedor com credencial válida em Configurações → Roteamento LLM."
    )


async def _wizard_hosted_fallback(
    failed_provider: str,
) -> tuple[Optional[str], Optional[str]]:
    """Alvo de fallback HOSPEDADO quando o provider roteado está inacessível.

    Usa o `multimodal_fallback` do Roteamento LLM (azure/gpt-4o por padrão) —
    o modelo "sempre disponível" da plataforma, acessível pela internet. Só
    retorna se for um provider DIFERENTE do que falhou (evita cair no MESMO
    hub interno inacessível — ex.: gpt-oss-20b → gpt-oss-120b só dobraria o
    timeout). (None, None) quando não há alternativa distinta.
    """
    from app.core.llm_providers import canonical_provider
    failed = canonical_provider(failed_provider)
    try:
        routing = await load_routing()
    except Exception:
        return None, None
    target = (routing.get("multimodal_fallback") or "").strip()
    if "/" not in target:
        return None, None
    fb_provider, fb_model = target.split("/", 1)
    fb_provider = fb_provider.strip().lower()
    fb_model = fb_model.strip()
    # canonical: "openai" é alias de "azure" no get_provider — sem isso um
    # fallback openai→azure re-tentaria o MESMO backend com a MESMA chave.
    if not fb_provider or canonical_provider(fb_provider) == failed:
        return None, None
    return fb_provider, fb_model


async def _wizard_llm_complete(
    messages: list[dict], provider: str, model: str, *, route: str,
    temperature: Optional[float] = None, response_format: Optional[dict] = None,
    reasoning_effort: Optional[str] = None,
    usage_sink: Optional[dict] = None,
) -> tuple[str, str, str]:
    """Gera com o provider roteado; se INACESSÍVEL, tenta o fallback hospedado.

    Returns (content, used_provider, used_model). Levanta HTTPException(503)
    com mensagem acionável quando nem o primário nem o fallback respondem.
    Exceções que NÃO são de alcance propagam (caller mapeia para 500).

    `temperature`/`response_format` (opcionais): quando informados, propagam ao
    provider (construtor) e ao generate respectivamente. None → comportamento
    legado intacto (o /catalog/suggest não os passa). Usado pelo Tier 2 para
    gerar struct DETERMINÍSTICO (temperature=0 + JSON-mode).

    `reasoning_effort` (opcional): pedido de raciocínio repassado ao factory
    get_provider — o gate por MODELO vive lá (gpt-oss aceita; gpt-4o dropa).
    Vale para o primário E para o fallback: se o primário com reasoning cair
    num fallback que não aceita o parâmetro, a geração segue sem reasoning.
    """
    prov_kwargs: dict = {"model": (model or None)}
    if temperature is not None:
        prov_kwargs["temperature"] = temperature
    if reasoning_effort is not None:
        prov_kwargs["reasoning_effort"] = reasoning_effort
    gen_kwargs: dict = {}
    if response_format is not None:
        gen_kwargs["response_format"] = response_format
    canon_primary = canonical_provider(provider)
    primary_auth = False

    # Primário — pula (sem pagar o timeout) se o breaker abriu o circuito dele.
    if await breaker.allow(canon_primary):
        try:
            llm = get_provider(provider, **prov_kwargs)
            resp = await llm.generate(messages, **gen_kwargs)
            await breaker.record_success(canon_primary)
            # usage_sink (45.0.0, optimizer): caller opta por receber o usage
            # REAL da chamada (tokens) p/ contabilizar custo no ledger —
            # out-param não muda a assinatura de retorno (call sites intactos).
            if usage_sink is not None:
                usage_sink.update({"provider": provider, "model": model,
                                   "usage": resp.get("usage") or {}})
            return resp["content"], provider, model
        except Exception as exc:
            # Rejeição de PARÂMETRO (ex.: servidor OpenAI-compatible atrás da URL
            # do gpt-oss que não conhece reasoning_effort → 400 "extra inputs"):
            # re-entra UMA vez sem o parâmetro — mesmo espírito do strip-retry da
            # cadeia do engine (#492). A recursão com reasoning_effort=None não
            # repete o strip e mantém TODO o resto da resiliência (fallback
            # hospedado, 503 acionável) para a re-tentativa.
            if reasoning_effort is not None and is_llm_param_rejection(exc):
                logger.info(
                    "wizard.llm.param_stripped_retry",
                    extra={
                        "event": "wizard.llm.param_stripped_retry",
                        "wizard_route": route,
                        "provider": provider,
                        "model": model,
                        "stripped": "reasoning_effort",
                    },
                )
                return await _wizard_llm_complete(
                    messages, provider, model, route=route,
                    temperature=temperature, response_format=response_format,
                    reasoning_effort=None, usage_sink=usage_sink,
                )
            primary_auth = _is_llm_auth_error(exc)
            if _is_llm_unreachable(exc):
                await breaker.record_failure(canon_primary)
            if not (_is_llm_unreachable(exc) or primary_auth):
                raise
            # cai no fallback hospedado abaixo
    else:
        note_short_circuit(canon_primary, f"wizard.{route}")
        # circuito ABERTO no primário → trata como inalcançável; cai no fallback

    # Tenta o fallback hospedado tanto em INACESSÍVEL (rede) quanto em AUTH
    # (401 — chave ruim): um provider com credencial inválida não deve derrubar
    # o wizard se o multimodal_fallback (azure) tem credencial OK.
    fb_provider, fb_model = await _wizard_hosted_fallback(provider)
    if fb_provider:
        canon_fb = canonical_provider(fb_provider)
        logger.warning(
            "wizard.llm.fallback",
            extra={
                "event": "wizard.llm.fallback",
                "wizard_route": route,
                "failed_provider": provider,
                "failed_model": model,
                "failed_reason": "auth" if primary_auth else "unreachable",
                "fallback_provider": fb_provider,
                "fallback_model": fb_model,
            },
        )
        if await breaker.allow(canon_fb):
            try:
                fb_kwargs = {**prov_kwargs, "model": (fb_model or None)}
                fb_llm = get_provider(fb_provider, **fb_kwargs)
                resp = await fb_llm.generate(messages, **gen_kwargs)
                await breaker.record_success(canon_fb)
                if usage_sink is not None:
                    usage_sink.update({"provider": fb_provider, "model": fb_model,
                                       "usage": resp.get("usage") or {}})
                return resp["content"], fb_provider, fb_model
            except Exception as exc2:
                if _is_llm_unreachable(exc2):
                    await breaker.record_failure(canon_fb)
                if not (_is_llm_unreachable(exc2) or _is_llm_auth_error(exc2)):
                    raise
                # fallback também falhou (rede/auth) → cai na mensagem acionável abaixo
        else:
            note_short_circuit(canon_fb, f"wizard.{route}")
            # fallback também curto-circuitado → mensagem acionável abaixo

    logger.warning(
        "wizard.llm.unavailable",
        extra={
            "event": "wizard.llm.unavailable",
            "wizard_route": route,
            "provider": provider,
            "model": model,
            "reason": "auth" if primary_auth else "unreachable",
        },
    )
    if primary_auth:
        raise HTTPException(503, _wizard_auth_message(provider, model))
    raise HTTPException(503, _wizard_unreachable_message(provider, model))


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
    # Cond-C.2 (36.2.0): Contrato de Decisão declarado por FORMULÁRIO —
    # {campo: [valores]} vira a seção ## Decisions (selada, copiada verbatim).
    # None/vazio = wizard não emite a seção.
    decisions: Optional[dict] = None

    @field_validator("decisions")
    @classmethod
    def _decisions_validas(cls, v):
        """Validação ACIONÁVEL (espelha extract_decisions_schema, que em runtime
        DESCARTA campo inválido em silêncio — no formulário o autor precisa
        saber exatamente o que consertar, não descobrir depois que o contrato
        nasceu morto)."""
        if not v:
            return None
        import re as _re

        from app.skill_parser.decisions_schema import _RESERVED_FIELDS, _norm
        problemas: list[str] = []
        limpo: dict = {}
        for campo, valores in v.items():
            campo_s = str(campo).strip()
            if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", campo_s):
                problemas.append(
                    f"campo '{campo_s}': use um identificador ASCII (letras, números e _; sem acento/espaço)")
                continue
            if campo_s in _RESERVED_FIELDS:
                problemas.append(
                    f"campo '{campo_s}': nome reservado — `decision.{campo_s}` resolveria um método interno e a regra nunca casaria")
                continue
            if campo_s in limpo:
                problemas.append(f"campo '{campo_s}' duplicado (após remover espaços)")
                continue
            if not isinstance(valores, list):
                problemas.append(f"campo '{campo_s}': valores devem ser uma lista")
                continue
            vals: list[str] = []
            vistos: set = set()
            for val in valores:
                s = str(val).strip()
                if not s:
                    continue
                if any(ch in s for ch in ";,="):
                    problemas.append(
                        f"valor '{s}' (campo '{campo_s}'): ';', ',' e '=' são separadores da linha DECISAO e não podem aparecer no valor")
                    continue
                # borda que o extract_decision_line STRIPA do valor emitido —
                # um canônico com esses chars na borda seria irrepresentável
                # (major do review pré-push: 'aprovado.' nasceria morto).
                if s != s.strip("\"'`*_."):
                    problemas.append(
                        f"valor '{s}' (campo '{campo_s}'): aspas, crase, '*', '_' e '.' nas BORDAS são removidos da linha no runtime — o valor nunca casaria")
                    continue
                # dedup pela MESMA norma do runtime (acento/caixa): 'Alta' e
                # 'alta' seriam o mesmo valor no match do enum.
                k = _norm(s)
                if k in vistos:
                    continue
                vistos.add(k)
                vals.append(s)
            if not vals:
                problemas.append(f"campo '{campo_s}': informe ao menos 1 valor válido")
                continue
            limpo[campo_s] = vals
        if problemas:
            raise ValueError("Contrato de Decisão inválido: " + " · ".join(problemas[:6]))
        return limpo or None


class WizardRefineRequest(BaseModel):
    current_content: str
    instruction: str
    field: str = "all"
    # Refino por camada: o tipo do agente muda o ESPÍRITO do refino. AOBD
    # (Orquestrador) e AR (Roteador) precisam de missão cristalina + regras de
    # delegação/roteamento — não de "formato de saída e guardrails" (pegada de
    # um Subagente executor). Ver _refine_persona(). Default subagent =
    # comportamento histórico (retrocompat com clients que não enviam kind).
    kind: str = "subagent"
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
        # Fallback hospedado quando o roteado está inacessível/401; reasoning
        # só chega ao modelo que aceita o parâmetro (gate no get_provider).
        content, used_provider, used_model = await _wizard_llm_complete([
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
        ], provider, model, route="agent", reasoning_effort=_wizard_reasoning_effort())
        content = content.strip()
        if content.startswith("```"):
            import re
            m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if m: content = m.group(1).strip()
        result = json.loads(content)
        return {"status": "ok", "agent": result}
    except json.JSONDecodeError:
        return {"status": "ok", "agent": {"name": "", "description": data.description, "kind": "subagent", "domain": data.domain, "system_prompt": content, "suggested_skills": [], "suggested_tools": []}}
    except HTTPException:
        # 503 acionável do _wizard_llm_complete — não re-empacotar como 500.
        raise
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
    from app.core.database import _get_pool

    # 1. MCP tools — vive em app.tools.* via repository simples
    if data.mcp_tool_ids:
        try:
            pool = _get_pool()
            async with pool.acquire() as con:
                rows = await con.fetch(
                    # 39.2.0 (item 3 PR3): operations/discovered_tools/per_tool_mode
                    # entram no lookup — o prompt do gerador e o validador passam a
                    # seguir o MODO efetivo de cada conector (e a orientação de
                    # operations volta a ter a lista real, que este SELECT magro
                    # havia deixado de trazer).
                    "SELECT id, name, description, operations, discovered_tools, "
                    "per_tool_mode FROM tools WHERE id = ANY($1::text[])",
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
                    "SELECT id, name, urn, schema_json, row_count, suggested_pk FROM data_tables "
                    "WHERE id = ANY($1::text[])",
                    data.table_ids,
                )
                for r in rows:
                    # schema_json é uma LISTA [{name,type,nullable}] — extrai resumo
                    # + nomes reais de coluna (usados no skeleton executável abaixo).
                    schema_summary, col_names = _summarize_table_schema(r.get("schema_json"))
                    result["data_tables"].append({
                        "id": r["id"],
                        "name": r["name"],
                        "urn": r.get("urn"),
                        "row_count": r.get("row_count"),
                        "schema_summary": schema_summary,
                        "columns": col_names,
                        "suggested_pk": r.get("suggested_pk"),
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
                            # path: caminho relativo (sem base_url). Exposto para o
                            # template do prompt incluir o campo `path:` no YAML
                            # — o linter exige path (linter.py:82-87) e o engine
                            # resolve host via connector em runtime.
                            "path": "/" + (r.get("path") or "").lstrip("/"),
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


def _split_tools_by_mode(mcp_tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """(per_tool, legacy) — conector entra no grupo per-tool quando o modo
    EFETIVO (per_tool_enabled_for: tri-state do conector compondo com o global)
    está ON e há discovered_tools persistido. É o MESMO critério do gate de
    build_openai_tools (39.0.0): o wizard ensina exatamente o que o runtime
    vai expor."""
    from app.mcp.runtime import per_tool_covered
    per, legacy = [], []
    for t in mcp_tools or []:
        if per_tool_covered(t):
            per.append(t)
        else:
            legacy.append(t)
    return per, legacy


def _mcp_block(mcp_tools: list[dict]) -> str:
    """Sub-bloco específico de MCP. Cobre o caso original do bug Context7.

    Bug v2 (2026-05-29): mesmo com Workflow imperativo correto, SKILL gerada
    pediu `operation=search` em Context7 (que só aceita docs/code/prompt).
    Servidor MCP devolveu erro, LLM em runtime respondeu honestamente "não
    consegui acessar". Causa: LLM gerador escolheu "search" como nome de
    operation por sonoridade, em vez de usar uma das operations declaradas
    no Registry. Fix: regra explícita listando operations + proibição de
    inventar names que não estão na lista.

    39.2.0 (item 3 PR3): conector em modo PER-TOOL ganha orientação própria —
    skills novas nascem chamando as funções pelos NOMES REAIS descobertos;
    operation/query é orientação SÓ para conectores em modo legado. Sem o
    gate, o wizard seguia gerando skills no paradigma velho mesmo com o
    per-tool ativo (instrução contraditória com o function spec do runtime).
    """
    per_tool, legacy = _split_tools_by_mode(mcp_tools)
    parts: list[str] = []
    if per_tool:
        from app.mcp.runtime import _parse_discovered_tools
        linhas = []
        for t in per_tool:
            names = [d["name"] for d in _parse_discovered_tools(t.get("discovered_tools"))]
            shown = ", ".join(f"`{n}`" for n in names[:8])
            if len(names) > 8:
                shown += f" (+{len(names) - 8})"
            linhas.append(f"    - `{t['name']}`: {shown}")
        first_fn = _parse_discovered_tools(per_tool[0].get("discovered_tools"))[0]["name"]
        parts.append(
            "[MCP per-tool] **Conectores em modo per-tool** — cada tool "
            "DESCOBERTA vira uma FUNÇÃO própria com o schema real do servidor:\n"
            + "\n".join(linhas) + "\n"
            "  - **REGRA CRÍTICA — nomes reais:** no Workflow e nos Examples, "
            "chame a função pelo NOME REAL descoberto (lista acima), com os "
            "parâmetros do schema dela. NÃO use `operation=`/`query=` para "
            "estes conectores — esse par NÃO existe no modo per-tool.\n"
            f"  - Exemplo no Workflow: \"Chame a função `{first_fn}` com os "
            "parâmetros extraídos do pedido ANTES de gerar a resposta.\""
        )
    if not legacy:
        return "\n".join(parts)
    mcp_tools = legacy  # o bloco legado abaixo orienta SÓ os conectores legados
    tool_names = ", ".join(f"`{t['name']}`" for t in mcp_tools)
    first = mcp_tools[0]
    first_name = first["name"]
    import re as _re
    _ops_raw = str(first.get("operations") or "").strip()
    _ops_match = _re.search(r"[a-zA-Z][a-zA-Z0-9_]*", _ops_raw)
    first_op = _ops_match.group(0) if _ops_match else "search"
    # Lista completa das operations da PRIMEIRA tool pra dar contexto ao LLM
    # gerador. Pra skills com múltiplas tools, cada uma já aparece no
    # obligatory_sections de ## Tool Bindings com suas operations.
    ops_display = _ops_raw if _ops_raw else "(operations não declaradas no Registry)"
    parts.append(
        "[MCP] **Tools registradas:** " + tool_names + ". "
        "Use os NOMES EXATOS dessas tools em Workflow e Examples.\n"
        f"  - Verbo recomendado: **Chame** / **Invoque**.\n"
        f"  - **Operations disponíveis em `{first_name}`: `{ops_display}`**\n"
        f"  - **REGRA CRÍTICA — operations:** Use APENAS uma operation listada "
        f"acima (e em `## Tool Bindings` do bloco obrigatório). NUNCA invente "
        f"nomes de operation como `search`, `query`, `fetch`, `get` se não "
        f"estiverem declarados. O servidor MCP REJEITA operations inventadas "
        f"e o LLM em runtime devolve erro ao usuário.\n"
        f"  - Exemplo no Workflow: \"Chame a tool `{first_name}` com "
        f"`operation={first_op}` e `query=<entrada do usuário>` ANTES de gerar a "
        "resposta.\"\n"
        f"  - Exemplo no Examples: `**Chamada à tool:** `{first_name}` "
        f"operation=`{first_op}` query=`<...>``\n"
        "  - Quando NÃO há ## Evidence Policy no bloco obrigatório (skill só "
        "com MCP), a seção Evidence Policy deve dizer: \"_A única fonte "
        f"autorizada é o binding **{first_name}** declarado em ## Tool Bindings._\""
    )
    return "\n".join(parts)


def _canonical_mcp_inputs_block() -> str:
    """Bloco ## Inputs canônico para tool MCP: contrato `{operation, query}` que
    o runtime entende (`build_openai_tools`). O ENUM das ops é injetado em runtime
    a partir do Registry (`_make_parameters_from_inputs_schema`), então aqui basta
    `operation` como string — não precisa plumbar as ops na geração.
    """
    import json as _json
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Entrada da ferramenta MCP",
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": (
                    "Operação MCP a executar (ex.: search, extract). O servidor "
                    "declara as válidas — o runtime injeta o enum."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Parâmetro da operação: consulta em linguagem natural para "
                    "busca; URL para extract/crawl/map."
                ),
            },
        },
        "required": ["operation", "query"],
    }
    return "## Inputs\n```json\n" + _json.dumps(schema, ensure_ascii=False, indent=2) + "\n```"


def _inputs_has_operation(skill_md: str) -> bool:
    """True se a seção ## Inputs declara a propriedade `operation` (o contrato
    MCP). Tolerante: parse falho → fallback substring."""
    import re as _re, json as _json

    m = _re.search(r"(?ms)^##\s+Inputs\b(.*?)(?=^##\s|\Z)", skill_md or "")
    if not m:
        return False
    section = m.group(1)
    fence = _re.search(r"```(?:json)?\s*(.*?)```", section, _re.DOTALL)
    body = fence.group(1) if fence else section
    try:
        d = _json.loads(body)
    except Exception:
        return '"operation"' in section
    props = d.get("properties", {}) if isinstance(d, dict) else {}
    return "operation" in props


def _ensure_decisions_contract(skill_md: str, decisions: Optional[dict]) -> str:
    """Força a seção ## Decisions CANÔNICA pós-geração (mesmo espírito do
    `_ensure_mcp_inputs_contract` logo abaixo): o contrato veio SELADO do
    formulário — drift/omissão/tradução do LLM gerador não pode alterá-lo
    (major do review pré-push: o LLM podia emitir {"escalate": ["yes"]} ou
    omitir a seção, e as arestas escritas contra os valores DECLARADOS nunca
    casariam). Determinístico — sem retry de LLM."""
    if not decisions:
        return skill_md
    from app.skill_parser.decisions_schema import extract_decisions_schema
    if extract_decisions_schema(skill_md or "") == decisions:
        return skill_md
    block = "## Decisions\n```json\n" + json.dumps(decisions, ensure_ascii=False) + "\n```"
    sec_re = re.compile(r"\n*##\s+Decisions[\s\S]*?(?=\n## |\s*$)")
    if sec_re.search(skill_md or ""):
        return sec_re.sub("\n\n" + block, skill_md, count=1)
    for anchor in ("## Failure Modes", "## Guardrails"):
        idx = (skill_md or "").find(anchor)
        if idx > 0:
            return skill_md[:idx] + block + "\n\n" + skill_md[idx:]
    return (skill_md or "").rstrip() + "\n\n" + block + "\n"


def _ensure_mcp_inputs_contract(skill_md: str, mcp_tools: list[dict]) -> str:
    """Determinístico: se a SKILL vincula tool MCP mas o ## Inputs NÃO declara
    `operation`, substitui o ## Inputs pelo contrato canônico `{operation, query}`.

    Corrige a causa-raiz do bug "tavily a" (2026-06-08): o LLM gerador modelava
    inputs de DOMÍNIO (address/radius_meters) pela finalidade da skill; sem
    `operation`, o runtime usava o nome do servidor como tool → "Unknown tool" →
    bolha vazia. Só toca quando está ERRADO (MCP + sem operation) → baixa
    regressão. Idempotente: roda sem efeito se já estiver no contrato.
    """
    if not mcp_tools or _inputs_has_operation(skill_md):
        return skill_md
    # F4 — modo per-tool (flag ON): cada tool MCP é sua própria função com o
    # schema real; o runtime ignora o contrato genérico {operation, query}
    # (build_openai_tools expande per-tool). Não reescreve o ## Inputs.
    try:
        from app.mcp.runtime import per_tool_enabled
        if per_tool_enabled():
            return skill_md
    except Exception:
        pass
    import re as _re

    pat = _re.compile(r"(?ms)^##\s+Inputs\b.*?(?=^##\s|\Z)")
    if not pat.search(skill_md or ""):
        # Sem seção ## Inputs → conteúdo não é uma SKILL plausível (lixo/parse
        # error). NÃO anexa — deixa o parser/validador lidar. Replace-only.
        return skill_md
    return pat.sub(_canonical_mcp_inputs_block() + "\n\n", skill_md, count=1)


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


def _slug_id(name: str) -> str:
    """Slug curto e seguro p/ id de binding de tabela (alnum + underscore)."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s[:40] or "tabela"


def _summarize_table_schema(schema_json) -> tuple[str, list[str]]:
    """Extrai (resumo p/ prompt, lista de NOMES de coluna) de schema_json.

    `schema_json` de data_tables é uma LISTA `[{name,type,nullable}]` — NÃO um
    dict `{columns:[...]}`. O código antigo tratava como dict → `parsed.get(
    'columns')` em lista falhava → schema_summary virava sempre '(sem schema)',
    e o LLM nunca via os nomes de coluna reais. Tolera string JSON, lista ou o
    formato dict legado.
    """
    try:
        parsed = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
    except (json.JSONDecodeError, TypeError):
        return "(schema não-parseável)", []
    if isinstance(parsed, dict):
        cols = parsed.get("columns") or []
    elif isinstance(parsed, list):
        cols = parsed
    else:
        cols = []
    names = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")]
    summary = ", ".join(
        f"{c.get('name')}:{c.get('type')}" for c in cols[:12] if isinstance(c, dict)
    ) or "(sem schema)"
    return summary, names


def _tables_block(data_tables: list[dict]) -> str:
    """Sub-bloco de Data Tables (RAG-Tabela Tier 1). A consulta é PARAMETRIZADA
    no bloco `## Data Tables` (select/filters/op com bind vars); o engine executa
    via DuckDB. O LLM **NÃO** escreve SQL e **NÃO** consulta a tabela 'de cabeça'
    — só referencia o RESULTADO (via as chaves do `output_mapping`)."""
    table_refs = ", ".join(f"`{t['urn']}`" for t in data_tables)
    first = data_tables[0]
    slug = _slug_id(first.get("name") or "tabela")
    return (
        "[TABLES] **Tabelas registradas:** " + table_refs + ". "
        "A consulta é PARAMETRIZADA no bloco `## Data Tables` (select/filters/op); "
        "o engine executa via DuckDB com bind vars. O LLM **NÃO escreve SQL**.\n"
        "  - O frontmatter DEVE ter `execution_mode: declarative` (senão a tabela "
        "NUNCA é lida — o agente vira LLM puro e recusa por falta de evidência).\n"
        "  - O `inputs_schema` (## Inputs) DEVE conter os campos usados nos "
        "`filters` da tabela (a chave do filtro) — senão a consulta falha em runtime.\n"
        "  - No Workflow, NÃO escreva 'SQL' nem 'SELECT'. Descreva: \"O engine "
        f"consulta a tabela e disponibiliza o resultado em `context.{slug}_resultado`; "
        "componha a resposta a partir desse resultado.\"\n"
        "  - No Output Contract / Examples, reflita os campos do RESULTADO da "
        "tabela — não invente colunas fora do schema declarado.\n"
        "  - NÃO gere a seção `## Response Template`: sem ela o engine renderiza "
        "as linhas retornadas como tabela (render default). O operador adiciona "
        "o template à mão SÓ se quiser frase customizada."
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


# ─────────────────────────────────────────────────────────────────────
# Corpos do esqueleto por nível de verbosidade (68.0.0).
#
# O que VARIA entre níveis é SÓ o corpo abaixo (lista de seções + regras de
# tamanho + linha de tom). O que NUNCA varia — a armadura anti-alucinação com
# histórico de bugs reais: anti_halluc_rules (regras 1-7), as regras G1-G4 de
# binding_invocation_rules, o bloco SEÇÕES OBRIGATÓRIAS (YAMLs prontos,
# passo 1 do Workflow injetado, placeholder de Tool Bindings, Execution
# Profile explícito) e os pós-processadores _ensure_*_contract.
#
# 'didatico' é BYTE-IDÊNTICO ao corpo anterior a 68.0.0 — provado por golden
# em tests/fixtures/wizard_prompt_golden_didatico_*.txt. Não reformate.
#
# Fios de alta tensão preservados no 'enxuto' (wizard_validator dispararia
# retry sem eles): verbo imperativo nos passos do Workflow (G1.no_imperative)
# e o passo 1 com `operation=` do bloco obrigatório (operation.missing).
# ─────────────────────────────────────────────────────────────────────

_WIZARD_BODY_DIDATICO = """## Purpose
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

Gere o SKILL.md completo em formato markdown. Seja específico e detalhado."""

_WIZARD_BODY_PADRAO = """## Purpose
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
Pares entrada/saída para avaliação — NO MÁXIMO 2 exemplos. Os bindings
(tools MCP, sources RAG, endpoints API, tabelas) referenciados nos exemplos
DEVEM bater com o bloco obrigatório — sem invenções.
Quando esta skill tem QUALQUER binding declarado (MCP, RAG, API, Tabelas),
cada exemplo DEVE rastrear a interação com o binding (Entrada → Ação no
binding → Resposta do binding → Saída final) antes do output final —
exemplo que pula direto pra saída ensina o LLM em runtime a alucinar.

IMPORTANTE: NÃO inclua a seção `## Budget` (limites de tokens, tempo ou custo).
Restrições de budget devem ser definidas pelo operador depois, conscientemente —
gerar valores automáticos prejudica o desempenho do agente em runtime sem ganho
real (LLM não sabe o ROI/budget aceitável do caso de uso). Seção é opcional no
parser, então omitir é seguro.

REGRAS DE CONCISÃO (nível PADRÃO):
- Purpose, Workflow, Output Contract e Guardrails entram VERBATIM no prompt de
  execução a CADA invocação da skill — e Output Contract + Guardrails de novo
  no prompt do juiz de qualidade. Escreva-as objetivas: sem parágrafos
  introdutórios, sem repetir o que outra seção já declara.
- NÃO gere prosa didática/explicativa além do necessário para operar a skill.

Gere o SKILL.md completo em formato markdown. Seja específico e completo, mas conciso — cada seção no tamanho necessário para operar a skill, e nada mais."""

_WIZARD_BODY_ENXUTO = """## Purpose
Declaração imperativa do que este agente faz e do que NÃO faz. No máximo 2 frases.

## Activation Criteria
Condições sob as quais este skill deve ser selecionado. 2-3 bullets de UMA linha.

## Inputs
Schema tipado do envelope esperado em formato JSON Schema. Só o fence JSON, sem prosa ao redor.

## Workflow
Sequência de passos do workflow. Para subagentes, linear. Para roteadores, DAG.
UMA linha por passo, cada passo começando com verbo imperativo.

## Tool Bindings
Lista de tools MCP **estritamente** das fornecidas no bloco obrigatório.
Se o bloco obrigatório declara "sem tools MCP", reproduza essa declaração
sem listar tools inventadas.

## Output Contract
Schema tipado da saída esperada. Só o schema, sem prosa ao redor.

## Failure Modes
Enumeração de falhas e ação prescrita. UMA linha por falha.

## Guardrails
Políticas de conteúdo, PII, jurisdição. Até 5 itens de UMA linha.

## Examples
UM único exemplo entrada/saída. Se a skill tem QUALQUER binding declarado
(MCP, RAG, API, Tabelas), o exemplo DEVE rastrear a interação com o binding
(Entrada → Ação no binding → Resposta do binding → Saída final) antes do
output final — exemplo que pula direto pra saída ensina o LLM em runtime a
alucinar.

IMPORTANTE: NÃO inclua a seção `## Budget` nem seções além das listadas acima,
das do bloco SEÇÕES OBRIGATÓRIAS e das exigidas explicitamente pelos SUB-BLOCOS
POR TIPO DE BINDING (ex.: `## Evidence Policy` de UMA linha quando indicada
para skill só-MCP). Restrições de budget são definidas pelo operador depois,
conscientemente.

REGRAS DE CONCISÃO (nível ENXUTO — CRÍTICAS):
- Purpose, Workflow, Output Contract e Guardrails entram VERBATIM no prompt de
  execução a CADA invocação da skill — e Output Contract + Guardrails de novo
  no prompt do juiz de qualidade, sem teto. Cada caractere delas é custo
  recorrente: escreva o MÍNIMO que opera a skill corretamente.
- ZERO prosa didática: sem parágrafos introdutórios, sem justificativas, sem
  repetir o que outra seção já declara.
- NUNCA encurte à custa de: verbos imperativos nos passos do Workflow, blocos
  YAML/JSON do bloco SEÇÕES OBRIGATÓRIAS (copie-os VERBATIM) e o rastro de
  binding no exemplo.

Gere o SKILL.md completo em formato markdown. Seja específico e enxuto."""

_WIZARD_PROMPT_BODIES = {
    "didatico": _WIZARD_BODY_DIDATICO,
    "padrao": _WIZARD_BODY_PADRAO,
    "enxuto": _WIZARD_BODY_ENXUTO,
}


def _build_wizard_prompt(
    data: WizardSkillRequest, bindings: dict, exec_mode: str,
    verbosity: Optional[str] = None,
) -> tuple[str, str]:
    """Monta system + user prompts pro LLM gerar o SKILL.md.

    Tudo que antes ficava concatenado no frontend (mcpContext, apiContext,
    execContext) agora é construído aqui no servidor a partir de IDs
    estruturados — nomes humanos vêm do lookup, não de string passada
    pelo cliente. Mais robusto + testável.

    `verbosity` (68.0.0): nível explícito {enxuto,padrao,didatico}; None ou
    valor fora do enum = lê o setting `wizard_verbosity` da plataforma.

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
        #
        # MUDANÇA 2026-05-29 (bug runtime Context7 v2): SKILL gerada pediu
        # `operation=search` mas servidor MCP Context7 só aceita docs/code/
        # prompt. Em runtime gpt-oss-120b seguiu a SKILL e o servidor recusou.
        # Causa raiz: obligatory_sections do MCP NÃO listava as operations
        # declaradas no Registry — só id+name+description. LLM gerador via
        # exemplo `operation=docs` no _mcp_block mas, sem lista oficial das
        # operations no bloco obrigatório, escolheu "search" por sonoridade.
        # Fix: incluir operations EXPLICITAMENTE em cada linha do bloco.
        def _format_mcp_line(t):
            line = f"- `{t['id']}` ({t['name']}) — {(t.get('description') or '').strip()[:300]}"
            # 39.2.0 (item 3 PR3): conector em modo per-tool anuncia as tools
            # DESCOBERTAS (nomes reais que viram funções) — operations é
            # conceito do legado e confundiria o gerador.
            _per, _ = _split_tools_by_mode([t])
            if _per:
                from app.mcp.runtime import _parse_discovered_tools
                names = ", ".join(
                    f"`{d['name']}`" for d in
                    _parse_discovered_tools(t.get("discovered_tools"))[:12]
                )
                line += (
                    f"\n  **Tools descobertas (chame pelo NOME REAL; sem "
                    f"operation/query):** {names}"
                )
                return line
            ops_raw = (t.get("operations") or "").strip()
            if ops_raw:
                line += (
                    f"\n  **Operations declaradas (use APENAS estas):** `{ops_raw}`"
                )
            return line
        bindings_md = "\n".join(
            _format_mcp_line(t) for t in bindings["mcp_tools"]
        )
        obligatory_sections.append(
            "## Tool Bindings\n" + bindings_md
        )

        # ──── Pre-injection do passo 1 do Workflow ────
        # MUDANÇA 2026-05-29 (bugs Context7 #1-#4): em 4 tentativas
        # consecutivas, o LLM gerador (gpt-oss-120b) errou o mesmo padrão —
        # omitiu `operation=` no passo do Workflow. Validador pós-geração
        # (PR #186/#188/#191) detecta e roda retry, mas o LLM continua
        # falhando o retry com mesma probabilidade.
        #
        # Fix: parar de confiar no LLM gerador pro passo crítico. O Wizard
        # injeta LITERALMENTE o passo 1 do Workflow em obligatory_sections
        # com a primeira operation declarada no Registry. LLM gerador só
        # escreve os passos 2-N específicos da skill (avaliar, formatar,
        # retornar, etc).
        #
        # Vale só pra primeira tool MCP — skills com 2+ tools precisam que
        # o LLM gerador decida ordem/lógica das chamadas extras (mas o
        # passo 1 sempre é "Chame a primeira com primeira op").
        first_tool = bindings["mcp_tools"][0]
        first_tool_name = first_tool["name"]
        _ops_first = (first_tool.get("operations") or "").strip()
        import re as _re_inject
        _ops_match = _re_inject.search(r"[a-zA-Z][a-zA-Z0-9_]*", _ops_first)
        if _ops_match:
            first_op = _ops_match.group(0)
            workflow_step1 = (
                f"1. **Chame** a tool `{first_tool_name}` com "
                f"`operation={first_op}` e `query=<entrada do usuário>` "
                "ANTES de gerar a resposta."
            )
            obligatory_sections.append(
                "## Workflow\n"
                "(Passo 1 abaixo é OBRIGATÓRIO e LITERAL — NÃO altere "
                f"`operation={first_op}` nem remova `query=`. "
                "Adicione passos 2-N descrevendo como avaliar/formatar/"
                "retornar a resposta da tool.)\n\n"
                + workflow_step1
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
        # Quando ausente, o engine usa default 0.0 desde PR #238 (era 0.3 antes;
        # ver engine.py:_DEFAULT_MIN_RELEVANCE). Filtro de qualidade vira opt-in.
        # Faixa válida [0..1] garantida pelo Pydantic (Field ge/le).
        threshold_yaml = ""
        if data.min_relevance is not None:
            threshold_yaml = f"\nmin_relevance: {data.min_relevance}"
        obligatory_sections.append(
            "## Evidence Policy\n```yaml\nsources:\n" + sources_yaml + threshold_yaml + "\n```"
        )

    if bindings["data_tables"]:
        # RAG-Tabela Tier 1: bloco EXECUTÁVEL (id, table_ref, query{select,filters},
        # output_mapping) — NÃO o formato binding-only (urn/name) que o engine NÃO
        # executa. Quando a tabela tem suggested_pk, gera um filtro de lookup por PK
        # mapeado de um input; senão, select+limit sem filtro (ainda executável).
        # Exige execution_mode: declarative (senão a tabela nunca é lida).
        table_blocks = []
        pk_inputs: list[str] = []
        for t in bindings["data_tables"]:
            cols = t.get("columns") or []
            select_yaml = "[" + ", ".join(cols[:12]) + "]" if cols else "[]"
            slug = _slug_id(t.get("name") or "tabela")
            pk = t.get("suggested_pk")
            if pk:
                pk_inputs.append(pk)
            # WHERE multi-campo: filtro `if_present` p/ CADA coluna do select —
            # só os campos efetivamente informados em inputs filtram (o engine
            # descarta filtros de input ausente ANTES do Jinja; o serviço repete
            # a checagem). A MESMA skill atende qualquer combinação de campos:
            # {cd_cliente: 2}, {uf: "RS", nr_idade: 30}, ou nenhum (lista tudo
            # até o limit).
            filter_cols = cols[:12]
            if filter_cols:
                _flines = ["      filters:"]
                for c in filter_cols:
                    _flines.append(f"        - col: {c}")
                    _flines.append("          op: \"=\"")
                    _flines.append(f"          value: \"{{{{ inputs.{c} }}}}\"")
                    _flines.append(f"          if_present: {c}")
                filters_yaml = "\n".join(_flines) + "\n"
            else:
                filters_yaml = (
                    "      filters: []   # adicione: {col: <coluna>, op: \"=\", "
                    "value: \"{{ inputs.<campo> }}\", if_present: <campo>}\n"
                )
            table_blocks.append(
                f"  - id: {slug}\n"
                f"    table_ref: {t['urn']}\n"
                "    query:\n"
                f"      select: {select_yaml}\n"
                f"{filters_yaml}"
                "      limit: 100\n"
                "    output_mapping:\n"
                f"      {slug}_resultado: \"$.rows\"\n"
                "    on_error: fail"
            )
        refs_md = "\n".join(
            f"- `{t['urn']}` ({t['name']}, ~{t.get('row_count', '?')} linhas): {t.get('schema_summary', '')}"
            for t in bindings["data_tables"]
        )
        if pk_inputs:
            uniq = ", ".join(f"`{p}`" for p in sorted(set(pk_inputs)))
            pk_note = (
                "\n\nIMPORTANTE: o `inputs_schema` (## Inputs) DEVE declarar TODAS as "
                "colunas dos filtros como propriedades — " + uniq + " como OBRIGATÓRIA "
                "(required) e as DEMAIS como OPCIONAIS, com tipos JSON coerentes com o "
                "schema da tabela (BIGINT→integer, DOUBLE→number, VARCHAR→string). "
                "Qualquer combinação de campos informados filtra a consulta (if_present)."
            )
        else:
            pk_note = (
                "\n\nIMPORTANTE: o `inputs_schema` (## Inputs) DEVE declarar as colunas "
                "dos filtros como propriedades OPCIONAIS (sem required), com tipos JSON "
                "coerentes com o schema da tabela. Qualquer combinação de campos "
                "informados filtra a consulta (if_present); sem campos, lista até o limit."
            )
        obligatory_sections.append(
            "INCLUA no frontmatter YAML: `execution_mode: declarative`\n\n"
            "## Data Tables\n```yaml\ntables:\n" + "\n".join(table_blocks) + "\n```"
            "\n\nReferências disponíveis:\n" + refs_md + pk_note
        )

        # NOTA: NÃO emitimos ## Response Template default. Sem o bloco, o engine
        # renderiza as linhas retornadas como tabela markdown (render DEFAULT,
        # com PII-catalogada mascarada) — já mostra os DADOS sem autoria. O
        # ## Response Template fica para frase CUSTOMIZADA (o autor adiciona à
        # mão; orientação no _tables_block). O default antigo (só contagem,
        # "Encontrei N registro(s)") escondia o resultado — removido 2026-06-10.

    if bindings["api_endpoints"]:
        # Formato YAML (atualizado 2026-05-31):
        #   - lista PURA no topo do bloco (sem chave wrapper 'endpoints:').
        #     O parser canônico em skill_parser/parser.py:_parse_api_bindings
        #     agora aceita ambos os formatos por defesa, mas lista pura é o
        #     canônico — alinha com SKILL.md feitas à mão.
        #   - campo `path:` em vez de comentário `# URL: ...`. O linter exige
        #     path; o comentário não era extraído por nada. Engine resolve
        #     host via connector em runtime.
        #   - tanto `connector` quanto `connector_id` ficam presentes —
        #     `connector` para o linter, `connector_id` mantido por compat
        #     com qualquer leitor que já dependa do nome longo.
        api_yaml = "\n".join(
            f"  - id: {ep['ep_id']}\n"
            f"    connector: {ep['conn_id']}\n"
            f"    connector_id: {ep['conn_id']}\n"
            f"    name: {ep['ep_name']}\n"
            f"    method: {ep['method']}\n"
            f"    path: {ep.get('path') or ep['url']}"
            for ep in bindings["api_endpoints"]
        )
        # APIs também exigem frontmatter execution_mode: declarative
        obligatory_sections.append(
            "INCLUA no frontmatter YAML: `execution_mode: declarative`\n\n"
            "## API Bindings\n```yaml\n" + api_yaml + "\n```"
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

    # Cond-C.2 (36.2.0): Contrato de Decisão declarado por formulário. Seção
    # SELADA — o enum fechado é o contrato do gate condicional (`decision.*`);
    # a plataforma injeta a diretiva da linha DECISAO no runtime a partir dela.
    if data.decisions:
        obligatory_sections.append(
            "## Decisions\n```json\n"
            + json.dumps(data.decisions, ensure_ascii=False)
            + "\n```"
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
        + (
            "\n7. Copie a seção ## Decisions (fence JSON) VERBATIM — não traduza "
            "campos/valores, não adicione nem remova valores: é um CONTRATO "
            "selado que o gate condicional valida em runtime."
            if data.decisions else ""
        )
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
    # - Data Tables: query PARAMETRIZADA no bloco ## Data Tables (engine executa
    #   via DuckDB com bind vars; o LLM NÃO escreve SQL). Exige execution_mode:
    #   declarative — sem ele a tabela nunca é lida (agente vira LLM puro).
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

    # Nível de verbosidade: explícito (param — usado por testes e, adiante,
    # pelo campo do request) ou o setting da plataforma. O corpo didático é
    # byte-idêntico ao esqueleto pré-68.0.0 (golden em tests/fixtures/).
    level = verbosity if verbosity in _WIZARD_VERBOSITY_VALUES else _wizard_verbosity()
    corpo = _WIZARD_PROMPT_BODIES[level]

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

{corpo}{obligatory_block}"""

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

        # ── Geração inicial ── (fallback hospedado quando o roteado está
        # inacessível/401; reasoning só chega ao modelo que aceita o param)
        skill_md, used_provider, used_model = await _wizard_llm_complete([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], provider, model, route="skill", reasoning_effort=_wizard_reasoning_effort())
        # Descasca a cerca de código (```markdown … ```) que o LLM às vezes
        # embrulha — o RETORNO (skill_md) ia com o wrapper mesmo o parser
        # tolerando internamente. Mesma lógica do parser (helper compartilhado).
        skill_md = strip_code_fence(skill_md)
        # Contrato MCP (fix bug "tavily a", 2026-06-08): força o ## Inputs ao
        # `{operation, query}` quando há tool MCP e o LLM inventou inputs de
        # domínio. Antes de validar, pra o validador ver a versão corrigida.
        skill_md = _ensure_mcp_inputs_contract(skill_md, bindings.get("mcp_tools") or [])
        # Cond-C.2: contrato de decisão SELADO — corrige drift/omissão do LLM
        # gerador de forma determinística (major do review pré-push).
        skill_md = _ensure_decisions_contract(skill_md, data.decisions)

        # ── Validação pós-geração + retry com instrução corretiva ──
        from app.skill_parser.parser import parse_skill_md
        from app.skill_parser.wizard_validator import validate_generated_skill

        retries_used = 0
        validation_result = None
        try:
            parsed = parse_skill_md(skill_md)
            validation_result = validate_generated_skill(parsed, bindings, raw_md=skill_md)
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
                # Reusa o par que RESPONDEU a geração inicial — se o primário
                # caiu e o fallback atendeu, não re-paga o timeout do morto.
                retry_skill_md, retry_provider, retry_model = await _wizard_llm_complete([
                    {"role": "system", "content": system_prompt + retry_instruction},
                    {"role": "user", "content": user_prompt},
                ], used_provider, used_model, route="skill",
                    reasoning_effort=_wizard_reasoning_effort())
                retry_skill_md = strip_code_fence(retry_skill_md)
                retry_skill_md = _ensure_mcp_inputs_contract(retry_skill_md, bindings.get("mcp_tools") or [])
                retry_skill_md = _ensure_decisions_contract(retry_skill_md, data.decisions)
                # Re-valida o retry — se também violar, mantém o RETRY (geralmente
                # melhor que o original) mas devolve warnings pro operador
                try:
                    parsed_retry = parse_skill_md(retry_skill_md)
                    retry_validation = validate_generated_skill(parsed_retry, bindings, raw_md=retry_skill_md)
                    skill_md = retry_skill_md
                    validation_result = retry_validation
                    retries_used = 1
                    # O retry adotado pode ter saído de OUTRO par (fallback
                    # dentro do próprio retry) — `resolved` reporta quem
                    # gerou o conteúdo FINAL. Se o retry for descartado
                    # (parse error), o par da geração inicial permanece.
                    used_provider, used_model = retry_provider, retry_model
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
                # Wave Wizard Routing: mostra qual LLM REALMENTE respondeu
                # (pós-fallback — pode diferir do roteado quando o hub caiu).
                "llm_provider": used_provider,
                "llm_model": used_model,
                # True quando a resposta veio de par diferente do roteado —
                # reroteio por fallback nunca é silencioso na API.
                "llm_fallback": used_provider != provider,
                "llm_task_type": resolved_task,
            },
        }
        # Validação: só inclui quando rodou. UI pode mostrar warnings/crítico
        # remanescentes pro operador revisar antes de salvar.
        if validation_result is not None:
            result["validation"] = validation_result.to_dict()
            result["validation"]["retries_used"] = retries_used
        return result
    except HTTPException:
        # 503 acionável do _wizard_llm_complete — não re-empacotar como 500.
        raise
    except Exception as e:
        logger.exception("wizard_skill falhou")
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


# ── Refino por camada (kind) ─────────────────────────────────────────
# A diretriz muda o ESPÍRITO do system prompt refinado. Orquestradores
# (AOBD) e Roteadores (AR) precisam de missão cristalina + regras de
# delegação/roteamento; um Subagente (SA) executor quer formato de saída e
# guardrails. SA/desconhecido mantém o comportamento histórico (retrocompat).
_REFINE_PERSONA_SA = (
    "Você é um especialista em refinamento de configurações de IA. "
    "Melhore o conteúdo conforme a instrução do usuário. Responda APENAS "
    "com o conteúdo melhorado, sem explicações adicionais."
)
_REFINE_PERSONA_AOBD = (
    "Você é um especialista em projetar agentes ORQUESTRADORES (camada AOBD). "
    "Reescreva o conteúdo como uma diretiva de orquestração SIMPLES, porém "
    "EXTREMAMENTE CLARA quanto à missão. Estruture em: (1) Missão — 1 frase "
    "imperativa; (2) Critérios de roteamento — quando delegar e para qual "
    "agente/skill; (3) Política de fallback — o que fazer quando nenhuma rota "
    "se aplica; (4) Regra de ouro — o orquestrador NUNCA executa a tarefa "
    "final, apenas decide quem executa e delega. Seja enxuto, direto e "
    "inequívoco. Responda APENAS com o conteúdo melhorado, sem explicações "
    "adicionais."
)
_REFINE_PERSONA_AR = (
    "Você é um especialista em projetar agentes ROTEADORES (camada AR). "
    "Reescreva o conteúdo como uma diretiva de classificação e roteamento. "
    "Estruture em: (1) Missão de triagem — 1 frase; (2) Categorias/intenções "
    "reconhecidas e como diferenciá-las; (3) Destino de cada categoria (para "
    "qual agente/skill encaminhar); (4) Comportamento padrão para entradas "
    "ambíguas ou fora de escopo. Seja objetivo e determinístico. Responda "
    "APENAS com o conteúdo melhorado, sem explicações adicionais."
)


def _refine_persona(kind: str) -> str:
    """System message do /refine conforme a camada (kind) do agente.

    AOBD (Orquestrador) → diretiva de orquestração curta e clara em missão.
    AR / router (Roteador) → diretiva de classificação/roteamento determinística.
    SA / subagent / desconhecido / vazio → comportamento histórico (retrocompat).

    Função pura (sem I/O) — fácil de testar isoladamente.
    """
    k = (kind or "").strip().lower()
    if k == "aobd":
        return _REFINE_PERSONA_AOBD
    if k == "router":
        return _REFINE_PERSONA_AR
    return _REFINE_PERSONA_SA


@router.post("/refine")
async def wizard_refine(data: WizardRefineRequest):
    """Wizard IA: refina/melhora um campo ou conteúdo existente.

    Wave Wizard Routing: usa task_type=instruct (default) — refinamento é
    instruction-following, modelo menor (gpt-oss-20b por padrão) basta.

    Refino por camada (kind): a persona/diretriz muda conforme o tipo do
    agente — AOBD recebe diretiva de orquestração, AR de roteamento, SA o
    comportamento histórico. Ver _refine_persona().
    """
    persona_kind = (data.kind or "").strip().lower() or "subagent"
    try:
        provider, model, _ = await _resolve_wizard_llm(data, "refine")
        messages = [
            {"role": "system", "content": _refine_persona(data.kind)},
            {"role": "user", "content": f"Campo: {data.field}\n\nConteúdo atual:\n{data.current_content}\n\nInstrução de melhoria:\n{data.instruction}"},
        ]
        refined, used_provider, used_model = await _wizard_llm_complete(
            messages, provider, model, route="refine"
        )
        logger.info(
            "wizard.refine.completed",
            extra={
                "event": "wizard.refine.completed",
                "kind": persona_kind,
                "field": data.field,
                "provider": used_provider,
                "model": used_model,
            },
        )
        return {"status": "ok", "refined": refined}
    except HTTPException:
        raise
    except Exception as e:
        # Tracing estruturado: kind/field ajudam a diagnosticar refino que
        # falhou por camada (ex.: provider sem credencial p/ task_type).
        logger.exception(
            "wizard.refine.failed",
            extra={
                "event": "wizard.refine.failed",
                "kind": persona_kind,
                "field": data.field,
            },
        )
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


# ── Pergunte ao mentor (chat contextual) ─────────────────────────────
# Um chat LLM dentro do painel Mentor da tela de criação de agentes. O
# iniciante pergunta "como faço X?" e o mentor responde JÁ SABENDO a camada
# (kind) escolhida e o estado atual do form (nome, prompt, prontidão). Reusa
# a mesma infra de roteamento do /refine (task_type=instruct por default da
# rota — conversa-guia é instruction-following, modelo menor basta).
#
# Pureza testável: _mentor_persona (qual diretriz por camada) e
# _build_mentor_context (como serializar o estado do form) são funções puras
# — testadas isoladamente sem I/O, espelhando o padrão de _refine_persona.

# Regras de comportamento COMUNS a todas as camadas. O mentor é um guia para
# INICIANTES — fala simples, traduz jargão, é acionável (cita os botões REAIS
# da tela) e nunca despeja teoria. Estas regras ancoram o tom independente da
# camada.
_MENTOR_RULES = (
    "Você é o Mentor de Agentes da plataforma Maestro — um guia paciente e "
    "prático que ajuda QUALQUER iniciante a criar um ótimo agente, ali mesmo "
    "na tela de criação. Siga estas regras SEMPRE:\n\n"
    "1. Responda em português do Brasil, em tom acolhedor e direto. Seja "
    "conciso — no máximo ~6 frases curtas, salvo se o usuário pedir mais.\n"
    "2. Traduza qualquer jargão técnico (RAG, pass-through, task_type, AI "
    "Mesh, system prompt) para linguagem do dia a dia ANTES de usá-lo.\n"
    "3. Seja ACIONÁVEL: aponte o próximo passo concreto citando os botões "
    "REAIS desta tela quando fizer sentido — 'Estrutura' (gera o esqueleto do "
    "prompt), 'Compor missão' (monta a missão e as rotas de delegação), "
    "'Sincronizar com AI Mesh' (conecta os agentes de destino), 'Exigir "
    "Evidência' (liga o RAG) e 'Vincular Skill' (dá conhecimento ao agente).\n"
    "4. Use o ESTADO ATUAL e a PRONTIDÃO do agente (fornecidos no contexto) "
    "para personalizar — comece pelo item pendente mais importante.\n"
    "5. NÃO escreva o system prompt inteiro a menos que o usuário peça "
    "explicitamente; prefira orientar para que ele use 'Estrutura' ou "
    "'Compor missão' e depois refine.\n"
    "6. Se a pergunta fugir do tema (não for sobre criar/configurar este "
    "agente), reconduza gentilmente ao objetivo da tela.\n"
    "7. Nunca invente recursos que a tela não tem; baseie-se nos botões e no "
    "estado informado."
)

# Foco específico por camada — costurado depois das regras comuns. Cada
# camada tem um "espírito" diferente (mesmo critério do _refine_persona).
_MENTOR_PERSONA_AOBD = (
    "CAMADA EM FOCO: 🎼 Maestro (Orquestrador / AOBD). O usuário está criando "
    "um agente que NUNCA executa a tarefa final — ele interpreta a intenção e "
    "DELEGA para outros agentes/skills. Guie-o para: uma missão cristalina, "
    "pelo menos 2 rotas de delegação ('quando X → delegar a Y'), uma política "
    "de fallback e conectar os destinos no AI Mesh. Reforce a regra de ouro: "
    "orquestrador decide quem faz, não faz."
)
_MENTOR_PERSONA_AR = (
    "CAMADA EM FOCO: 🧭 Triagem (Roteador / AR). O usuário está criando um "
    "agente que CLASSIFICA a entrada e a encaminha para o destino certo. "
    "Guie-o para: uma missão de triagem clara, pelo menos 2 categorias com "
    "seus destinos ('quando X → encaminhar para Y') e um comportamento padrão "
    "para entradas ambíguas ou fora de escopo. Determinismo é a virtude aqui."
)
_MENTOR_PERSONA_SA = (
    "CAMADA EM FOCO: 🎯 Especialista (Subagente / SA). O usuário está criando "
    "um agente que EXECUTA uma tarefa atômica muito bem. Guie-o para: "
    "instruções reais e específicas (não genéricas), dar conhecimento ao "
    "agente (Vincular uma Skill ou ligar 'Exigir Evidência'/RAG) e definir um "
    "formato de saída claro. Quanto mais concreta a instrução, melhor o "
    "resultado."
)


def _mentor_persona(kind: str) -> str:
    """System message do /mentor conforme a camada (kind) do agente.

    aobd → foco em orquestração/delegação; router → triagem/roteamento;
    subagent/desconhecido/vazio → execução especialista (default).

    Função pura (regras comuns + foco da camada) — fácil de testar.
    """
    k = (kind or "").strip().lower()
    if k == "aobd":
        focus = _MENTOR_PERSONA_AOBD
    elif k == "router":
        focus = _MENTOR_PERSONA_AR
    else:
        focus = _MENTOR_PERSONA_SA
    return _MENTOR_RULES + "\n\n" + focus


# Rótulo humano por camada — usado no bloco de contexto do estado.
_MENTOR_LAYER_LABEL = {
    "aobd": "🎼 Maestro (Orquestrador)",
    "router": "🧭 Triagem (Roteador)",
    "subagent": "🎯 Especialista (Subagente)",
}


def _build_mentor_context(data) -> str:
    """Serializa o estado atual do form num bloco de contexto pro LLM.

    O mentor responde sabendo: a camada, o nome, um resumo do system prompt e
    a prontidão (itens feitos vs pendentes). Isso permite respostas
    personalizadas ("seu próximo passo é …") em vez de genéricas.

    Função pura — recebe o request e devolve string. Sem I/O.
    """
    kind = (getattr(data, "kind", "") or "subagent").strip().lower()
    layer = _MENTOR_LAYER_LABEL.get(kind, _MENTOR_LAYER_LABEL["subagent"])
    name = (getattr(data, "agent_name", "") or "").strip() or "(sem nome ainda)"
    prompt = (getattr(data, "system_prompt", "") or "").strip()
    if prompt:
        resumo = prompt[:800]
        if len(prompt) > 800:
            resumo += " …"
    else:
        resumo = "(System Prompt ainda vazio)"

    lines = [
        "[ESTADO ATUAL DO AGENTE]",
        f"Camada: {layer}",
        f"Nome: {name}",
        f"System Prompt (resumo): {resumo}",
    ]

    # Prontidão — checklist vivo enviado pelo frontend. Lista o que falta pra
    # o mentor priorizar o próximo passo concreto.
    checklist = getattr(data, "checklist", None) or []
    if checklist:
        done = [c for c in checklist if (isinstance(c, dict) and c.get("done"))]
        pending = [
            c.get("label", "?")
            for c in checklist
            if isinstance(c, dict) and not c.get("done")
        ]
        lines.append(f"Prontidão: {len(done)}/{len(checklist)} itens concluídos.")
        if pending:
            lines.append("Pendências (priorize): " + "; ".join(pending) + ".")
        else:
            lines.append("Todos os itens da prontidão estão concluídos.")

    return "\n".join(lines)


class WizardMentorRequest(BaseModel):
    """Request do chat 'Pergunte ao mentor' no painel Mentor.

    O frontend envia a pergunta + o estado atual do form (camada, nome,
    system prompt, checklist de prontidão) + o histórico recente da conversa.
    O backend monta system (persona da camada + contexto) e responde.
    """
    question: str
    kind: str = "subagent"
    agent_name: str = ""
    system_prompt: str = ""
    # Checklist vivo do painel Mentor: [{label, done}]. Personaliza a resposta.
    checklist: list[dict] = Field(default_factory=list)
    # Histórico recente: [{role: 'user'|'assistant', content: str}].
    history: list[dict] = Field(default_factory=list)
    # Wave Wizard Routing: task_type=instruct por default da rota /mentor.
    task_type: Optional[str] = ""
    # Legacy (retrocompat).
    provider: str = "openai"
    model: Optional[str] = ""


# Quantos turnos anteriores reaproveitar como contexto da conversa. 6 (≈3
# idas e voltas) equilibra continuidade e custo — o estado do form já vai no
# system, então o histórico só precisa cobrir o fio recente do diálogo.
_MENTOR_HISTORY_MAX = 6


@router.post("/mentor")
async def wizard_mentor(data: WizardMentorRequest):
    """Wizard IA: chat contextual do Mentor de Agentes.

    Responde dúvidas do iniciante JÁ SABENDO a camada escolhida e o estado
    atual do form (nome, prompt, prontidão). Reusa a infra de roteamento do
    wizard (task_type=instruct por default — conversa-guia é
    instruction-following).
    """
    question = (data.question or "").strip()
    if not question:
        raise HTTPException(400, "Pergunta vazia — escreva o que você quer saber.")

    persona_kind = (data.kind or "").strip().lower() or "subagent"
    try:
        provider, model, _ = await _resolve_wizard_llm(data, "mentor")

        system_content = (
            _mentor_persona(data.kind) + "\n\n" + _build_mentor_context(data)
        )
        messages = [{"role": "system", "content": system_content}]

        # Histórico recente — sanitizado: só roles válidos, conteúdo truncado
        # pra não estourar contexto se o frontend mandar turnos enormes.
        for turn in (data.history or [])[-_MENTOR_HISTORY_MAX:]:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:2000]})

        messages.append({"role": "user", "content": question})

        answer, used_provider, used_model = await _wizard_llm_complete(
            messages, provider, model, route="mentor"
        )
        logger.info(
            "wizard.mentor.completed",
            extra={
                "event": "wizard.mentor.completed",
                "kind": persona_kind,
                "provider": used_provider,
                "model": used_model,
                "history_turns": min(len(data.history or []), _MENTOR_HISTORY_MAX),
            },
        )
        return {"status": "ok", "answer": answer}
    except HTTPException:
        raise
    except Exception:
        # Tracing estruturado: kind ajuda a diagnosticar falha por camada
        # (ex.: provider sem credencial pro task_type da rota).
        logger.exception(
            "wizard.mentor.failed",
            extra={
                "event": "wizard.mentor.failed",
                "kind": persona_kind,
            },
        )
        raise HTTPException(500, "Erro no mentor — tente novamente em instantes.")


# ─── "IA, me ajude!" — compor missão/triagem estruturada no Composer ──────
# O Composer monta o System Prompt por CAMPOS (missão, regras quando→destino,
# fallback, regra de ouro). Antes ele abria VAZIO: o usuário olhava a tela em
# branco sem saber por onde começar. Aqui a IA gera um RASCUNHO desses campos a
# partir de uma intenção em linguagem natural — ANCORADO no catálogo real de
# skills/agentes do usuário (reduz alucinação de destinos que não existem). O
# frontend NÃO auto-aplica: preenche os campos como rascunho e o usuário revisa
# antes de "Aplicar ao System Prompt". A "Verificação de roteamento" existente
# sinaliza destinos sem match (⚠ texto livre).

# Regras de saída comuns às duas camadas (orquestrador/roteador). Não usa
# f-string: as chaves do exemplo JSON são literais.
_COMPOSE_RULES = (
    "Você é um arquiteto de agentes de IA ajudando o usuário a COMPOR a "
    "configuração de um agente coordenador. Você devolve um RASCUNHO que o "
    "usuário vai REVISAR — seja concreto e fiel ao catálogo de destinos "
    "fornecido.\n"
    "REGRAS DE SAÍDA (obrigatórias):\n"
    "- Responda APENAS com JSON válido. Sem markdown, sem cercas ```, sem "
    "nenhum texto fora do JSON.\n"
    "- Use EXATAMENTE esta estrutura:\n"
    "{\n"
    '  "statement": "missão em 1-2 frases, na voz do agente",\n'
    '  "rules": [{"when": "intenção/condição da entrada", "target": "nome do destino"}],\n'
    '  "fallback": "o que fazer quando nenhuma regra se aplica",\n'
    '  "goldenRule": true\n'
    "}\n"
    "- Em cada 'target', use SEMPRE o nome EXATO de um AGENTE do catálogo "
    "fornecido abaixo. Os destinos de roteamento são AGENTES (nós da malha) — "
    "skills NÃO são destinos válidos (são capacidades internas de um agente, "
    "não recebem roteamento). Só proponha um AGENTE fora do catálogo quando "
    "nenhum existente cobrir a necessidade — e então use um nome curto e "
    "descritivo de agente (ex.: 'Agente de Cobrança').\n"
    "- Gere de 2 a 5 regras cobrindo os principais caminhos.\n"
    "- Escreva em português do Brasil."
)

_COMPOSE_PERSONA_AOBD = (
    "CAMADA: 🎼 Orquestrador (AOBD). Ele interpreta a intenção do usuário e "
    "DELEGA para outros AGENTES — nunca executa a tarefa final. As regras são "
    "critérios de delegação ('quando X → delegar ao agente Y'); o 'target' é "
    "SEMPRE um agente (skills não recebem delegação). Defina goldenRule=true: "
    "o orquestrador decide quem faz, não faz."
)
_COMPOSE_PERSONA_AR = (
    "CAMADA: 🧭 Roteador (AR). Ele CLASSIFICA a entrada e a encaminha para o "
    "AGENTE certo. As regras são categorias com destinos ('quando a entrada "
    "for X → encaminhar para o agente Y'); o 'target' é SEMPRE um agente. "
    "Defina goldenRule=false (não se aplica ao roteador). O 'fallback' "
    "descreve o comportamento para entradas ambíguas ou fora de escopo."
)


def _compose_persona(kind: str) -> str:
    """System message do /compose conforme a camada (aobd vs router).

    Função pura (regras comuns + foco da camada) — fácil de testar.
    """
    k = (kind or "").strip().lower()
    focus = _COMPOSE_PERSONA_AR if k == "router" else _COMPOSE_PERSONA_AOBD
    return _COMPOSE_RULES + "\n\n" + focus


def _compose_catalog_names(items) -> list[str]:
    """Normaliza uma lista de skills/agentes (strings OU dicts {name}) em nomes
    únicos não-vazios, preservando a ordem. Tolerante ao que o frontend manda."""
    out: list[str] = []
    for it in (items or []):
        if isinstance(it, str):
            name = it.strip()
        elif isinstance(it, dict):
            name = str(it.get("name") or "").strip()
        else:
            name = ""
        if name and name not in out:
            out.append(name)
    return out


def _schema_param_names(schema) -> list:
    """Nomes dos parâmetros de um JSON-schema de ## Inputs (chaves de
    'properties'; fallback p/ chaves de topo sem meta). Vazio se não for dict."""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if isinstance(props, dict):
        return [str(k) for k in props.keys()]
    _meta = {"$schema", "type", "required", "properties", "title", "description"}
    return [str(k) for k in schema.keys() if k not in _meta]


async def _collect_destination_inputs(agents) -> dict:
    """Mapa {nome_do_agente: [param,...]} com os parâmetros declarados no
    ## Inputs da skill de cada agente-destino (Slice 2b, 2026-06-07).

    Best-effort + fail-safe: qualquer erro (assinatura de repo, skill ausente,
    parse) é ignorado por destino → o catálogo cai para só-nomes (comportamento
    anterior). É o que permite a IA sugerir params POR destino no /compose.
    """
    names = _compose_catalog_names(agents)
    if not names:
        return {}
    out: dict = {}
    try:
        from app.core.database import agents_repo, skills_repo
        from app.skill_parser.parser import parse_skill_md
        from app.routes.agents import _extract_inputs_schema

        all_agents = await agents_repo.find_all(limit=500)
        by_name = {}
        for a in (all_agents or []):
            nm = str(a.get("name") or "").strip().lower()
            if nm and nm not in by_name:
                by_name[nm] = a
        for name in names:
            a = by_name.get(name.strip().lower())
            if not a or not a.get("skill_id"):
                continue
            try:
                sk = await skills_repo.find_by_id(a["skill_id"])
                if not (sk and sk.get("raw_content")):
                    continue
                parsed = parse_skill_md(sk["raw_content"])
                params = _schema_param_names(_extract_inputs_schema(parsed.inputs or ""))
                if params:
                    out[name] = params
            except Exception:
                continue
    except Exception as e:  # pragma: no cover - lookup best-effort
        logger.warning(
            "wizard.compose.collect_inputs_failed",
            extra={"event": "wizard.compose", "error_type": type(e).__name__,
                   "error_msg": str(e)[:200]},
        )
    return out


def _build_compose_catalog(skills, agents, agent_inputs=None) -> str:
    """Bloco de contexto com o catálogo REAL de destinos disponíveis.

    Os DESTINOS de roteamento são AGENTES (nós da malha). Skills aparecem só
    como CONTEXTO de capacidades já existentes — NÃO são destinos de regra (não
    recebem roteamento na malha; uma regra apontando pra skill é no-op). Aterra
    a IA nos agentes que existem de verdade — principal mitigação contra
    alucinar destinos inexistentes. Listas vazias viram aviso explícito.

    `agent_inputs` (Slice 2b): {nome_do_agente: [param,...]} com os parâmetros
    que cada destino declara (## Inputs). Quando presente, anexa um bloco
    "[PARÂMETROS POR DESTINO]" para a IA sugerir, por destino, quais valores o
    roteador deve EXTRAIR da mensagem (e o roteador os emite p/ o binding da SA).

    Função pura — recebe as listas/mapa e devolve string. Sem I/O.
    """
    sk = _compose_catalog_names(skills)
    ag = _compose_catalog_names(agents)
    lines = ["[CATÁLOGO DE DESTINOS DISPONÍVEIS]"]
    lines.append(
        "Agentes (DESTINOS de roteamento — use estes nomes em 'target'): "
        + (", ".join(ag) if ag else "(nenhum cadastrado)") + "."
    )
    lines.append(
        "Skills (capacidades internas dos agentes — apenas CONTEXTO, NÃO são "
        "destinos de regra): "
        + (", ".join(sk) if sk else "(nenhuma cadastrada)") + "."
    )
    lines.append(
        "Em 'target', use SEMPRE o nome de um AGENTE da lista acima — skills "
        "não recebem roteamento. Se nenhum agente servir, proponha um novo "
        "agente com nome claro."
    )
    ai = agent_inputs or {}
    with_params = [(name, ai.get(name)) for name in ag if ai.get(name)]
    if with_params:
        lines.append("")
        lines.append("[PARÂMETROS POR DESTINO]")
        for name, params in with_params:
            lines.append(f"- {name} requer: {', '.join(params)}.")
        lines.append(
            "Quando uma regra encaminhar para um destino acima, deixe claro que "
            "esses parâmetros devem ser EXTRAÍDOS da mensagem do usuário — o "
            "roteador os repassa ao destino (que chama a ferramenta/API)."
        )
    return "\n".join(lines)


def _parse_compose_json(content: str, kind: str) -> dict:
    """Extrai o rascunho estruturado da resposta do LLM.

    Tolera cercas ```json. Em falha de parse, devolve rascunho mínimo com o
    texto bruto na missão e parsed=False (frontend avisa "revise"). SEMPRE
    devolve as chaves esperadas, sanitizadas — o frontend confia no shape.

    Função pura — fácil de testar contra respostas reais/quebradas do LLM.
    """
    raw = (content or "").strip()
    text = raw
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    is_router = (kind or "").strip().lower() == "router"
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("resposta não é um objeto JSON")
    except (json.JSONDecodeError, ValueError):
        # Graceful: a IA respondeu em texto livre. Não joga fora — vira missão
        # rascunho pro usuário aproveitar/reescrever; parsed=False sinaliza isso.
        return {
            "statement": raw[:1000],
            "rules": [],
            "fallback": "",
            "goldenRule": not is_router,
            "parsed": False,
        }
    statement = str(data.get("statement") or "").strip()
    fallback = str(data.get("fallback") or "").strip()
    rules: list[dict] = []
    for r in (data.get("rules") or []):
        if not isinstance(r, dict):
            continue
        when = str(r.get("when") or "").strip()
        target = str(r.get("target") or "").strip()
        if when or target:
            rules.append({"when": when, "target": target})
    golden = data.get("goldenRule")
    golden_rule = golden if isinstance(golden, bool) else (not is_router)
    return {
        "statement": statement,
        "rules": rules,
        "fallback": fallback,
        "goldenRule": golden_rule,
        "parsed": True,
    }


def _ground_compose_targets(draft: dict, skills, agents) -> dict:
    """Canoniza os 'target' das regras para o nome EXATO de um AGENTE do catálogo.

    Match por nome normalizado (caixa/espaços) SÓ contra AGENTES — os únicos
    destinos válidos de roteamento na malha. Quando casa, troca pelo nome
    canônico do agente, fazendo a "Verificação de roteamento" do frontend
    classificar como agente em vez de ⚠ texto livre por mera divergência de
    caixa. Skills NÃO são canonizadas de propósito: se a IA insistir numa skill
    como destino, ela permanece como veio e o frontend a sinaliza (skill =
    no-op de roteamento). Sem match: mantém como veio (texto livre — sinalizado).

    `skills` é aceito por compatibilidade de assinatura/chamada, mas IGNORADO:
    skills não são destinos de roteamento.

    Muta e devolve o próprio draft (conveniência de encadeamento).
    """
    canon: dict[str, str] = {}
    for name in _compose_catalog_names(agents):
        canon.setdefault(name.strip().lower(), name)
    for rule in draft.get("rules", []):
        key = (rule.get("target") or "").strip().lower()
        if key in canon:
            rule["target"] = canon[key]
    return draft


class WizardComposeRequest(BaseModel):
    """Request do "IA, me ajude!" no Composer de Missão/Triagem.

    O usuário descreve em linguagem natural a intenção do agente coordenador, e
    a IA devolve um RASCUNHO estruturado (missão, regras, fallback, regra de
    ouro) ANCORADO no catálogo real de skills/agentes — para que os destinos de
    delegação casem com alvos que existem de verdade.
    """
    intent: str
    kind: str = "aobd"  # aobd (orquestrador) | router (roteador)
    # Catálogo real do form (frontend manda os nomes de availableSkills/Agents).
    # Aceita strings OU dicts {name} — _compose_catalog_names normaliza.
    skills: list = Field(default_factory=list)
    agents: list = Field(default_factory=list)
    # Wave Wizard Routing: task_type=reasoning por default da rota /compose.
    task_type: Optional[str] = ""
    # Legacy (retrocompat — quando task_type vier, ignora).
    provider: str = "openai"
    model: Optional[str] = ""


class WizardDestinationInputsRequest(BaseModel):
    """Nomes dos agentes-destino — o Compor pede os ## Inputs de cada um p/
    exibir 'requer: <param>' sob cada regra (Slice UI 2026-06-07)."""
    agents: list = Field(default_factory=list)


@router.post("/destination-inputs")
async def wizard_destination_inputs(data: WizardDestinationInputsRequest):
    """{nome_do_agente: [param,...]} com os ## Inputs declarados de cada destino.
    Reusa _collect_destination_inputs (best-effort + fail-safe). Usado pelo
    Composer p/ anotar os params por regra."""
    return {"inputs": await _collect_destination_inputs(data.agents)}


@router.post("/compose")
async def wizard_compose(data: WizardComposeRequest):
    """Wizard IA: "me ajude!" do Composer — gera rascunho de missão/triagem.

    Recebe a intenção em linguagem natural + o catálogo real de destinos e
    devolve {statement, rules:[{when,target}], fallback, goldenRule, parsed}.
    Reusa a infra resiliente do wizard (task_type=reasoning; fallback hospedado
    + 503 acionável quando o modelo está inacessível). O frontend trata como
    RASCUNHO (não auto-aplica).
    """
    intent = (data.intent or "").strip()
    if not intent:
        raise HTTPException(
            400, "Descreva a intenção — o que este agente deve coordenar/triar?"
        )

    kind = (data.kind or "").strip().lower() or "aobd"
    try:
        provider, model, _ = await _resolve_wizard_llm(data, "compose")

        # Slice 2b: olha os ## Inputs de cada destino p/ a IA sugerir params por
        # destino no rascunho (best-effort; sem inputs → catálogo só-nomes).
        agent_inputs = await _collect_destination_inputs(data.agents)
        system_content = (
            _compose_persona(kind)
            + "\n\n"
            + _build_compose_catalog(data.skills, data.agents, agent_inputs)
        )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": intent},
        ]

        content, used_provider, used_model = await _wizard_llm_complete(
            messages, provider, model, route="compose"
        )
        draft = _parse_compose_json(content, kind)
        draft = _ground_compose_targets(draft, data.skills, data.agents)

        logger.info(
            "wizard.compose.completed",
            extra={
                "event": "wizard.compose.completed",
                "kind": kind,
                "provider": used_provider,
                "model": used_model,
                "rules": len(draft.get("rules", [])),
                "parsed": draft.get("parsed", True),
                "catalog_skills": len(_compose_catalog_names(data.skills)),
                "catalog_agents": len(_compose_catalog_names(data.agents)),
            },
        )
        return {"status": "ok", "draft": draft}
    except HTTPException:
        raise
    except Exception:
        # Tracing estruturado: kind ajuda a diagnosticar falha por camada.
        logger.exception(
            "wizard.compose.failed",
            extra={"event": "wizard.compose.failed", "kind": kind},
        )
        raise HTTPException(500, "Erro ao compor — tente novamente em instantes.")


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
        "openai": openai_models,  # alias histórico (Azure)
        # OpenAI público real (api.openai.com) — PR #194. Mesmo catálogo do
        # Azure porque os modelos são os mesmos no provedor da OpenAI; só
        # muda o endpoint (api.openai.com em vez de azure.com).
        "openai_public": openai_models,
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
