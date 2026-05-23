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

TASK_TYPES = ("tool_calling", "reasoning", "instruct", "classification")

DEFAULT_ROUTING: dict[str, str] = {
    "tool_calling": "gpt-oss-120b/openai/gpt-oss-120b",
    "reasoning": "gpt-oss-120b/openai/gpt-oss-120b",
    "instruct": "gpt-oss-20b/openai/gpt-oss-20b",
    "classification": "gpt-oss-20b/openai/gpt-oss-20b",
    "multimodal_fallback": "azure/gpt-4o",
}

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
    try:
        all_settings = await settings_store.get_all()
    except Exception as e:
        logger.warning(f"settings_store.get_all falhou: {e}; usando defaults")
        return out

    for key, value in all_settings.items():
        if key.startswith("llm_routing.") and value:
            short_key = key[len("llm_routing."):]
            if short_key in DEFAULT_ROUTING:
                out[short_key] = value.strip()

    _routing_cache = dict(out)
    _routing_cache_at = now
    return out


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
