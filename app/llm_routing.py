"""LLM Routing por Task Type — Onda 7.

Inverte o paradigma: agent escolhe TIPO DE TAREFA (Tool Calling /
Reasoning / Instruct / Classification), e este módulo resolve qual
provider/model será usado, lendo o roteamento configurável em
`platform_settings` (key-value store).

Defaults (preset inicial — calibrado para open-weight first, com fallback
hospedado para multimodal):
- tool_calling   → gpt-oss-120b/openai/gpt-oss-120b  (function calls / inferência complexa)
- reasoning      → gpt-oss-120b/openai/gpt-oss-120b  (raciocínio em PT-BR)
- instruct       → gpt-oss-20b/openai/gpt-oss-20b    (texto simples, instruction-following)
- classification → gpt-oss-20b/openai/gpt-oss-20b    (estruturação de output)
- multimodal_fallback → azure/gpt-4o                 (único multimodal nativo
                                                      pronto pra produção)

A escolha do GPT-OSS como default é deliberada: open-weight no hub interno
elimina custo por token e mantém soberania de dados em BR (sem trânsito EU/US).
Multimodal fallback continua em Azure GPT-4o porque GPT-OSS atual é text-only.
Operadores podem mudar tudo via /settings → Roteamento LLM sem restart.

Resolver é cache-friendly: load_routing usa um TTL pra não bater no DB
a cada interação.

LiteLLM gateway: este módulo NÃO depende dele. Quando gateway está
ligado, o engine usa o (provider, model) resolvido aqui pra construir
a string `<provider>/<model>` que o LiteLLM aceita. Quando gateway está
desligado, o engine chama o provider direto.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Catálogo ──────────────────────────────────────────────────────

TASK_TYPES = ("tool_calling", "reasoning", "instruct", "classification",
              "skill_generation", "judge")

DEFAULT_ROUTING: dict[str, str] = {
    "tool_calling": "gpt-oss-120b/openai/gpt-oss-120b",
    "reasoning": "gpt-oss-120b/openai/gpt-oss-120b",
    "instruct": "gpt-oss-20b/openai/gpt-oss-20b",
    "classification": "gpt-oss-20b/openai/gpt-oss-20b",
    # skill_generation: tarefa específica de criar/alterar SKILL.md no Wizard.
    # Separado de `reasoning` porque o gerador precisa seguir regras estruturais
    # MUITO específicas (verbos imperativos, operations declaradas, frases
    # proibidas). Default EFETIVO segue o Modelo Primário global da plataforma
    # (ver global_primary_routing + load_routing) — operador pode trocar via UI.
    # O valor abaixo é só o ÚLTIMO recurso, quando nenhum Modelo Primário está
    # configurado em platform_settings.
    "skill_generation": "azure/gpt-4o",
    # judge: "LLM como Juiz" do Verifier §14.2 (MultiDimJudge) — avalia cada
    # resposta em 4 dimensões (factualidade/completude/tom/segurança).
    # Recomendação anti-autopreferência: provedor DIFERENTE do que gera as
    # respostas dos agentes. Default EFETIVO honra a env legada
    # VERIFIER_JUDGE_MODEL quando o operador não salvou rota na UI (ver
    # _apply_judge_env_default) — retrocompat com instalações pré-UI.
    "judge": "azure/gpt-4o",
    "multimodal_fallback": "azure/gpt-4o",
}


def global_primary_routing() -> Optional[str]:
    """Modelo Primário global da plataforma como string de roteamento
    `provider/model`. Lê platform_settings (via get_settings, que reflete o
    que o operador salvou em /settings). None quando não configurado.

    Usado como default EFETIVO de `skill_generation` — "sempre o modelo global
    como default, permitindo override do usuário".
    """
    try:
        from app.core.config import get_settings
        s = get_settings()
    except Exception:
        return None
    p = (getattr(s, "primary_provider", "") or "").strip()
    m = (getattr(s, "primary_model", "") or "").strip()
    if p and m:
        return f"{p}/{m}"
    return None

# Modelos sabidamente multimodais (visão). Atualizar manualmente quando
# catálogo crescer. Lista é used por is_multimodal() pra decidir se
# precisa rotear pro multimodal_fallback quando attachment tem imagem.
MULTIMODAL_MODELS: set[str] = {
    # OpenAI / Azure
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4.1", "gpt-4.1-mini",
    "o1-preview", "o1-mini",
    # Anthropic (caso adicione provider futuro)
    "claude-sonnet-4-5", "claude-opus-4-5", "claude-3-5-sonnet",
    "claude-3-5-haiku",
    # Google (caso adicione)
    "gemini-2.5-pro", "gemini-2.5-flash",
    # Maritaca: Sabiá-4 NÃO suporta visão (text-only)
    # Ollama: depende do modelo; assume false pra ser conservador
}


def is_multimodal(provider: str, model: str) -> bool:
    """True se o modelo aceita imagens. Conservador — falsa-negativa
    é melhor que falsa-positiva (rotear pra fallback funciona; rotear
    pra modelo que não aceita quebra)."""
    if not model:
        return False
    return model.strip() in MULTIMODAL_MODELS


# ─── Cache leve do routing ─────────────────────────────────────────

_routing_cache: dict[str, str] = {}
_routing_cache_at: float = 0.0
_ROUTING_CACHE_TTL_S = 30  # 30s — leve mas reativo a save


def _cache_invalidate() -> None:
    """Força reload no próximo load_routing(). Chamado por save_routing."""
    global _routing_cache_at
    _routing_cache_at = 0.0


async def load_routing() -> dict[str, str]:
    """Lê routing config de platform_settings. Mescla com DEFAULT_ROUTING
    (entries faltantes recebem default).

    Cache TTL 30s — em runtime de execução intensa evita query por
    interação. Save invalida automaticamente.
    """
    global _routing_cache, _routing_cache_at
    now = time.time()
    if _routing_cache and (now - _routing_cache_at) < _ROUTING_CACHE_TTL_S:
        return dict(_routing_cache)

    from app.core.database import settings_store

    out = dict(DEFAULT_ROUTING)
    explicit: set[str] = set()
    try:
        all_settings = await settings_store.get_all()
    except Exception as e:
        logger.warning(f"settings_store.get_all falhou: {e}; usando defaults")
        _apply_skill_generation_global_default(out, explicit)
        _apply_judge_env_default(out, explicit)
        return out  # não cacheia em erro — retenta no próximo load

    for key, value in all_settings.items():
        if key.startswith("llm_routing.") and value:
            short_key = key[len("llm_routing."):]
            if short_key in DEFAULT_ROUTING:
                out[short_key] = value.strip()
                explicit.add(short_key)

    _apply_skill_generation_global_default(out, explicit)
    _apply_judge_env_default(out, explicit)

    _routing_cache = dict(out)
    _routing_cache_at = now
    return out


def _apply_skill_generation_global_default(
    out: dict[str, str], explicit: set[str]
) -> None:
    """Quando o operador NÃO definiu rota explícita pra skill_generation, o
    default segue o Modelo Primário global (não o hardcoded de DEFAULT_ROUTING).
    Mutates `out` in place. No-op se não há Modelo Primário configurado."""
    if "skill_generation" in explicit:
        return
    gm = global_primary_routing()
    if gm:
        out["skill_generation"] = gm


def _apply_judge_env_default(out: dict[str, str], explicit: set[str]) -> None:
    """Retrocompat do papel `judge`: quando o operador NÃO salvou rota
    explícita na UI, o default honra a env legada VERIFIER_JUDGE_MODEL
    (.env/container) — instalações que já configuravam o juiz por env não
    mudam de comportamento ao atualizar. Rota salva na UI SEMPRE vence.
    Mutates `out` in place; no-op se a env não estiver setada."""
    if "judge" in explicit:
        return
    try:
        from app.core.config import get_settings
        s = get_settings()
        vj = (s.verifier_judge_model or "").strip()
    except Exception:
        return
    if not vj or "/" not in vj:
        return
    if vj == "azure/gpt-4o":
        # Default hardcoded intocado pelo operador: honra o DEPLOYMENT Azure
        # real configurado — model explícito vira azure_deployment literal, e
        # instalações com deployment de nome customizado (ex. "meu-gpt4o")
        # dariam 404 DeploymentNotFound em todo julgamento (regressão vs o
        # get_provider("openai") sem model, que usava o deployment da config).
        dep = (getattr(s, "azure_openai_chat_deployment", "") or "").strip()
        if dep and dep != "gpt-4o":
            vj = f"azure/{dep}"
    out["judge"] = vj


async def save_routing(updates: dict[str, str]) -> dict[str, str]:
    """Atualiza routing config. Aceita subset (só keys mencionadas são
    salvas). Valida formato `provider/model` mínimo.

    Retorna o estado completo final (mesclado com defaults).
    """
    from app.core.database import settings_store

    valid_keys = set(DEFAULT_ROUTING.keys())
    payload = {}
    for key, value in (updates or {}).items():
        if key not in valid_keys:
            continue
        v = (value or "").strip()
        if not v:
            continue
        if "/" not in v:
            logger.warning(f"save_routing: '{v}' inválido (esperado provider/model); pulando {key}")
            continue
        payload[f"llm_routing.{key}"] = v

    if payload:
        await settings_store.set_many(payload)
    _cache_invalidate()
    return await load_routing()


# ─── Contingência LLM — nota visível no painel ─────────────────────

# Key no settings_store que controla SE a nota de contingência (cadeia de
# resiliência caiu pro prioritário/fallback) aparece no painel de
# Rastreabilidade do Workspace. Default "true": transparência por padrão.
#
# IMPORTANTE: este flag controla SOMENTE a nota VISÍVEL na UI. A observabilidade
# (ctx.metadata["llm_fallback"]) e os LOGs estruturados (event=agent.llm.fallback*)
# são SEMPRE registrados, independente deste valor — auditoria nunca é silenciada.
_FALLBACK_SHOW_IN_TRACE_KEY = "llm_fallback.show_in_trace"
_FALLBACK_SHOW_IN_TRACE_DEFAULT = True


def _coerce_bool(raw: str, default: bool) -> bool:
    """'true'/'1'/'yes'/'on' → True; 'false'/'0'/'no'/'off' → False.
    String vazia/desconhecida → default."""
    v = (raw or "").strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return default


async def fallback_show_in_trace() -> bool:
    """True se a nota de contingência LLM deve aparecer no painel de
    Rastreabilidade. Lê settings_store (key llm_fallback.show_in_trace).
    Default True (transparência). Erro de leitura → default (não esconde)."""
    try:
        from app.core.database import settings_store
        raw = await settings_store.get(
            _FALLBACK_SHOW_IN_TRACE_KEY, ""
        )
    except Exception as e:
        logger.warning(f"fallback_show_in_trace: leitura falhou ({e}); usando default")
        return _FALLBACK_SHOW_IN_TRACE_DEFAULT
    return _coerce_bool(raw, _FALLBACK_SHOW_IN_TRACE_DEFAULT)


async def set_fallback_show_in_trace(value: bool) -> bool:
    """Persiste o flag da nota de contingência. Retorna o valor salvo."""
    from app.core.database import settings_store
    await settings_store.set(
        _FALLBACK_SHOW_IN_TRACE_KEY, "true" if value else "false"
    )
    return bool(value)


async def resolve_llm_for_task(
    task_type: str,
    has_image: bool = False,
) -> tuple[str, str]:
    """Resolve (provider, model) pra um task_type, considerando multimodal.

    Args:
        task_type: uma das TASK_TYPES.
        has_image: True quando input/attachments contém imagem; força
                   fallback se modelo resolvido for text-only.

    Returns:
        (provider, model) — strings prontas pro engine usar.
    """
    if task_type not in TASK_TYPES:
        # task_type inválido cai no resolved padrão (instruct)
        logger.warning(f"task_type inválido '{task_type}'; usando 'instruct'")
        task_type = "instruct"

    routing = await load_routing()
    resolved = routing.get(task_type) or DEFAULT_ROUTING[task_type]

    provider, model = _split_pm(resolved)

    if has_image and not is_multimodal(provider, model):
        # Preferência (2026-06-01): se o Modelo Primário da plataforma já é
        # multimodal, usa ele no lugar do fallback hardcoded. Sem isso o
        # operador via "Modelo Primário=gpt-4.1" na UI e mesmo assim a request
        # ia pro `multimodal_fallback` (default azure/gpt-4o) — UX confusa.
        # Só dispara quando primary É multimodal; caso contrário cai no fallback
        # como antes.
        primary = global_primary_routing()
        if primary:
            p_provider, p_model = _split_pm(primary)
            if is_multimodal(p_provider, p_model):
                logger.info(
                    f"resolve_llm_for_task: input multimodal → primary {primary} "
                    f"(original {resolved} é text-only; primary é multimodal)"
                )
                return p_provider, p_model
        fallback = routing.get("multimodal_fallback") or DEFAULT_ROUTING["multimodal_fallback"]
        f_provider, f_model = _split_pm(fallback)
        logger.info(
            f"resolve_llm_for_task: input multimodal → fallback {fallback} "
            f"(original {resolved} é text-only)"
        )
        return f_provider, f_model

    return provider, model


def _split_pm(s: str) -> tuple[str, str]:
    """`azure/gpt-4o` → ('azure', 'gpt-4o'). Tolera ausência de provider."""
    if "/" in s:
        p, m = s.split("/", 1)
        return p.strip().lower(), m.strip()
    return "azure", s.strip()


def detect_image_in_attachments(attachments: Optional[list]) -> bool:
    """Heurística: True se alguma anexo do attachments parece imagem.
    Lê `type`, `mime_type`, ou nome com extensão de imagem.

    Usado pelo engine pra decidir se passa has_image=True ao resolver.
    """
    if not attachments:
        return False
    image_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic")
    for att in attachments:
        if not isinstance(att, dict):
            continue
        t = str(att.get("type") or "").lower()
        if "image" in t:
            return True
        mime = str(att.get("mime_type") or att.get("content_type") or "").lower()
        if mime.startswith("image/"):
            return True
        name = str(att.get("name") or "").lower()
        if name.endswith(image_exts):
            return True
    return False
