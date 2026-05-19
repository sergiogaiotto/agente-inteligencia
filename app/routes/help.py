"""Endpoint de IA Help — assistente contextual para o Guia dos Módulos.

POST /api/v1/help/ask
  body: {module_id, module_label, module_section, question, history?: list}
  resp: {answer, model, usage}

Estratégia:
- Carrega o conteúdo do módulo de `app/static/js/module-guide.js` no startup
  (parse mínimo do `window.MODULE_GUIDE = [...]`) para usar como contexto.
- Prompt template injeta: módulo, fundamento, ativar, usar, history, question.
- Chama `get_provider("azure")` (via gateway se ativo, senão direto).
- max_tokens cap pra controlar custo (~$0.001/pergunta).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.llm_providers import get_provider
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)

router = APIRouter(prefix="/api/v1/help", tags=["help"])

MAX_TOKENS = 600
MAX_HISTORY = 5  # turnos passados a manter no contexto

# Stripper simples de HTML para enviar texto ao LLM.
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    no_tags = _HTML_TAG.sub(" ", s)
    return _WS.sub(" ", no_tags).strip()


# Cache do parse do module-guide.js (carrega 1x por processo).
_MODULE_CACHE: dict[str, dict] | None = None


def _load_module_guide() -> dict[str, dict]:
    """Parse mínimo do array MODULE_GUIDE. Procura por entries `{id, section,
    label, fundamento, aplicacao, ativar, usar}` via regex robusta a backticks
    multi-linha. Não depende de execução JS.

    Cacheia em memória — recarrega só com restart.
    """
    global _MODULE_CACHE
    if _MODULE_CACHE is not None:
        return _MODULE_CACHE

    path = Path(__file__).resolve().parent.parent / "static" / "js" / "module-guide.js"
    if not path.exists():
        logger.warning(f"module-guide.js não encontrado em {path}")
        _MODULE_CACHE = {}
        return _MODULE_CACHE

    src = path.read_text(encoding="utf-8")
    # Cada entry começa com `id: 'xxx'`. Pegamos blocos delimitados por `{` ... `},`
    # contendo id/section/label/fundamento/etc. Como Python regex não tem
    # parser JS, usamos heurística: encontra `id: '<x>'` e captura próximos
    # 5 campos via regex específica para template literals (backticks).
    entries: dict[str, dict] = {}
    for m in re.finditer(r"\{\s*id:\s*['\"]([^'\"]+)['\"]", src):
        start = m.start()
        # Encontra o `},` que fecha este objeto no nível raiz do array.
        # Heurística: balanceamento de `{}` ignorando dentro de strings/backticks.
        depth = 0
        in_bt = False  # backtick template literal
        in_str = False
        sc = None
        i = start
        while i < len(src):
            c = src[i]
            if in_bt:
                if c == "`":
                    in_bt = False
                i += 1
                continue
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == sc:
                    in_str = False
                    sc = None
                i += 1
                continue
            if c == "`":
                in_bt = True
                i += 1
                continue
            if c in ("'", '"'):
                in_str = True
                sc = c
                i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = src[start:i + 1]

        def _grab(field: str) -> str:
            # Captura `field: \`...\`` permitindo backticks multi-linha.
            mm = re.search(rf"{field}\s*:\s*`((?:\\`|[^`])*)`", block, re.DOTALL)
            if mm:
                return mm.group(1)
            # Fallback string com aspas simples ou duplas
            mm = re.search(rf"{field}\s*:\s*['\"]((?:\\.|[^'\"])*)['\"]", block, re.DOTALL)
            return mm.group(1) if mm else ""

        eid = m.group(1)
        entries[eid] = {
            "id": eid,
            "section": _grab("section"),
            "label": _grab("label"),
            "fundamento": _strip_html(_grab("fundamento")),
            "aplicacao": _strip_html(_grab("aplicacao")),
            "ativar": _strip_html(_grab("ativar")),
            "usar": _strip_html(_grab("usar")),
        }

    _MODULE_CACHE = entries
    logger.info(f"module-guide carregado: {len(entries)} módulos")
    return entries


class HelpAskRequest(BaseModel):
    module_id: str = Field(..., description="id do módulo (ex: 's14', 'onda4a')")
    module_label: str | None = None
    module_section: str | None = None
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] | None = Field(default=None, description="Turnos anteriores [{role, content}]")


class HelpAskContextRequest(BaseModel):
    """Versão alternativa: recebe o contexto direto do front em vez de buscar
    pelo module_id no module-guide.js. Usado pela "Ajuda desta página"
    (page-specific) que tem dados próprios em helpContent (base.html).
    """
    title: str = Field(..., max_length=200)
    section: str = Field(..., max_length=200)
    what: str = Field(default="", max_length=4000)
    foundation: str = Field(default="", max_length=4000)
    usage: str = Field(default="", max_length=4000)
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] | None = None


class HelpAskResponse(BaseModel):
    answer: str
    model: str
    usage: dict[str, Any]


@router.post("/ask", response_model=HelpAskResponse)
async def ask_help(req: HelpAskRequest) -> HelpAskResponse:
    """Responde uma pergunta contextual sobre o módulo via LLM.

    Robusto:
    - Módulo desconhecido → 404.
    - LLM falha → 503 com erro explícito (não derruba o app).
    """
    with _tracer.start_as_current_span("help.ask") as span:
        span.set_attribute("module.id", req.module_id)
        span.set_attribute("question.length", len(req.question))

        modules = _load_module_guide()
        ctx = modules.get(req.module_id)
        if ctx is None:
            raise HTTPException(404, f"Módulo '{req.module_id}' desconhecido.")

        system_prompt = (
            "Você é um assistente técnico do produto Maestro, "
            "uma plataforma multi-agente. Responda perguntas do usuário sobre o "
            "MÓDULO específico que ele está consultando. Use as informações do "
            "CONTEXTO abaixo como verdade canônica. Seja direto e prático.\n\n"
            "REGRAS:\n"
            "- Se não souber, diga que não sabe — não invente comandos ou flags.\n"
            "- Se a pergunta sair do escopo do módulo, redirecione gentilmente.\n"
            "- Use blocos de código (```bash) para comandos.\n"
            "- Responda em português do Brasil. Tom direto, sem floreio.\n"
            "- Limite resposta a ~250 palavras."
        )

        ctx_text = (
            f"=== MÓDULO: {ctx.get('section', '')} — {ctx.get('label', '')} ===\n\n"
            f"FUNDAMENTO:\n{ctx.get('fundamento', '')}\n\n"
            f"APLICAÇÃO:\n{ctx.get('aplicacao', '')}\n\n"
            f"COMO ATIVAR:\n{ctx.get('ativar', '')}\n\n"
            f"COMO USAR:\n{ctx.get('usar', '')}"
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": ctx_text + "\n\n=== PERGUNTA DO USUÁRIO ===\n" + req.question},
        ]

        # Adiciona histórico recente (máx MAX_HISTORY turnos prévios) entre o
        # contexto e a pergunta atual.
        if req.history:
            tail = req.history[-MAX_HISTORY:]
            # Insere antes da última mensagem (que é a pergunta atual)
            messages = [messages[0]] + tail + [messages[-1]]

        try:
            provider = get_provider("azure")
            resp = await provider.generate(messages, max_tokens=MAX_TOKENS)
            answer = (resp.get("content") or "").strip()
            if not answer:
                answer = "(O modelo não retornou resposta. Tente reformular a pergunta.)"
            span.set_attribute("response.length", len(answer))
            return HelpAskResponse(
                answer=answer,
                model=resp.get("model", "azure"),
                usage=resp.get("usage", {}) or {},
            )
        except Exception as e:
            logger.warning(f"help.ask falhou: {type(e).__name__}: {e}")
            raise HTTPException(503, f"Assistente IA indisponível: {type(e).__name__}: {str(e)[:160]}")


# ─── Variante para "Ajuda desta página" ─────────────────────────
# Recebe o contexto direto do front (helpContent já tem os dados estruturados
# em base.html). Não depende de module-guide.js.

_PAGE_SYSTEM_PROMPT = (
    "Você é um assistente técnico do produto Maestro. "
    "O usuário está navegando em uma PÁGINA específica e quer ajuda contextual "
    "para usá-la. Use o CONTEXTO DA PÁGINA abaixo como verdade canônica.\n\n"
    "REGRAS:\n"
    "- Foco no fluxo da página (criar/editar/executar). Nada genérico.\n"
    "- Se a pergunta for sobre tela/elemento/botão da página, descreva em 1-2 passos diretos.\n"
    "- Se sair do escopo da página, redirecione gentilmente.\n"
    "- Use blocos ```bash``` para comandos curl quando relevante.\n"
    "- Português do Brasil. Tom direto. Limite ~250 palavras."
)


@router.post("/ask-context", response_model=HelpAskResponse)
async def ask_help_context(req: HelpAskContextRequest) -> HelpAskResponse:
    """Variante context-driven do help — recebe os campos diretamente.

    Usado pelo drawer "Ajuda desta página" que tem os dados em helpContent
    no template (não em module-guide.js).
    """
    with _tracer.start_as_current_span("help.ask_context") as span:
        span.set_attribute("page.title", req.title)
        span.set_attribute("page.section", req.section)
        span.set_attribute("question.length", len(req.question))

        # Strip de qualquer HTML que tenha vindo (front usa textContent, mas defesa)
        what_t = _strip_html(req.what)
        foundation_t = _strip_html(req.foundation)
        usage_t = _strip_html(req.usage)

        ctx_text = (
            f"=== PÁGINA: {req.title} — {req.section} ===\n\n"
            f"O QUE É:\n{what_t}\n\n"
            f"FUNDAMENTO (especificação):\n{foundation_t}\n\n"
            f"COMO USAR:\n{usage_t}"
        )

        messages: list[dict] = [
            {"role": "system", "content": _PAGE_SYSTEM_PROMPT},
            {"role": "user", "content": ctx_text + "\n\n=== PERGUNTA DO USUÁRIO ===\n" + req.question},
        ]
        if req.history:
            tail = req.history[-MAX_HISTORY:]
            messages = [messages[0]] + tail + [messages[-1]]

        try:
            provider = get_provider("azure")
            resp = await provider.generate(messages, max_tokens=MAX_TOKENS)
            answer = (resp.get("content") or "").strip()
            if not answer:
                answer = "(O modelo não retornou resposta. Tente reformular a pergunta.)"
            span.set_attribute("response.length", len(answer))
            return HelpAskResponse(
                answer=answer,
                model=resp.get("model", "azure"),
                usage=resp.get("usage", {}) or {},
            )
        except Exception as e:
            logger.warning(f"help.ask_context falhou: {type(e).__name__}: {e}")
            raise HTTPException(503, f"Assistente IA indisponível: {type(e).__name__}: {str(e)[:160]}")
